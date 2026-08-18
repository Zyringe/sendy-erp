-- 164_express_sales_orders.sql
-- ใบสั่งขาย from Express OESO (headers) + OESOIT (lines), carried by the daily
-- zip since 2026-08-17.
--
-- What it is: customer demand that has been ordered but not yet invoiced. The
-- ledger only ever sees a sale once it becomes an IV, so an order sitting
-- unfulfilled is invisible to Sendy today.
--
-- ⚠ Read the numbers before building anything on this. 1,732 of the 9,333
-- orders carry a remaining quantity, but only 20 of those are dated 2026
-- (฿112,464 of demand). The other 1,712 run back to 2003 and are orders nobody
-- ever closed out — stale rows, not backlog. Any "open orders" view MUST filter
-- by date or it will report ฿13.98M of demand that does not exist.
--
-- DOCSTAT (M 7,545 / N 1,731 / C 57) is stored verbatim. As with BKTRN.CHQSTAT,
-- the letters are not decodable from the data alone, and guessing them into a
-- business meaning is how a wrong label becomes permanent.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/164_express_sales_orders.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

CREATE TABLE IF NOT EXISTS express_sales_orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entity         TEXT    NOT NULL,
    so_no          TEXT    NOT NULL,            -- SONUM (unique across all 9,333)
    so_date_iso    TEXT,
    customer_code  TEXT,
    customer_name  TEXT,
    salesperson_code TEXT,
    your_ref       TEXT,
    pay_terms      INTEGER,
    delivery_date_iso TEXT,
    completed_date_iso TEXT,
    total          REAL    NOT NULL DEFAULT 0,
    discount_amount REAL   NOT NULL DEFAULT 0,
    vat_amount     REAL    NOT NULL DEFAULT 0,
    net_amount     REAL    NOT NULL DEFAULT 0,
    status_code    TEXT,                        -- DOCSTAT verbatim, uninterpreted
    imported_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity, so_no)
);

CREATE TABLE IF NOT EXISTS express_sales_order_lines (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entity         TEXT    NOT NULL,
    so_no          TEXT    NOT NULL,
    line_seq       INTEGER NOT NULL,
    product_code   TEXT,                        -- STKCOD (Express's own code)
    product_name   TEXT,
    ordered_qty    REAL    NOT NULL DEFAULT 0,
    cancelled_qty  REAL    NOT NULL DEFAULT 0,
    remaining_qty  REAL    NOT NULL DEFAULT 0,  -- REMQTY: what is still owed
    unit           TEXT,
    unit_price     REAL    NOT NULL DEFAULT 0,
    line_total     REAL    NOT NULL DEFAULT 0,
    UNIQUE (entity, so_no, line_seq)
);

CREATE INDEX IF NOT EXISTS idx_sales_orders_date
    ON express_sales_orders(entity, so_date_iso);
CREATE INDEX IF NOT EXISTS idx_sales_order_lines_remaining
    ON express_sales_order_lines(entity, remaining_qty);

COMMIT;
