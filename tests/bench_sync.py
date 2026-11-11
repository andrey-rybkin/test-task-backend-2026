"""Reproducible synthetic first/repeat/changed sync on an isolated PostgreSQL schema.

TEST_DATABASE_URL=postgresql+psycopg://... python tests/bench_sync.py 20000 50000
"""
import json
import os
import sys
import time
import uuid

from flask import Flask
from sqlalchemy import event, text

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import main
from amocrm import COMPANIES, CONTACTS, Customer, Lead
from models import db

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None


def peak_rss_mib():
    if sys.platform.startswith('linux'):
        with open('/proc/self/status', encoding='ascii') as status:
            return round(next(int(line.split()[1]) for line in status
                              if line.startswith('VmHWM:')) / 1024, 1)
    if resource:
        units = 1024**2 if sys.platform == 'darwin' else 1024
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / units, 1)
    return None


def run(buyers, deals):
    if buyers < 1 or deals < 0:
        raise ValueError('buyers must be positive and deals nonnegative')
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['TEST_DATABASE_URL']
    db.init_app(app)
    with app.app_context():
        engine = db.engine
        if engine.dialect.name != 'postgresql':
            raise ValueError('This benchmark requires PostgreSQL')
        schema = 'sync_bench_' + uuid.uuid4().hex[:12]
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
        engine.dispose()

        def set_schema(dbapi_conn, _):
            dbapi_conn.autocommit = True
            try:
                with dbapi_conn.cursor() as cursor:
                    cursor.execute(f'SET search_path TO {schema}')
            finally:
                dbapi_conn.autocommit = False

        event.listen(engine, 'connect', set_schema)
        try:
            with engine.connect() as conn:
                if conn.scalar(text('SELECT current_schema()')) != schema:
                    raise RuntimeError('Refusing to run outside the isolated benchmark schema')
            db.create_all()
            filler = 'x' * 2500
            original_stage = main._stage_stream

            for scenario in ('first', 'repeat', 'changed'):
                def stream(cls, **_):
                    if cls is Customer:
                        return (Customer(
                            id=i, name=f'Buyer {i}' + (' edited' if scenario == 'changed' and i % 20 == 0 else ''),
                            custom_fields_values={'Логин': {'value': f'user-{i}'},
                                                  'note': {'value': filler}},
                            _embedded={CONTACTS: [], COMPANIES: []},
                        ) for i in range(1, buyers + 1))
                    return (Lead(
                        id=100000 + i,
                        name=f'Deal {i}' + (' edited' if scenario == 'changed' and i % 20 == 0 else ''),
                        custom_fields_values={'Логин': {'value': f'user-{(i - 1) % buyers + 1}'},
                                              'note': {'value': filler}},
                        _embedded={CONTACTS: []},
                    ) for i in range(1, deals + 1)
                      if scenario != 'changed' or i % 100 != 0)

                stage = {}

                def measure_stage(conn, cls, kind, with_value, full):
                    original_stage(conn, cls, kind, with_value, full)
                    if kind == 'lead':
                        stage['mib'] = round(conn.execute(text(
                            "SELECT pg_total_relation_size('pg_temp.sync_stage')"
                        )).scalar() / 2**20, 2)
                        conn.commit()
                        counts['commits'] -= 1  # Measurement's own commit is not part of sync.

                counts = {'sql': 0, 'commits': 0}

                def count_sql(conn, cursor, statement, parameters, context, many):
                    if 'pg_total_relation_size' not in statement:
                        counts['sql'] += 1

                def count_commit(conn):
                    counts['commits'] += 1

                event.listen(engine, 'before_cursor_execute', count_sql)
                event.listen(engine, 'commit', count_commit)
                main.amocrm_get = stream
                main._stage_stream = measure_stage
                started = time.perf_counter()
                try:
                    main.collect_leads_customers(True)
                    seconds = round(time.perf_counter() - started, 2)
                finally:
                    event.remove(engine, 'before_cursor_execute', count_sql)
                    event.remove(engine, 'commit', count_commit)
                    main._stage_stream = original_stage

                with engine.connect() as conn:
                    rows = conn.execute(text('''
                        SELECT (SELECT count(*) FROM amo_customer),
                               (SELECT count(*) FROM amo_lead),
                               (SELECT count(*) FROM abstract_customer)
                    ''')).one()
                assert tuple(rows) == (buyers, deals - (deals // 100 if scenario == 'changed' else 0), buyers), rows
                print(json.dumps({'scenario': scenario, 'seconds': seconds, **counts,
                                  'peak_process_rss_mib': peak_rss_mib(), 'stage_mib': stage['mib'],
                                  'rows': tuple(rows)}, ensure_ascii=False), flush=True)

            with engine.connect() as conn:
                for table, field, value in (
                    ('amo_customer', 'login', 'user-1'),
                    ('amo_lead', 'name', 'Deal 1'),
                ):
                    conn.execute(text(f'ANALYZE {table}'))
                    plan = conn.execute(text(
                        f'EXPLAIN (ANALYZE, BUFFERS) SELECT crm_id FROM {table} WHERE {field}=:value'
                    ), {'value': value}).scalars().all()
                    scan = next((line.strip() for line in plan if 'Index' in line or 'Seq Scan' in line), plan[0])
                    print(json.dumps({'lookup': f'{table}.{field}', 'plan': scan}), flush=True)
        finally:
            db.session.remove()
            event.remove(engine, 'connect', set_schema)
            engine.dispose()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))


if __name__ == '__main__':
    run(int(sys.argv[1]), int(sys.argv[2]))
