-- 002_supplier_catalogue.sql
-- Phase 1 of supplier-catalogue intelligence project.
-- Adds tables to track supplier catalogues, their items over time, price
-- history, and the manual mapping between catalogue items and ERP products
-- (or directly to purchased product_name_raw values).
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/002_supplier_catalogue.sql
--
-- Verify:
--   sqlite3 .../inventory.db ".schema suppliers"
--   sqlite3 .../inventory.db ".tables" | tr ' ' '\n' | grep '^supplier_'
--
-- Rollback: 002_supplier_catalogue.rollback.sql
--
-- Design notes (POC validated 2026-04-29):
--   * Auto-matching catalogue → purchases yields <2% — manual mapping UI is
--     critical path. Schema therefore favors mapping flexibility over auto-link.
--   * Catalogue items may exist that are NEVER purchased (most rows). Conversely,
--     frequently-purchased SKUs (e.g. BROVO-101) may NOT be in the catalogue.
--     Both directions are valid; mapping is many-to-many in concept but stored
--     as one mapping row per (catalogue_item, target).
--   * Font color from openpyxl is reliable — store as price_change_flag enum.

BEGIN;

-- ──────────────────────────────────────────────────────────────────────────
-- suppliers: master list. Starts with one row (ศรีไทยเจริญโลหะกิจ) but
-- generic so additional suppliers (e.g. Thai brand distributors) can be added.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS suppliers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,   -- exact name as it appears in purchase_transactions.supplier
    display_name    TEXT,                       -- optional friendlier label
    contact_info    TEXT,                       -- free-form (phone, line, address)
    note            TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────────────────────
-- supplier_catalogue_versions: each imported price-list file is a version.
-- Catalogue date (e.g. "2-69" = ก.พ. 2569 = Feb 2026 CE) recorded as catalogue_date.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS supplier_catalogue_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    source_file     TEXT    NOT NULL,           -- original filename
    catalogue_date  TEXT,                        -- ISO YYYY-MM (the month the catalogue covers)
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    imported_by     TEXT,                        -- session username
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_catalogue_versions_supplier
    ON supplier_catalogue_versions(supplier_id, catalogue_date);

-- ──────────────────────────────────────────────────────────────────────────
-- supplier_catalogue_items: distinct line items aggregated across versions.
-- One row per (supplier, normalized name) — the name_normalized acts as the
-- stable identity since this supplier has no SKU codes.
-- Latest-known fields (unit, list_price, etc.) are mirrored here for fast
-- read; full history lives in supplier_catalogue_price_history.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS supplier_catalogue_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    name_raw            TEXT    NOT NULL,           -- as it appeared in catalogue (latest seen)
    name_normalized     TEXT    NOT NULL,           -- stable identity within supplier
    name_tokens         TEXT,                        -- JSON array of tokens, for fuzzy search
    category_hint       TEXT,                        -- sub-category from **markers** (latest seen)
    sheet_name          TEXT,                        -- which Thai-consonant sheet (latest seen)
    unit                TEXT,                        -- e.g. ตัว / โหล / แผ่น
    min_order_qty       REAL,
    list_price          REAL,                        -- latest list price (THB)
    trade_discount_pct  REAL,                        -- e.g. 25.0 means 25%
    cash_discount_pct   REAL,                        -- e.g. 5.0 means 5%
    net_cash_price      REAL,                        -- list_price * (1 - trade) * (1 - cash), latest
    price_change_flag   TEXT CHECK(price_change_flag IN ('same','changed','new','preorder','unknown')),
    first_seen_version_id INTEGER REFERENCES supplier_catalogue_versions(id),
    last_seen_version_id  INTEGER REFERENCES supplier_catalogue_versions(id),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(supplier_id, name_normalized)
);

CREATE INDEX IF NOT EXISTS idx_catalogue_items_supplier_active
    ON supplier_catalogue_items(supplier_id, is_active);
CREATE INDEX IF NOT EXISTS idx_catalogue_items_name_norm
    ON supplier_catalogue_items(name_normalized);

-- ──────────────────────────────────────────────────────────────────────────
-- supplier_catalogue_price_history: append-only price log per item per version.
-- Lets us answer "did this item's price change between Feb and Mar?" directly,
-- and survives if font-color reading later becomes unreliable.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS supplier_catalogue_price_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER NOT NULL REFERENCES supplier_catalogue_items(id) ON DELETE CASCADE,
    version_id          INTEGER NOT NULL REFERENCES supplier_catalogue_versions(id) ON DELETE CASCADE,
    list_price          REAL,
    trade_discount_pct  REAL,
    cash_discount_pct   REAL,
    net_cash_price      REAL,
    unit                TEXT,
    price_change_flag   TEXT CHECK(price_change_flag IN ('same','changed','new','preorder','unknown')),
    captured_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(item_id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_catalogue_price_history_item
    ON supplier_catalogue_price_history(item_id, version_id);

-- ──────────────────────────────────────────────────────────────────────────
-- supplier_quick_updates: ad-hoc price changes received via text/line/phone
-- between catalogue versions. Not tied to a version_id; standalone signal.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS supplier_quick_updates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    item_id             INTEGER REFERENCES supplier_catalogue_items(id) ON DELETE SET NULL,
    name_raw            TEXT    NOT NULL,           -- in case item not yet in catalogue
    new_list_price      REAL,
    new_net_cash_price  REAL,
    effective_date      TEXT,                        -- ISO date when change takes effect
    source              TEXT,                        -- 'line', 'phone', 'sms', etc.
    note                TEXT,
    captured_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    captured_by         TEXT
);

CREATE INDEX IF NOT EXISTS idx_quick_updates_supplier
    ON supplier_quick_updates(supplier_id, effective_date);

-- ──────────────────────────────────────────────────────────────────────────
-- supplier_product_mapping: manual link between a catalogue item and either:
--   (a) an ERP product (products.id), OR
--   (b) a purchased name_raw that has no products row yet (purchase_name_raw)
-- One catalogue_item maps to at most one target. Reverse: a product can have
-- multiple catalogue items mapped to it (different supplier sizes/variants).
-- supplier_unit/erp_unit/ratio bridge the two unit systems (catalogue full
-- words like "โหล" vs ERP unit_type like "ตัว").
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS supplier_product_mapping (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    catalogue_item_id   INTEGER REFERENCES supplier_catalogue_items(id) ON DELETE CASCADE,
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,
    purchase_name_raw   TEXT,                        -- when mapping a purchased item that has no catalogue entry
    supplier_unit       TEXT,
    erp_unit            TEXT,
    ratio               REAL DEFAULT 1.0,            -- supplier_qty * ratio = erp_qty
    is_ignored          INTEGER NOT NULL DEFAULT 0 CHECK(is_ignored IN (0,1)),
    confidence          TEXT CHECK(confidence IN ('manual','suggested','imported')),
    note                TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    -- Either catalogue_item_id or purchase_name_raw must be set (the source side).
    CHECK(catalogue_item_id IS NOT NULL OR purchase_name_raw IS NOT NULL),
    -- Mapping must have at least one target OR be explicitly ignored.
    -- Targets: catalogue_item_id (purchase→catalogue) OR product_id (catalogue→ERP product).
    -- Both can be set (purchase→catalogue→ERP product chain).
    CHECK(catalogue_item_id IS NOT NULL OR product_id IS NOT NULL OR is_ignored = 1),
    UNIQUE(supplier_id, catalogue_item_id, purchase_name_raw)
);

CREATE INDEX IF NOT EXISTS idx_supplier_mapping_product
    ON supplier_product_mapping(product_id);
CREATE INDEX IF NOT EXISTS idx_supplier_mapping_catalogue
    ON supplier_product_mapping(catalogue_item_id);
CREATE INDEX IF NOT EXISTS idx_supplier_mapping_purchase
    ON supplier_product_mapping(supplier_id, purchase_name_raw);

-- ──────────────────────────────────────────────────────────────────────────
-- Seed: insert the one supplier we know about. Name must match the value
-- already used in purchase_transactions.supplier so joins line up.
-- ──────────────────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO suppliers (name, display_name)
VALUES ('ศรีไทยเจริญโลหะกิจ', 'ศรีไทยเจริญโลหะกิจ');

COMMIT;
