ALTER TABLE lab.orders ADD COLUMN idempotency_key uuid;

-- Existing Lab rows predate the client key; their order IDs are unique stable keys.
UPDATE lab.orders SET idempotency_key = id WHERE idempotency_key IS NULL;

ALTER TABLE lab.orders ALTER COLUMN idempotency_key SET NOT NULL;
ALTER TABLE lab.orders ADD CONSTRAINT uq_lab_orders_idempotency_key UNIQUE (idempotency_key);
