CREATE TABLE ${catalog}.crm.customers (
  customer_id BIGINT NOT NULL,
  email STRING MASK ${catalog}.security.mask_email
);
