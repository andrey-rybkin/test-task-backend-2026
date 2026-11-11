import json
import logging
from collections import defaultdict

from sqlalchemy import bindparam, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from abstract_customer import get_customer_json, get_lead_json
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import AbstractCustomer, AmoCompany, AmoContact, AmoCustomer, AmoLead, db

logger = logging.getLogger(__name__)
BATCH_SIZE = 500
SYNC_LOCK_ID = 20261109


def _payload(entity, kind):
    return {
        'data': get_customer_json(entity) if kind == 'customer' else get_lead_json(entity),
        'contacts': getattr(entity, 'contacts', None) or [],
        'companies': getattr(entity, 'companies', None) or [],
    }


def _unique(mapping, login):
    matches = mapping.get(login, set()) if login else set()
    if len(matches) > 1:
        logger.warning('Ambiguous CRM login %r; ignoring login match', login)
        return None
    return next(iter(matches), None)


def _dedupe_refs(refs):
    result = {}
    for ref in refs or []:
        if isinstance(ref, dict) and ref.get('id') is not None:
            result[ref['id']] = ref
    return list(result.values())


class RecordIds:
    def __init__(self, conn):
        self.conn = conn
        self.ids = []

    def next(self):
        if not self.ids:
            self.ids = list(self.conn.execute(text("""
                SELECT nextval(pg_get_serial_sequence('abstract_customer', 'id'))
                FROM generate_series(1, :count)
            """), {'count': BATCH_SIZE}).scalars())
        return self.ids.pop()


def _load_indexes(conn):
    maps = {
        'customer_id': {}, 'lead_id': {},
        'customer_login': defaultdict(set), 'lead_login': defaultdict(set),
        'customer_login_by_id': {}, 'lead_login_by_id': {},
        'customer_login_count': defaultdict(int), 'lead_login_count': defaultdict(int),
        'customer_members': defaultdict(set), 'lead_members': defaultdict(set),
    }
    for model, prefix in ((AmoCustomer, 'customer'), (AmoLead, 'lead')):
        for crm_id, aggregate_id, login in conn.execute(
            select(model.crm_id, model.abstract_customer_id, model.login)
        ):
            maps[f'{prefix}_id'][crm_id] = aggregate_id
            maps[f'{prefix}_login_by_id'][crm_id] = (aggregate_id, login)
            maps[f'{prefix}_members'][aggregate_id].add(crm_id)
            if login:
                maps[f'{prefix}_login'][login].add(aggregate_id)
                maps[f'{prefix}_login_count'][(login, aggregate_id)] += 1
    return maps


def _set_login(mapping, counts, login, aggregate_id, add):
    if not login:
        return
    if add:
        key = (login, aggregate_id)
        counts[key] += 1
        mapping[login].add(aggregate_id)
    else:
        key = (login, aggregate_id)
        counts[key] -= 1
        if counts[key] <= 0:
            del counts[key]
            mapping[login].discard(aggregate_id)
            if not mapping[login]:
                del mapping[login]


def _allocate(conn, ids, pending_aggregates):
    aggregate_id = ids.next()
    pending_aggregates.add(aggregate_id)
    return aggregate_id


def _merge_aggregates(conn, target, source, maps, pending_aggregates):
    if target == source:
        return
    if source in pending_aggregates:
        pending_aggregates.discard(source)
    # Move CRM rows; merge related JSON rows by the (aggregate, CRM ID) key.
    for model in (AmoCustomer, AmoLead):
        conn.execute(model.__table__.update().where(
            model.abstract_customer_id == source
        ).values(abstract_customer_id=target))
    for model in (AmoContact, AmoCompany):
        table = model.__table__
        stmt = pg_insert(table).from_select(
            [table.c.crm_id, table.c.abstract_customer_id, table.c.data],
            select(table.c.crm_id, text(':target'), table.c.data)
            .where(table.c.abstract_customer_id == source),
        )
        # Related duplicates already at the winner keep winner's payload.
        conn.execute(stmt.on_conflict_do_nothing(
            index_elements=[table.c.crm_id, table.c.abstract_customer_id]
        ), {'target': target})
        conn.execute(table.delete().where(table.c.abstract_customer_id == source))

    source_methods, target_methods = conn.execute(text(
        'SELECT (SELECT send_methods FROM abstract_customer WHERE id=:source), '
        '(SELECT send_methods FROM abstract_customer WHERE id=:target)'
    ), {'source': source, 'target': target}).one()
    methods = {}
    for item in (source_methods or []) + (target_methods or []):
        methods[(item.get('type'), item.get('value'))] = item
    conn.execute(text('UPDATE abstract_customer SET send_methods=:methods WHERE id=:target').bindparams(
        bindparam('methods', type_=JSONB)),
        {'methods': list(methods.values()), 'target': target})
    conn.execute(text('DELETE FROM abstract_customer WHERE id=:source'), {'source': source})

    for kind in ('customer', 'lead'):
        login_map = maps[f'{kind}_login']
        counts = maps[f'{kind}_login_count']
        members = maps[f'{kind}_members']
        for crm_id in members.pop(source, ()):
            _, login = maps[f'{kind}_login_by_id'][crm_id]
            _set_login(login_map, counts, login, source, False)
            _set_login(login_map, counts, login, target, True)
            maps[f'{kind}_id'][crm_id] = target
            maps[f'{kind}_login_by_id'][crm_id] = (target, login)
            members[target].add(crm_id)


def _upsert(conn, model, rows, conflict, fields):
    if not rows:
        return
    rows = list({tuple(row[name] for name in conflict): row for row in rows}.values())
    table = model.__table__
    stmt = pg_insert(table).values(rows)
    conn.execute(stmt.on_conflict_do_update(
        index_elements=[table.c[name] for name in conflict],
        set_={name: getattr(stmt.excluded, name) for name in fields},
        where=text(' OR '.join(f'{table.name}.{name} IS DISTINCT FROM excluded.{name}' for name in fields)),
    ))


def _stage_stream(conn, entity_cls, kind, with_value, full):
    stream = amocrm_get(entity_cls, _with=with_value)
    rows = []
    ordinal = 0
    for entity in stream:
        if not full and ((kind == 'customer' and entity.status_id == 111349)
                         or (kind == 'lead' and entity.status_id == 143)):
            continue
        if entity.id is None:
            raise ValueError(f'{kind} CRM entity has no id')
        if kind == 'customer' and not entity.get_field('Логин'):
            # Keep the ID in staging for safe full cleanup, but do not write it.
            payload = {'data': None, 'contacts': entity.contacts or [],
                       'companies': entity.companies or []}
        else:
            payload = _payload(entity, kind)
        rows.append({'kind': kind, 'ordinal': ordinal, 'crm_id': entity.id,
                     'login': entity.get_field('Логин'), 'payload': payload})
        ordinal += 1
        if len(rows) == BATCH_SIZE:
            conn.execute(text("""
                INSERT INTO sync_stage(kind, ordinal, crm_id, login, payload)
                VALUES (:kind, :ordinal, :crm_id, :login, CAST(:payload AS jsonb))
            """), [{**r, 'payload': json.dumps(r['payload'])} for r in rows])
            conn.commit()
            rows.clear()
    if rows:
        conn.execute(text("""
            INSERT INTO sync_stage(kind, ordinal, crm_id, login, payload)
            VALUES (:kind, :ordinal, :crm_id, :login, CAST(:payload AS jsonb))
        """), [{**r, 'payload': json.dumps(r['payload'])} for r in rows])
        conn.commit()


def _related_data(ref):
    general = {'id': ref['id']}
    general.update({key: ref[key] for key in ('name', 'phone', 'inn') if key in ref})
    return {'general': general, 'api_value': ref}


def _apply_kind(conn, kind, maps, ids, is_full):
    is_customer = kind == 'customer'
    model = AmoCustomer if is_customer else AmoLead
    other_map = maps['lead_login'] if is_customer else maps['customer_login']
    id_map = maps['customer_id'] if is_customer else maps['lead_id']
    login_map = maps['customer_login'] if is_customer else maps['lead_login']
    by_id_login = maps['customer_login_by_id'] if is_customer else maps['lead_login_by_id']
    members = maps['customer_members'] if is_customer else maps['lead_members']
    entity_rows, contact_rows, company_rows = [], [], []
    pending_aggregates = set()
    offset = 0

    def flush():
        if pending_aggregates:
            conn.execute(insert(AbstractCustomer.__table__), [{'id': value} for value in pending_aggregates])
            pending_aggregates.clear()
        update_fields = ['abstract_customer_id', 'data']
        if is_full:
            update_fields.extend(['contact_ids', 'company_ids'])
        _upsert(conn, model, entity_rows, ['crm_id'], update_fields)
        if is_full:
            _upsert(conn, AmoContact, contact_rows, ['crm_id', 'abstract_customer_id'], ['data'])
            if is_customer:
                _upsert(conn, AmoCompany, company_rows, ['crm_id', 'abstract_customer_id'], ['data'])
        entity_rows.clear()
        contact_rows.clear()
        company_rows.clear()

    while True:
        staged = conn.execute(text("""
            SELECT crm_id, login, payload FROM sync_stage
            WHERE kind=:kind AND ordinal >= :offset
            ORDER BY ordinal LIMIT :limit
        """), {'kind': kind, 'offset': offset, 'limit': BATCH_SIZE}).mappings().all()
        if not staged:
            break
        offset += len(staged)
        for item in staged:
            crm_id, login, payload = item['crm_id'], item['login'], item['payload']
            if payload['data'] is None:
                previous = by_id_login.get(crm_id)
                if previous:
                    _set_login(login_map, maps[f'{"customer" if is_customer else "lead"}_login_count'],
                               previous[1], previous[0], False)
                    by_id_login[crm_id] = (previous[0], None)
                continue
            by_id = id_map.get(crm_id)
            by_login = _unique(other_map, login)
            aggregate_id = by_id or by_login
            if not is_customer and by_id is not None and by_login is not None and by_id != by_login:
                flush()
                _merge_aggregates(conn, by_id, by_login, maps, pending_aggregates)
                aggregate_id = by_id
            if aggregate_id is None:
                aggregate_id = _allocate(conn, ids, pending_aggregates)

            old = by_id_login.get(crm_id)
            if old:
                _set_login(login_map, maps[f'{"customer" if is_customer else "lead"}_login_count'],
                           old[1], old[0], False)
                if old[0] != aggregate_id:
                    members[old[0]].discard(crm_id)
            entity_rows.append({
                'crm_id': crm_id, 'abstract_customer_id': aggregate_id,
                'data': payload['data'],
                'contact_ids': sorted({r['id'] for r in _dedupe_refs(payload['contacts'])}),
                'company_ids': sorted({r['id'] for r in _dedupe_refs(payload['companies'])}) if is_customer else [],
            })
            id_map[crm_id] = aggregate_id
            by_id_login[crm_id] = (aggregate_id, login)
            members[aggregate_id].add(crm_id)
            _set_login(login_map, maps[f'{"customer" if is_customer else "lead"}_login_count'],
                       login, aggregate_id, True)
            if is_full:
                for ref in _dedupe_refs(payload['contacts']):
                    contact_rows.append({'crm_id': ref['id'], 'abstract_customer_id': aggregate_id,
                                         'data': _related_data(ref)})
                if is_customer:
                    for ref in _dedupe_refs(payload['companies']):
                        company_rows.append({'crm_id': ref['id'], 'abstract_customer_id': aggregate_id,
                                             'data': _related_data(ref)})

        flush()


def collect_leads_customers(is_full=True):
    """Stage the complete CRM snapshot, then apply it atomically to PostgreSQL."""
    conn = db.engine.connect()
    locked = False
    try:
        locked = bool(conn.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': SYNC_LOCK_ID}).scalar())
        conn.commit()
        if not locked:
            raise RuntimeError('Another CRM synchronization is already running')
        conn.execute(text('''
            CREATE TEMP TABLE sync_stage (
                kind text NOT NULL, ordinal bigint NOT NULL, crm_id bigint NOT NULL,
                login text, payload jsonb NOT NULL,
                PRIMARY KEY (kind, crm_id), UNIQUE (kind, ordinal)
            ) ON COMMIT PRESERVE ROWS
        '''))
        conn.commit()
        _stage_stream(conn, Customer, 'customer', f'{COMPANIES},{CONTACTS}', is_full)
        _stage_stream(conn, Lead, 'lead', CONTACTS, is_full)

        with conn.begin():
            maps = _load_indexes(conn)
            ids = RecordIds(conn)
            _apply_kind(conn, 'customer', maps, ids, is_full)
            _apply_kind(conn, 'lead', maps, ids, is_full)
            if is_full:
                for table in ('amo_customer', 'amo_lead', 'amo_contact', 'amo_company'):
                    conn.execute(text(f"""
                        DELETE FROM {table}
                        WHERE COALESCE(name, '') ~ '[уУ][дД][аА][лЛ][иИ][тТ][ьЬ]'
                    """))
                conn.execute(text('''
                    DELETE FROM amo_customer c
                    WHERE NOT EXISTS (SELECT 1 FROM sync_stage s
                                      WHERE s.kind='customer' AND s.crm_id=c.crm_id)
                '''))
                conn.execute(text('''
                    DELETE FROM amo_lead l
                    WHERE NOT EXISTS (SELECT 1 FROM sync_stage s
                                      WHERE s.kind='lead' AND s.crm_id=l.crm_id)
                '''))
                conn.execute(text('''
                    DELETE FROM amo_contact c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM amo_customer ac
                        WHERE ac.abstract_customer_id=c.abstract_customer_id
                          AND c.crm_id = ANY(ac.contact_ids)
                    ) AND NOT EXISTS (
                        SELECT 1 FROM amo_lead al
                        WHERE al.abstract_customer_id=c.abstract_customer_id
                          AND c.crm_id = ANY(al.contact_ids)
                    )
                '''))
                conn.execute(text('''
                    DELETE FROM amo_company c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM amo_customer ac
                        WHERE ac.abstract_customer_id=c.abstract_customer_id
                          AND c.crm_id = ANY(ac.company_ids)
                    )
                '''))
                conn.execute(text('''
                    DELETE FROM abstract_customer a
                    WHERE NOT EXISTS (SELECT 1 FROM amo_customer c WHERE c.abstract_customer_id=a.id)
                      AND NOT EXISTS (SELECT 1 FROM amo_lead l WHERE l.abstract_customer_id=a.id)
                '''))
    except Exception:
        conn.rollback()
        logger.exception('CRM synchronization failed; full-snapshot cleanup was not committed')
        raise
    finally:
        try:
            if conn.in_transaction():
                conn.rollback()
            try:
                conn.execute(text('DROP TABLE IF EXISTS sync_stage'))
                conn.commit()
            except Exception:
                conn.invalidate()  # The failed session may still hold a temp table or advisory lock.
                raise
            finally:
                if locked and not conn.invalidated:
                    if conn.in_transaction():
                        conn.rollback()
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': SYNC_LOCK_ID})
                        conn.commit()
                    except Exception:
                        conn.invalidate()
                        raise
        finally:
            conn.close()
