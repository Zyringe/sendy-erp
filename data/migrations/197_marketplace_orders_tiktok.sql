-- 197 · marketplace_orders accepts 'tiktok'
--
-- TikTok orders get imported and shown on /marketplace
-- (projects/tiktok-order-import/tiktok-order-import-plan.md, PR-1). Mig 140
-- widened the three listing tables and left this one out on purpose; this is
-- that follow-up. Only 'tiktok' is added (Put, 2026-09-30: no thaimart yet).
-- A TikTok order never deducts the platform_skus.stock mirror: the engine
-- skips every platform outside bsn_sync.PLATFORM_STOCK_DEDUCT_CUSTOMERS.
--
-- SQLite cannot ALTER a CHECK, so this rebuilds the table (mig 175's shape):
--   * foreign_keys OFF before BEGIN (a no-op inside a transaction).
--     marketplace_order_items.order_id REFERENCES marketplace_orders(id)
--     ON DELETE CASCADE, so with foreign keys ON the DROP below would delete
--     every order line.
--   * explicit column list, id included, so every order keeps its id: the
--     items above and platform_stock_deductions.source_id (a value
--     reference, no FK) both point at it.
--   * sqlite_sequence.seq is put back to its old value. The rebuild would
--     leave it at MAX(id), and a new order could then reuse the id of a
--     deleted one that a deduction row still names. The old value is kept
--     in a temp table and written back as it was (absent stays absent; an
--     emptied table keeps the seq it had).
--   * the 4 indexes come back verbatim (they drop with the old table).
--
-- Re-runnable: every CREATE is preceded by its DROP ... IF EXISTS.
-- Rollback: scripts/rollback_197_marketplace_orders_tiktok.py (a guarded
-- Python script, not SQL; see 197_marketplace_orders_tiktok.rollback.sql).

PRAGMA foreign_keys = OFF;

BEGIN;

DROP TABLE IF EXISTS temp.mig197_seq;
CREATE TEMP TABLE mig197_seq AS
    SELECT seq FROM sqlite_sequence WHERE name = 'marketplace_orders';

DROP TABLE IF EXISTS marketplace_orders_new;
CREATE TABLE marketplace_orders_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT    NOT NULL CHECK(platform IN ('shopee','lazada','tiktok')),
    order_sn         TEXT    NOT NULL,        -- Shopee order_sn / Lazada order number / TikTok Order ID
    status           TEXT,                    -- marketplace order status
    buyer_name       TEXT,
    buyer_phone      TEXT,
    ship_address     TEXT,
    order_date       TEXT,                    -- ISO; order create time
    paid_date        TEXT,                    -- ISO; settlement/payment time (nullable until settled)
    item_total       REAL,                    -- sum of line subtotals, pre-fee
    marketplace_fee  REAL,                    -- หักค่าบริการ (nullable until settled)
    payout           REAL,                    -- ยอดรวมหลังหักค่าคอม (nullable until settled)
    currency         TEXT    NOT NULL DEFAULT 'THB',
    source_file      TEXT,                    -- export filename this row was imported from
    raw_json         TEXT,                    -- full export row(s) for forensics
    first_synced_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_synced_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    actual_payout    REAL,
    settled_at       TEXT,
    settlement_source TEXT,
    payout_batch_id  INTEGER,
    payout_id        INTEGER,
    UNIQUE(platform, order_sn)
);

INSERT INTO marketplace_orders_new
    (id, platform, order_sn, status, buyer_name, buyer_phone, ship_address,
     order_date, paid_date, item_total, marketplace_fee, payout, currency,
     source_file, raw_json, first_synced_at, last_synced_at,
     actual_payout, settled_at, settlement_source, payout_batch_id, payout_id)
SELECT
     id, platform, order_sn, status, buyer_name, buyer_phone, ship_address,
     order_date, paid_date, item_total, marketplace_fee, payout, currency,
     source_file, raw_json, first_synced_at, last_synced_at,
     actual_payout, settled_at, settlement_source, payout_batch_id, payout_id
FROM marketplace_orders;

DROP TABLE marketplace_orders;
ALTER TABLE marketplace_orders_new RENAME TO marketplace_orders;

-- The INSERT above left seq at MAX(id) (0 with no rows): put the old row back as it was.
DELETE FROM sqlite_sequence WHERE name = 'marketplace_orders';
INSERT INTO sqlite_sequence (name, seq)
    SELECT 'marketplace_orders', seq FROM temp.mig197_seq;
DROP TABLE temp.mig197_seq;

DROP INDEX IF EXISTS idx_marketplace_orders_date;
CREATE INDEX idx_marketplace_orders_date
    ON marketplace_orders(platform, order_date DESC);
DROP INDEX IF EXISTS idx_marketplace_orders_status;
CREATE INDEX idx_marketplace_orders_status
    ON marketplace_orders(status);
DROP INDEX IF EXISTS idx_marketplace_orders_payout_batch;
CREATE INDEX idx_marketplace_orders_payout_batch
    ON marketplace_orders(payout_batch_id);
DROP INDEX IF EXISTS idx_marketplace_orders_payout_id;
CREATE INDEX idx_marketplace_orders_payout_id ON marketplace_orders(payout_id);

COMMIT;

PRAGMA foreign_keys = ON;
