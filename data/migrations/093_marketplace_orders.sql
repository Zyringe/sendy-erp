-- Migration 093 — marketplace order capture (Shopee/Lazada).
--
-- Stores orders imported from the Shopee/Lazada Seller Center ORDER-EXPORT files
-- (uploaded into Sendy) so the team can stop hand-keying the Google tracking
-- sheet. These tables are OPERATIONAL tracking only and are deliberately kept
-- SEPARATE from sales_transactions: marketplace sales already enter the
-- accounting ledger via the weekly Express import (customers หน้าร้านS /
-- หน้าร้านB / หน้าร้านL), so writing these orders into sales_transactions would
-- double-count revenue. No stock mutation, no Express write — both stay as-is.
--
-- (OAuth/API token storage is intentionally NOT created here: v1 ingests via
-- file upload, which needs no Shopee Key Account Manager and no API approval.
-- A marketplace_auth table will be added in the migration that ships the
-- Lazada API auto-pull, if/when that path is built.)
--
-- Purely additive (2 new tables + indexes). Nothing existing is touched.

BEGIN;

-- One row per marketplace order (header).
CREATE TABLE IF NOT EXISTS marketplace_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    order_sn         TEXT    NOT NULL,        -- Shopee order_sn / Lazada order number
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
    UNIQUE(platform, order_sn)
);
CREATE INDEX IF NOT EXISTS idx_marketplace_orders_date
    ON marketplace_orders(platform, order_date DESC);
CREATE INDEX IF NOT EXISTS idx_marketplace_orders_status
    ON marketplace_orders(status);

-- One row per order line. internal_product_id resolved via platform_skus:
-- Shopee order exports carry no SKU ref/variation_id, so resolution matches
-- (product_name, variation_name); Lazada can match variation_id. NULL
-- internal_product_id = needs mapping, surfaced on /marketplace/unmapped.
-- line_key is a deterministic per-line key (product+variation) so re-importing
-- the same order upserts, never dups.
CREATE TABLE IF NOT EXISTS marketplace_order_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL REFERENCES marketplace_orders(id) ON DELETE CASCADE,
    platform            TEXT    NOT NULL,
    order_sn            TEXT    NOT NULL,
    line_key            TEXT    NOT NULL,
    seller_sku          TEXT,
    variation_id        TEXT,
    item_name           TEXT,
    variation_name      TEXT,
    internal_product_id INTEGER REFERENCES products(id),
    qty                 REAL    NOT NULL DEFAULT 0,
    unit_price          REAL,
    item_subtotal       REAL,
    raw_json            TEXT,
    UNIQUE(platform, order_sn, line_key)
);
CREATE INDEX IF NOT EXISTS idx_marketplace_items_order
    ON marketplace_order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_marketplace_items_unmapped
    ON marketplace_order_items(internal_product_id) WHERE internal_product_id IS NULL;

COMMIT;
