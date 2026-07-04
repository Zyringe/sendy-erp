-- data/migrations/127_product_labels.sql
--
-- Phase 1 of ป้ายสินค้า (product-label printing to GoDEX), projects/product-label-printing/
-- plan.md. Two tables:
--
--   product_labels       — the label master (1 row per label). Standalone, NOT tied to
--                           `products` (decision D1 — no reliable key back to a product;
--                           a future `product_id` link will be matched by barcode).
--   label_company_block  — single-row config for the constant boilerplate printed on
--                           every label (distributor/importer/address/quality/price line).
--
-- Apply: drop this file + its .rollback.sql into data/migrations/, restart sendy — the
-- migration runner (database.py::init_db()) auto-applies on boot.
-- Rollback: run 127_product_labels.rollback.sql manually.

BEGIN;

CREATE TABLE product_labels (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode        TEXT,
    product_name   TEXT NOT NULL,
    brand          TEXT,
    usage_th       TEXT,
    warning_th     TEXT,
    packaging_th   TEXT,
    size_th        TEXT,
    label_size     TEXT NOT NULL DEFAULT 'big' CHECK(label_size IN ('small', 'big')),
    legacy_no      TEXT,
    needs_review   INTEGER NOT NULL DEFAULT 0,
    review_note    TEXT,
    product_id     INTEGER NULL REFERENCES products(id) ON DELETE SET NULL,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_product_labels_barcode      ON product_labels(barcode);
CREATE INDEX idx_product_labels_brand        ON product_labels(brand);
CREATE INDEX idx_product_labels_needs_review ON product_labels(needs_review);
CREATE INDEX idx_product_labels_product_id   ON product_labels(product_id);

CREATE TABLE label_company_block (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    distributor_th     TEXT,
    importer_th        TEXT,
    address_th         TEXT,
    importer_addr1_th  TEXT,
    importer_addr2_th  TEXT,
    country_th         TEXT,
    quality_th         TEXT,
    price_line_th      TEXT DEFAULT 'ราคา : ตรวจสอบ ณ จุดขาย',
    updated_at         TEXT DEFAULT (datetime('now'))
);

COMMIT;
