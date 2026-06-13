-- Rollback 100 — drop the 3 settlement columns from marketplace_orders.
--
-- SQLite (this deploy targets <3.35) cannot ALTER TABLE DROP COLUMN, so we use
-- the table-rebuild-preserving-current-rows pattern (house style — see mig 088).
--
-- Two hazards this rollback must avoid (both verified against a live-DB backup):
--   1. marketplace_order_items.order_id REFERENCES marketplace_orders(id) ON
--      DELETE CASCADE. get_connection() runs with foreign_keys = ON, so a naive
--      `DROP TABLE marketplace_orders` cascade-deletes every child item row.
--      `PRAGMA foreign_keys = OFF` (before BEGIN; no-op inside a txn) prevents it.
--   2. `CREATE TABLE ... AS SELECT` copies rows only — it loses the PK
--      AUTOINCREMENT, NOT NULL/CHECK/DEFAULT/UNIQUE constraints AND the two
--      indexes, and leaves marketplace_order_items' FK pointing at a table whose
--      id is no longer a primary key (foreign_key_check mismatch). So we write
--      the EXACT pre-100 schema from mig 093, INSERT...SELECT, then recreate the
--      indexes.

PRAGMA foreign_keys = OFF;

BEGIN;

-- 1) Drop the two indexes (recreated below after the rebuild).
DROP INDEX IF EXISTS idx_marketplace_orders_status;
DROP INDEX IF EXISTS idx_marketplace_orders_date;

-- 2) Rebuild marketplace_orders without the 3 settlement columns, preserving
--    CURRENT rows. Schema below = the EXACT pre-100 shape from migration 093.
DROP TABLE IF EXISTS marketplace_orders_old;

CREATE TABLE marketplace_orders_old (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    order_sn         TEXT    NOT NULL,
    status           TEXT,
    buyer_name       TEXT,
    buyer_phone      TEXT,
    ship_address     TEXT,
    order_date       TEXT,
    paid_date        TEXT,
    item_total       REAL,
    marketplace_fee  REAL,
    payout           REAL,
    currency         TEXT    NOT NULL DEFAULT 'THB',
    source_file      TEXT,
    raw_json         TEXT,
    first_synced_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_synced_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, order_sn)
);

INSERT INTO marketplace_orders_old
    (id, platform, order_sn, status, buyer_name, buyer_phone,
     ship_address, order_date, paid_date, item_total, marketplace_fee,
     payout, currency, source_file, raw_json, first_synced_at, last_synced_at)
SELECT
     id, platform, order_sn, status, buyer_name, buyer_phone,
     ship_address, order_date, paid_date, item_total, marketplace_fee,
     payout, currency, source_file, raw_json, first_synced_at, last_synced_at
FROM marketplace_orders;

DROP TABLE marketplace_orders;
ALTER TABLE marketplace_orders_old RENAME TO marketplace_orders;

-- 3) Recreate the two indexes (mig 093 shape).
CREATE INDEX idx_marketplace_orders_date
    ON marketplace_orders(platform, order_date DESC);
CREATE INDEX idx_marketplace_orders_status
    ON marketplace_orders(status);

-- 4) Remove the applied_migrations record so the runner re-applies it if replayed.
DELETE FROM applied_migrations WHERE filename = '100_marketplace_settlement.sql';

COMMIT;

PRAGMA foreign_keys = ON;
