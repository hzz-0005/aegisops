CREATE SCHEMA lab;

CREATE TABLE lab.inventory_items (
    sku varchar(80) PRIMARY KEY,
    available integer NOT NULL CONSTRAINT ck_inventory_nonnegative CHECK (available >= 0),
    unit_price_cents integer NOT NULL CONSTRAINT ck_inventory_price_positive
        CHECK (unit_price_cents > 0),
    currency varchar(3) NOT NULL
);

CREATE TABLE lab.payments (
    id uuid PRIMARY KEY,
    order_id uuid NOT NULL UNIQUE,
    amount_cents integer NOT NULL CONSTRAINT ck_payment_positive CHECK (amount_cents > 0),
    currency varchar(3) NOT NULL,
    status varchar(20) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE lab.orders (
    id uuid PRIMARY KEY,
    sku varchar(80) NOT NULL REFERENCES lab.inventory_items(sku),
    quantity integer NOT NULL CONSTRAINT ck_order_quantity_positive CHECK (quantity > 0),
    amount_cents integer NOT NULL CONSTRAINT ck_order_amount_positive CHECK (amount_cents > 0),
    currency varchar(3) NOT NULL,
    status varchar(20) NOT NULL,
    payment_id uuid REFERENCES lab.payments(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO lab.inventory_items (sku, available, unit_price_cents, currency)
VALUES ('sku-demo', 100, 100, 'CNY');
