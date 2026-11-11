import os
import unittest
import uuid
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url


DATABASE_URL = os.environ.get('TEST_DATABASE_URL')
MIGRATIONS = Path(__file__).resolve().parent.parent / 'migrations'


@unittest.skipUnless(DATABASE_URL, 'set TEST_DATABASE_URL to a disposable PostgreSQL database')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        dsn = make_url(DATABASE_URL).set(drivername='postgresql').render_as_string(hide_password=False)
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.schema = 'migration_check_' + uuid.uuid4().hex[:12]
        self.conn.execute(f'CREATE SCHEMA {self.schema}')
        self.conn.execute(f'SET search_path TO {self.schema}')
        self.conn.execute('''
            CREATE TABLE abstract_customer (
                id BIGSERIAL PRIMARY KEY, amo_customers JSONB, amo_leads JSONB,
                amo_contacts JSONB, amo_companies JSONB, send_methods JSONB,
                created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP
            )
        ''')

    def tearDown(self):
        self.conn.execute('ROLLBACK')
        self.conn.execute(f'DROP SCHEMA {self.schema} CASCADE')
        self.conn.close()

    def migration(self, filename):
        self.conn.execute((MIGRATIONS / filename).read_text(encoding='utf-8'))

    def test_migrates_json_and_drops_legacy_columns_only_in_second_step(self):
        self.conn.execute('''
            INSERT INTO abstract_customer (amo_customers, amo_leads, amo_contacts,
                                           amo_companies, send_methods)
            VALUES (%s, %s, %s, %s, %s)
        ''', (
            '[{"general":{"id":11,"login":"buyer","name":"Buyer"},"api_value":{"_embedded":{"contacts":[{"id":31}],"companies":[{"id":41}]}}}]',
            '[{"general":{"id":21,"login":"buyer","name":"Deal"},"api_value":{}}]',
            '[{"general":{"id":31,"name":"Contact"}}]',
            '[{"general":{"id":41,"name":"Company"}}]',
            '[{"type":"mail","value":"x"}]',
        ))
        self.migration('001_normalize_crm_entities.sql')
        self.assertEqual(self.conn.execute('''
            SELECT abstract_customer_id, login, name, contact_ids, company_ids
            FROM amo_customer WHERE crm_id=11
        ''').fetchone(), (1, 'buyer', 'Buyer', [31], [41]))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM amo_lead').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM amo_contact').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM amo_company').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT send_methods FROM abstract_customer WHERE id=1').fetchone()[0],
                         [{'type': 'mail', 'value': 'x'}])
        self.migration('002_drop_legacy_jsonb.sql')
        self.assertEqual(self.conn.execute('''
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema=%s AND table_name='abstract_customer' AND column_name LIKE 'amo_%%'
        ''', (self.schema,)).fetchone()[0], 0)

    def test_duplicate_crm_id_aborts_without_dropping_source(self):
        self.conn.execute('''
            INSERT INTO abstract_customer (amo_customers)
            VALUES ('[{"general":{"id":11}}]'), ('[{"general":{"id":11}}]')
        ''')
        with self.assertRaises(psycopg.errors.RaiseException):
            self.migration('001_normalize_crm_entities.sql')
        self.conn.execute('ROLLBACK')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM abstract_customer').fetchone()[0], 2)
        self.assertIsNone(self.conn.execute("SELECT to_regclass('amo_customer')").fetchone()[0])
