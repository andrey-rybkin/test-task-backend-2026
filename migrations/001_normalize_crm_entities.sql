BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM abstract_customer a
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(a.amo_customers)='array' THEN a.amo_customers ELSE '[]'::jsonb END
        ) e(item)
        GROUP BY e.item #>> '{general,id}'
        HAVING e.item #>> '{general,id}' IS NULL OR count(*) > 1 OR count(DISTINCT a.id) > 1
    ) OR EXISTS (
        SELECT 1
        FROM abstract_customer a
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(a.amo_leads)='array' THEN a.amo_leads ELSE '[]'::jsonb END
        ) e(item)
        GROUP BY e.item #>> '{general,id}'
        HAVING e.item #>> '{general,id}' IS NULL OR count(*) > 1 OR count(DISTINCT a.id) > 1
    ) THEN
        RAISE EXCEPTION 'CRM IDs are missing or duplicated across abstract_customer rows; resolve before migration';
    END IF;
END $$;

CREATE TABLE amo_customer (
    crm_id BIGINT PRIMARY KEY,
    abstract_customer_id BIGINT NOT NULL REFERENCES abstract_customer(id) ON DELETE CASCADE,
    data JSONB NOT NULL,
    login TEXT GENERATED ALWAYS AS (data -> 'general' ->> 'login') STORED,
    name TEXT GENERATED ALWAYS AS (data -> 'general' ->> 'name') STORED,
    contact_ids BIGINT[] NOT NULL DEFAULT '{}',
    company_ids BIGINT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP
);
CREATE INDEX ix_amo_customer_aggregate ON amo_customer(abstract_customer_id);
CREATE INDEX ix_amo_customer_login ON amo_customer(login);
CREATE INDEX ix_amo_customer_name ON amo_customer(name);

CREATE TABLE amo_lead (LIKE amo_customer INCLUDING DEFAULTS INCLUDING GENERATED);
ALTER TABLE amo_lead ADD CONSTRAINT pk_amo_lead PRIMARY KEY (crm_id);
ALTER TABLE amo_lead ADD CONSTRAINT fk_amo_lead_aggregate
    FOREIGN KEY (abstract_customer_id) REFERENCES abstract_customer(id) ON DELETE CASCADE;
CREATE INDEX ix_amo_lead_aggregate ON amo_lead(abstract_customer_id);
CREATE INDEX ix_amo_lead_login ON amo_lead(login);
CREATE INDEX ix_amo_lead_name ON amo_lead(name);

CREATE TABLE amo_contact (
    crm_id BIGINT NOT NULL,
    abstract_customer_id BIGINT NOT NULL REFERENCES abstract_customer(id) ON DELETE CASCADE,
    data JSONB NOT NULL,
    name TEXT GENERATED ALWAYS AS (data -> 'general' ->> 'name') STORED,
    PRIMARY KEY (crm_id, abstract_customer_id)
);
CREATE INDEX ix_amo_contact_name ON amo_contact(name);
CREATE INDEX ix_amo_contact_aggregate ON amo_contact(abstract_customer_id);

CREATE TABLE amo_company (
    crm_id BIGINT NOT NULL,
    abstract_customer_id BIGINT NOT NULL REFERENCES abstract_customer(id) ON DELETE CASCADE,
    data JSONB NOT NULL,
    name TEXT GENERATED ALWAYS AS (data -> 'general' ->> 'name') STORED,
    PRIMARY KEY (crm_id, abstract_customer_id)
);
CREATE INDEX ix_amo_company_name ON amo_company(name);
CREATE INDEX ix_amo_company_aggregate ON amo_company(abstract_customer_id);

INSERT INTO amo_customer(crm_id, abstract_customer_id, data, contact_ids, company_ids)
SELECT (item #>> '{general,id}')::bigint, aggregate_id, item,
       ARRAY(SELECT (ref->>'id')::bigint FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(item->'api_value'->'_embedded'->'contacts')='array'
                      THEN item->'api_value'->'_embedded'->'contacts' ELSE '[]'::jsonb END) ref
             WHERE ref->>'id' ~ '^[0-9]+$'),
       ARRAY(SELECT (ref->>'id')::bigint FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(item->'api_value'->'_embedded'->'companies')='array'
                      THEN item->'api_value'->'_embedded'->'companies' ELSE '[]'::jsonb END) ref
             WHERE ref->>'id' ~ '^[0-9]+$')
FROM (
    SELECT a.id aggregate_id, e.item
    FROM abstract_customer a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.amo_customers)='array' THEN a.amo_customers ELSE '[]'::jsonb END
    ) e(item)
) source;

INSERT INTO amo_lead(crm_id, abstract_customer_id, data, contact_ids)
SELECT (item #>> '{general,id}')::bigint, aggregate_id, item,
       ARRAY(SELECT (ref->>'id')::bigint FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(item->'api_value'->'_embedded'->'contacts')='array'
                      THEN item->'api_value'->'_embedded'->'contacts' ELSE '[]'::jsonb END) ref
             WHERE ref->>'id' ~ '^[0-9]+$')
FROM (
    SELECT a.id aggregate_id, e.item
    FROM abstract_customer a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.amo_leads)='array' THEN a.amo_leads ELSE '[]'::jsonb END
    ) e(item)
) source;

-- Existing nested JSON has no reliable ownership other than its containing aggregate.
INSERT INTO amo_contact(crm_id, abstract_customer_id, data)
SELECT DISTINCT ON (a.id, (item #>> '{general,id}')::bigint)
       (item #>> '{general,id}')::bigint, a.id, item
FROM abstract_customer a
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(a.amo_contacts)='array' THEN a.amo_contacts ELSE '[]'::jsonb END
) e(item)
WHERE item #>> '{general,id}' ~ '^[0-9]+$'
ORDER BY a.id, (item #>> '{general,id}')::bigint;

INSERT INTO amo_company(crm_id, abstract_customer_id, data)
SELECT DISTINCT ON (a.id, (item #>> '{general,id}')::bigint)
       (item #>> '{general,id}')::bigint, a.id, item
FROM abstract_customer a
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(a.amo_companies)='array' THEN a.amo_companies ELSE '[]'::jsonb END
) e(item)
WHERE item #>> '{general,id}' ~ '^[0-9]+$'
ORDER BY a.id, (item #>> '{general,id}')::bigint;

COMMIT;
