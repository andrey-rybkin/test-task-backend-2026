import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event, text

from amocrm import CONTACTS, COMPANIES, AmoCrmError, Customer, Lead
from main import collect_leads_customers
from models import AbstractCustomer, AmoCompany, AmoContact, AmoCustomer, AmoLead, db


DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(DATABASE_URL, 'set TEST_DATABASE_URL to a disposable PostgreSQL database')
class SyncIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(cls.app)
        cls.schema = 'sync_test_' + uuid.uuid4().hex[:12]
        with cls.app.app_context():
            engine = db.engine
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA {cls.schema}'))
            engine.dispose()

            def set_schema(dbapi_conn, _):
                dbapi_conn.autocommit = True
                try:
                    with dbapi_conn.cursor() as cursor:
                        cursor.execute(f'SET search_path TO {cls.schema}')
                finally:
                    dbapi_conn.autocommit = False

            cls.set_schema = set_schema
            event.listen(engine, 'connect', set_schema)
            with engine.connect() as conn:
                if conn.scalar(text('SELECT current_schema()')) != cls.schema:
                    raise RuntimeError('Refusing to create tables outside the isolated test schema')
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            engine = db.engine
            event.remove(engine, 'connect', cls.set_schema)
            engine.dispose()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA {cls.schema} CASCADE'))

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        if db.session.scalar(text('SELECT current_schema()')) != self.schema:
            raise RuntimeError('Refusing to drop tables outside the isolated test schema')
        db.session.remove()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    @staticmethod
    def customer(crm_id, login, name='Buyer', contacts=None):
        return Customer(id=crm_id, name=name, custom_fields_values={
            'Логин': {'value': login} if login is not None else None,
        }, _embedded={CONTACTS: contacts or [], COMPANIES: []})

    @staticmethod
    def lead(crm_id, login, name='Deal', contacts=None):
        return Lead(id=crm_id, name=name, custom_fields_values={
            'Логин': {'value': login} if login is not None else None,
        }, _embedded={CONTACTS: contacts or []})

    def run_sync(self, customers, leads, full=True):
        def fake_get(cls, **kwargs):
            return iter(customers if cls is Customer else leads)
        with patch('main.amocrm_get', side_effect=fake_get):
            collect_leads_customers(full)

    def test_merge_keeps_lead_aggregate_and_moves_everything(self):
        deal_owner = AbstractCustomer(send_methods=[{'type': 'mail', 'value': 'a', 'desc': 'target'}])
        buyer_owner = AbstractCustomer(send_methods=[{'type': 'sms', 'value': 'b', 'desc': 'source'}])
        db.session.add_all([deal_owner, buyer_owner])
        db.session.flush()
        deal_owner_id, buyer_owner_id = deal_owner.id, buyer_owner.id
        db.session.add_all([
            AmoLead(crm_id=10, abstract_customer_id=deal_owner.id,
                    data={'general': {'id': 10, 'login': 'old', 'name': 'Deal'}, 'api_value': {}},
                    contact_ids=[], company_ids=[]),
            AmoCustomer(crm_id=20, abstract_customer_id=buyer_owner.id,
                        data={'general': {'id': 20, 'login': 'new', 'name': 'Buyer'}, 'api_value': {}},
                        contact_ids=[], company_ids=[]),
        ])
        db.session.commit()

        self.run_sync([self.customer(20, 'new')], [self.lead(10, 'new')])

        self.assertIsNotNone(db.session.get(AbstractCustomer, deal_owner_id))
        self.assertIsNone(db.session.get(AbstractCustomer, buyer_owner_id))
        self.assertEqual(db.session.get(AmoCustomer, 20).abstract_customer_id, deal_owner_id)
        self.assertEqual(db.session.get(AmoLead, 10).data['general']['login'], 'new')
        self.assertEqual({m['type'] for m in db.session.get(AbstractCustomer, deal_owner_id).send_methods},
                         {'mail', 'sms'})

    def test_merge_reindexes_all_members_of_source_aggregate(self):
        target, source = AbstractCustomer(), AbstractCustomer()
        db.session.add_all([target, source])
        db.session.flush()
        target_id, source_id = target.id, source.id
        db.session.add(AmoLead(
            crm_id=10, abstract_customer_id=target_id,
            data={'general': {'id': 10, 'login': 'old', 'name': 'Deal'}, 'api_value': {}},
            contact_ids=[], company_ids=[]))
        for crm_id in (20, 21):
            db.session.add(AmoCustomer(
                crm_id=crm_id, abstract_customer_id=source_id,
                data={'general': {'id': crm_id, 'login': 'shared', 'name': 'Buyer'},
                      'api_value': {}}, contact_ids=[], company_ids=[]))
        db.session.commit()

        self.run_sync([self.customer(20, 'shared'), self.customer(21, 'shared')],
                      [self.lead(10, 'shared'), self.lead(11, 'shared')])
        self.assertIsNone(db.session.get(AbstractCustomer, source_id))
        for model, crm_id in ((AmoCustomer, 20), (AmoCustomer, 21),
                              (AmoLead, 10), (AmoLead, 11)):
            self.assertEqual(db.session.get(model, crm_id).abstract_customer_id, target_id)

    def test_ambiguous_login_is_ignored(self):
        first, second = AbstractCustomer(), AbstractCustomer()
        db.session.add_all([first, second])
        db.session.flush()
        first_id, second_id = first.id, second.id
        for aggregate in (first, second):
            db.session.add(AmoCustomer(
                crm_id=100 + aggregate.id, abstract_customer_id=aggregate.id,
                data={'general': {'id': 100 + aggregate.id, 'login': 'shared', 'name': 'Buyer'},
                      'api_value': {}}, contact_ids=[], company_ids=[]))
        db.session.commit()

        self.run_sync([], [self.lead(900, 'shared')])
        deal = db.session.get(AmoLead, 900)
        self.assertIsNotNone(deal)
        self.assertNotIn(deal.abstract_customer_id, (first_id, second_id))

    def test_failed_crm_read_does_not_touch_live_data(self):
        aggregate = AbstractCustomer()
        db.session.add(aggregate)
        db.session.flush()
        db.session.add(AmoCustomer(
            crm_id=1, abstract_customer_id=aggregate.id,
            data={'general': {'id': 1, 'login': 'old', 'name': 'Old'}, 'api_value': {}},
            contact_ids=[], company_ids=[]))
        db.session.commit()

        def fake_get(cls, **kwargs):
            if cls is Customer:
                return iter([self.customer(1, 'new')])
            def broken():
                raise RuntimeError('CRM interrupted')
                yield
            return broken()

        with patch('main.amocrm_get', side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                collect_leads_customers(True)
        db.session.expire_all()
        self.assertEqual(db.session.get(AmoCustomer, 1).login, 'old')

    def test_http_204_mid_pagination_does_not_apply_partial_snapshot(self):
        aggregate = AbstractCustomer()
        db.session.add(aggregate)
        db.session.flush()
        db.session.add(AmoCustomer(
            crm_id=1, abstract_customer_id=aggregate.id,
            data={'general': {'id': 1, 'login': 'old', 'name': 'Old'}, 'api_value': {}},
            contact_ids=[], company_ids=[]))
        db.session.commit()
        first_page = SimpleNamespace(status_code=200, json=lambda: {
            '_embedded': {'customers': [{'id': 1, 'name': 'Changed',
                                         'custom_fields_values': [{'field_name': 'Логин',
                                                                   'values': [{'value': 'new'}]}]}]},
            '_links': {'next': {'href': 'https://crm.example/page/2'}},
        })
        no_content = SimpleNamespace(status_code=204, url='https://crm.example/page/2',
                                     request=SimpleNamespace(method='GET'))
        with patch('amocrm.requests.get', side_effect=[first_page, no_content]), \
                patch('amocrm.time.sleep'):
            with self.assertRaises(AmoCrmError):
                collect_leads_customers(True)
        db.session.expire_all()
        self.assertEqual(db.session.get(AmoCustomer, 1).login, 'old')

    def test_application_error_rolls_back_all_upserts(self):
        from main import _apply_kind

        def fail_on_leads(conn, kind, *args):
            if kind == 'lead':
                raise RuntimeError('apply interrupted')
            return _apply_kind(conn, kind, *args)

        with patch('main._apply_kind', side_effect=fail_on_leads):
            with self.assertRaises(RuntimeError):
                self.run_sync([self.customer(1, 'one')], [self.lead(2, 'one')])
        self.assertIsNone(db.session.get(AmoCustomer, 1))
        self.assertEqual(db.session.query(AbstractCustomer).count(), 0)

    def test_failed_stage_cleanup_does_not_leak_session_lock_or_temp_table(self):
        def fail_drop(conn, cursor, statement, parameters, context, many):
            if statement.strip().startswith('DROP TABLE IF EXISTS sync_stage'):
                raise RuntimeError('temp table cleanup failed')

        event.listen(db.engine, 'before_cursor_execute', fail_drop)
        try:
            with self.assertRaisesRegex(RuntimeError, 'temp table cleanup failed'):
                self.run_sync([self.customer(1, 'one')], [])
        finally:
            event.remove(db.engine, 'before_cursor_execute', fail_drop)
        # A new run must not inherit a stale session lock or temporary table.
        self.run_sync([self.customer(1, 'one')], [])
        self.assertIsNotNone(db.session.get(AmoCustomer, 1))

    def test_full_cleanup_and_partial_sync_semantics(self):
        keep = AbstractCustomer()
        stale = AbstractCustomer()
        db.session.add_all([keep, stale])
        db.session.flush()
        stale_id = stale.id
        db.session.add_all([
            AmoCustomer(crm_id=1, abstract_customer_id=keep.id,
                        data={'general': {'id': 1, 'login': 'one', 'name': 'One'}, 'api_value': {}},
                        contact_ids=[], company_ids=[]),
            AmoCustomer(crm_id=2, abstract_customer_id=stale.id,
                        data={'general': {'id': 2, 'login': 'two', 'name': 'Two'}, 'api_value': {}},
                        contact_ids=[], company_ids=[]),
        ])
        db.session.commit()
        self.run_sync([self.customer(1, 'one')], [], full=False)
        self.assertIsNotNone(db.session.get(AmoCustomer, 2))
        self.run_sync([self.customer(1, 'one')], [], full=True)
        self.assertIsNone(db.session.get(AmoCustomer, 2))
        self.assertIsNone(db.session.get(AbstractCustomer, stale_id))

    def test_customer_without_login_is_skipped_but_existing_id_is_kept(self):
        aggregate = AbstractCustomer()
        db.session.add(aggregate)
        db.session.flush()
        aggregate_id = aggregate.id
        db.session.add(AmoCustomer(
            crm_id=31, abstract_customer_id=aggregate_id,
            data={'general': {'id': 31, 'login': 'old', 'name': 'Old'}, 'api_value': {}},
            contact_ids=[], company_ids=[]))
        db.session.commit()

        self.run_sync([self.customer(31, None, name='Should not update'),
                       self.customer(32, None)], [], full=True)

        self.assertEqual(db.session.get(AmoCustomer, 31).data['general']['name'], 'Old')
        self.assertIsNone(db.session.get(AmoCustomer, 32))
        self.assertIsNotNone(db.session.get(AbstractCustomer, aggregate_id))

    def test_marked_entity_is_deleted_even_when_present_in_snapshot(self):
        self.run_sync([self.customer(41, 'marked', name='УдАлИтЬ buyer')], [], full=True)
        self.assertIsNone(db.session.get(AmoCustomer, 41))
        self.assertEqual(db.session.query(AbstractCustomer).count(), 0)

    def test_shared_contact_and_company_follow_current_links(self):
        buyer = self.customer(51, 'linked', contacts=[{'id': 71, 'name': 'Contact'}])
        buyer.companies = [{'id': 81, 'name': 'Company'}]
        deal = self.lead(61, 'linked', contacts=[{'id': 71, 'name': 'Contact'}])
        self.run_sync([buyer], [deal])
        owner = db.session.get(AmoCustomer, 51).abstract_customer_id
        self.assertEqual(db.session.get(AmoLead, 61).abstract_customer_id, owner)
        self.assertIsNotNone(db.session.get(AmoContact, (71, owner)))
        self.assertIsNotNone(db.session.get(AmoCompany, (81, owner)))

        self.run_sync([self.customer(51, 'linked')], [deal])
        self.assertIsNotNone(db.session.get(AmoContact, (71, owner)))
        self.assertIsNone(db.session.get(AmoCompany, (81, owner)))

        self.run_sync([self.customer(51, 'linked')], [])
        self.assertIsNone(db.session.get(AmoContact, (71, owner)))
        self.assertIsNone(db.session.get(AmoLead, 61))


if __name__ == '__main__':
    unittest.main()
