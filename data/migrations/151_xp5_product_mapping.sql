-- 151: cross-book product mapping — xp5 (VAT company file) STKCOD → main
-- products.id. Written by the vat-book mapping pipeline (auto layers) and the
-- review-sheet apply; read by the VAT-view product badge. The cross-book key
-- is the xp5 CODE (TEXT PK) — never a product id (the two books' id
-- namespaces are unrelated). vat_book.db is regenerable, so this durable
-- knowledge lives in the MAIN DB (plan rev 3, decision #5).
CREATE TABLE xp5_product_mapping (
    xp5_code       TEXT PRIMARY KEY,
    product_id     INTEGER REFERENCES products(id),
    xp5_name       TEXT    NOT NULL DEFAULT '',
    match_layer    TEXT    NOT NULL DEFAULT 'manual'
                   CHECK (match_layer IN ('code+name', 'dualkey', 'name', 'manual')),
    status         TEXT    NOT NULL DEFAULT 'auto'
                   CHECK (status IN ('auto', 'reviewed', 'ignored')),
    evidence_count INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_xp5_mapping_product ON xp5_product_mapping(product_id);
