-- 134_product_generic_standins.sql
-- Curated equivalence: some color/size-specific SKUs are tracked as separate
-- products (own stock, own family_id) but the team books their marketplace
-- sales in Express under ONE generic catch-all product instead of the
-- specific variant. marketplace_match.py needs to treat "order resolves to
-- variant X" as ALSO compatible with "IV booked under generic G" — without
-- ever replacing X anywhere else (stock, mapping, unit_conversions are all
-- untouched by this table; see invariant below).
--
-- Design: Operations/05_analysis-reports/engineering/
--         generic-standin-schema-design_2026-07-10.md
-- Put approved in full 2026-07-10 (3 pairs as-is, reuse 'confident' on the
-- matcher side, SQL-only curation, no returns-pool coverage for now).
--
-- Per-product-id (not per-family): family 458 ("ลูกรีเวท DOME") mixes two
-- sizes — 4-4 (pid 456/457/458/459, individually well-tracked, 62-175 sales
-- each) and 4-6 (pid 982/983/2016/2017, ~0 sales, real stock). Only the 4-6
-- subset needs a stand-in; a family-level column would incorrectly also
-- apply to the healthy 4-4 siblings. Verified against the live DB 2026-07-10.
--
-- INVARIANT (do not violate in future changes): this table is consulted ONLY
-- by inventory_app/marketplace_match.py (order<->IV linking). It must NEVER
-- be joined into stock-deduction paths (_sync_bsn_to_stock, product_code_
-- mapping resolution, unit_conversions) — those already correctly deduct
-- stock against whichever pid Express actually booked; nothing to fix there.
--
-- Seed (18 rows, curated 2026-07-10, all verified against live sales_
-- transactions/stock_levels):
--   908 "เฉพาะหัวสายชำระ Sendai"      <- 519,520,521,522,523,524,525 (family 441, full family)
--   907 "เฉพาะหัวฝักบัว Sendai"        <- 512,513,514,515,516,517,518 (family 443, full family)
--   848 "ลูกรีเวท Sendai 4-6"          <- 982,983,2016,2017          (family 458, 4-6 subset only)
--
-- Apply:    sqlite3 .../inventory.db < data/migrations/134_product_generic_standins.sql
-- Rollback: data/migrations/134_product_generic_standins.rollback.sql

BEGIN;

CREATE TABLE product_generic_standins (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    generic_product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    note                TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(variant_product_id, generic_product_id),
    CHECK (variant_product_id <> generic_product_id)
);

CREATE INDEX idx_generic_standins_variant ON product_generic_standins(variant_product_id);
CREATE INDEX idx_generic_standins_generic ON product_generic_standins(generic_product_id);

-- Audit triggers (pattern mirrors 025_product_families.sql — curated
-- master/edit-prone lookup table, worth a change trail). No UPDATE trigger:
-- rows are curated by insert/delete only, never edited in place.
CREATE TRIGGER audit_generic_standins_insert
AFTER INSERT ON product_generic_standins
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_generic_standins', NEW.id, 'INSERT',
        json_object('variant_product_id', NEW.variant_product_id,
                     'generic_product_id', NEW.generic_product_id,
                     'note', NEW.note));
END;

CREATE TRIGGER audit_generic_standins_delete
BEFORE DELETE ON product_generic_standins
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('product_generic_standins', OLD.id, 'DELETE',
        json_object('variant_product_id', OLD.variant_product_id,
                     'generic_product_id', OLD.generic_product_id));
END;

-- Seed: guarded with WHERE EXISTS so a from-empty build (no products seeded)
-- no-ops these inserts instead of raising FOREIGN KEY constraint failed
-- (same pattern as migs 014/018 — see tests/test_fresh_db_build.py).
INSERT INTO product_generic_standins (variant_product_id, generic_product_id, note)
SELECT v, 908, 'curated 2026-07-10: family 441 เฉพาะหัวสายชำระ color variants -> generic Sendai pid'
FROM (SELECT 519 AS v UNION ALL SELECT 520 UNION ALL SELECT 521 UNION ALL SELECT 522
      UNION ALL SELECT 523 UNION ALL SELECT 524 UNION ALL SELECT 525)
WHERE EXISTS (SELECT 1 FROM products WHERE id = v)
  AND EXISTS (SELECT 1 FROM products WHERE id = 908);

INSERT INTO product_generic_standins (variant_product_id, generic_product_id, note)
SELECT v, 907, 'curated 2026-07-10: family 443 เฉพาะหัวฝักบัว color variants -> generic Sendai pid'
FROM (SELECT 512 AS v UNION ALL SELECT 513 UNION ALL SELECT 514 UNION ALL SELECT 515
      UNION ALL SELECT 516 UNION ALL SELECT 517 UNION ALL SELECT 518)
WHERE EXISTS (SELECT 1 FROM products WHERE id = v)
  AND EXISTS (SELECT 1 FROM products WHERE id = 907);

INSERT INTO product_generic_standins (variant_product_id, generic_product_id, note)
SELECT v, 848, 'curated 2026-07-10: family 458 ลูกรีเวท DOME 4-6-size subset -> generic Sendai pid (4-4 siblings excluded, already tracked directly)'
FROM (SELECT 982 AS v UNION ALL SELECT 983 UNION ALL SELECT 2016 UNION ALL SELECT 2017)
WHERE EXISTS (SELECT 1 FROM products WHERE id = v)
  AND EXISTS (SELECT 1 FROM products WHERE id = 848);

COMMIT;
