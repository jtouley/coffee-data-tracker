-- warehouse: postgres 16, schema "sales"
CREATE TABLE sales.customers (
    customer_id   BIGINT       PRIMARY KEY,
    email         VARCHAR(320) NOT NULL UNIQUE,
    country_code  CHAR(2)      NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE sales.orders (
    order_id      UUID          PRIMARY KEY,
    customer_id   BIGINT        NOT NULL REFERENCES sales.customers(customer_id),
    status        VARCHAR(16)   NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled')),
    total_amount  NUMERIC(12,2) NOT NULL CHECK (total_amount >= 0),
    currency      CHAR(3)       NOT NULL,
    line_items    JSONB,
    placed_at     TIMESTAMPTZ   NOT NULL,
    shipped_at    TIMESTAMPTZ
);
