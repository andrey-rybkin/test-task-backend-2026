BEGIN;
ALTER TABLE abstract_customer
    DROP COLUMN amo_customers,
    DROP COLUMN amo_leads,
    DROP COLUMN amo_contacts,
    DROP COLUMN amo_companies;
COMMIT;
