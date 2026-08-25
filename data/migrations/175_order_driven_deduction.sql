-- 175 · order-driven deduction: widen provenance sources + per-listing baseline
--
-- Reuses the mig-172 provenance table for the new order-import deduction path
-- (projects/order-driven-platform-deduction/plan.md): a second source_table
-- value, 'marketplace_orders', lets a marketplace order file's diff engine
-- record what it actually applied per listing, the same way the BSN walk
-- already does for 'sales_transactions'. SQLite cannot ALTER a CHECK, so
-- widening it is a table rebuild (rename-safety rule 1 — rebuild from the
-- CURRENT table so rows written since mig 172 shipped survive).
--
-- `platform_skus.stock_as_of` is the new per-listing baseline the diff engine
-- gates on: an order line only deducts if `order_date > stock_as_of` of the
-- listing it resolves to, because the platform's own stock snapshot already
-- reflects every order dated before it was taken.
--
-- ⚠ Cutover hole (Codex, 2026-08-25, HIGH): the OLD walk keeps deducting
-- between mig 172 (live since 2026-08-25) and this migration, writing
-- 'sales_transactions' provenance with a real created_at. A listing whose
-- last snapshot predates such a deduction would get stock_as_of OLDER than a
-- sale already deducted — the first order import would deduct it AGAIN. So
-- the baseline anchors to the LATEST of the snapshot and any existing
-- deduction, in two steps: SQLite's scalar MAX() returns NULL if ANY
-- argument is NULL, so a one-shot MAX(imported_at, subquery) would silently
-- null every row with no provenance.
--
-- Drop-first so a hand-applied rehearsal can be re-run (see
-- .claude/rules/erp-engineering-discipline.md).
--
-- Transaction wrapper (Codex/review, 2026-08-25, CRITICAL — the brief's
-- verbatim SQL lacked this): executescript() autocommits DDL statement-by-
-- statement with no explicit BEGIN. Without a wrapper, a crash between the
-- ADD COLUMN and the backfill UPDATE leaves stock_as_of durably added with
-- no applied_migrations row — every next boot retries the whole script and
-- dies forever on "duplicate column name". BEGIN makes the whole migration
-- atomic: a mid-script failure rolls back to nothing-applied, so a retry
-- (with the file intact) just runs clean. foreign_keys=OFF BEFORE BEGIN
-- (a no-op once inside a txn) mirrors mig 140's recipe for the same shape
-- of rebuild — platform_stock_deductions itself carries an FK to
-- platform_skus(id), so drop+recreate follows the same safe pattern even
-- though nothing currently references platform_stock_deductions back.

PRAGMA foreign_keys = OFF;

BEGIN;

DROP TABLE IF EXISTS platform_stock_deductions_new;
CREATE TABLE platform_stock_deductions_new (
    source_table    TEXT    NOT NULL CHECK(source_table IN ('sales_transactions','marketplace_orders')),
    source_id       INTEGER NOT NULL,
    platform_sku_id INTEGER NOT NULL REFERENCES platform_skus(id),
    units           INTEGER NOT NULL CHECK(units <> 0),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (source_table, source_id, platform_sku_id)
);
INSERT INTO platform_stock_deductions_new SELECT * FROM platform_stock_deductions;
DROP TABLE platform_stock_deductions;
ALTER TABLE platform_stock_deductions_new RENAME TO platform_stock_deductions;
DROP INDEX IF EXISTS idx_platform_stock_deductions_sku;
CREATE INDEX idx_platform_stock_deductions_sku ON platform_stock_deductions (platform_sku_id);

ALTER TABLE platform_skus ADD COLUMN stock_as_of TEXT;
UPDATE platform_skus SET stock_as_of = imported_at;

-- Step 2 (Codex-HIGH fix): pull the baseline forward to the latest deduction
-- already recorded against a listing, for the listings that have one AND
-- whose deduction is newer than the snapshot just used above.
UPDATE platform_skus SET stock_as_of = (
    SELECT MAX(created_at) FROM platform_stock_deductions d
    WHERE d.platform_sku_id = platform_skus.id
)
WHERE id IN (
    SELECT platform_sku_id FROM platform_stock_deductions d
    GROUP BY platform_sku_id
    HAVING MAX(d.created_at) > COALESCE(
        (SELECT ps.stock_as_of FROM platform_skus ps WHERE ps.id = d.platform_sku_id), '')
);

COMMIT;

PRAGMA foreign_keys = ON;
