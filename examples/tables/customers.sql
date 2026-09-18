-- A SQL spec: the same model as a YAML one, written as the CREATE statement
-- you'd write anyway. It's a declaration, never run as written — deltaplan
-- plans its own statements from the difference with the live table.
-- What SQL specs support: https://misja-pronk.github.io/deltaplan/formats/
CREATE TABLE ${catalog}.sales.customers (
  customer_id BIGINT NOT NULL COMMENT 'Surrogate key',
  name        STRING,
  country     STRING,
  created_at  TIMESTAMP,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
COMMENT 'One row per customer'
CLUSTER BY AUTO;

ALTER TABLE ${catalog}.sales.customers SET TAGS ('domain' = 'sales');
GRANT SELECT ON TABLE ${catalog}.sales.customers TO `analysts`;
