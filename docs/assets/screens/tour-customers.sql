-- A SQL spec: the same model as YAML, written as the CREATE you'd write anyway.
CREATE TABLE ${catalog}.sales.customers (
  customer_id BIGINT NOT NULL COMMENT 'Surrogate key',
  name        STRING,
  country     STRING,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
COMMENT 'One row per customer'
CLUSTER BY AUTO;
