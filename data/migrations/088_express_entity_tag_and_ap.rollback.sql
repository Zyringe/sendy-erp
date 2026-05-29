-- ============================================================================
-- Rollback 088 — drop express_ap_outstanding; remove express_ar_outstanding.entity
--
-- Per feedback_rename_migration_safety: the AR rebuild reads the CURRENT table,
-- so any rows INSERTed after mig 088 ran (including BSN imports) are preserved —
-- only the `entity` column is dropped. SQLite versions before 3.35 cannot
-- ALTER TABLE DROP COLUMN, so we use the table-rebuild-preserving-current-rows
-- pattern (the house style for column-add rollbacks).
--
-- Data note: dropping `entity` collapses BSN and SD rows back into one
-- untagged set. If both entities have been imported by the time this runs,
-- they become indistinguishable. That is the inherent cost of reverting the
-- entity split — restore from a pre-088 backup if the distinction matters.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- 1) Drop the new payables table (purely additive — clean drop, no dependents).
DROP TABLE IF EXISTS express_ap_outstanding;

-- 2) Drop the AR entity-scoped index (added by 088).
DROP INDEX IF EXISTS idx_express_ar_entity_snapshot;

-- 3) Rebuild express_ar_outstanding without the `entity` column, preserving
--    CURRENT rows. Schema below = the pre-088 shape (verified 2026-05-29).
DROP TABLE IF EXISTS express_ar_outstanding_old;

-- Schema below = the EXACT pre-088 shape from migration 013, including the
-- original foreign keys (batch_id CASCADE + customer_id) — rebuilding without
-- them would silently drop referential integrity after rollback.
CREATE TABLE express_ar_outstanding_old (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    snapshot_date_iso TEXT    NOT NULL,
    customer_code     TEXT    NOT NULL,
    customer_name     TEXT,
    customer_id       TEXT    REFERENCES customers(code),
    customer_type     TEXT,
    doc_date_iso      TEXT,
    doc_no            TEXT    NOT NULL,
    is_anomalous      INTEGER NOT NULL DEFAULT 0,
    salesperson_code  TEXT,
    bill_amount       REAL    NOT NULL DEFAULT 0,
    paid_amount       REAL    NOT NULL DEFAULT 0,
    outstanding_amount REAL   NOT NULL DEFAULT 0,
    has_warning       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Copy CURRENT rows (drops only the entity column).
INSERT INTO express_ar_outstanding_old
    (id, batch_id, snapshot_date_iso, customer_code, customer_name,
     customer_id, customer_type, doc_date_iso, doc_no, is_anomalous,
     salesperson_code, bill_amount, paid_amount, outstanding_amount,
     has_warning, created_at)
SELECT
     id, batch_id, snapshot_date_iso, customer_code, customer_name,
     customer_id, customer_type, doc_date_iso, doc_no, is_anomalous,
     salesperson_code, bill_amount, paid_amount, outstanding_amount,
     has_warning, created_at
FROM express_ar_outstanding;

DROP TABLE express_ar_outstanding;
ALTER TABLE express_ar_outstanding_old RENAME TO express_ar_outstanding;

-- 4) Recreate the 3 original indexes (pre-088 shape).
CREATE INDEX idx_express_ar_snapshot ON express_ar_outstanding(snapshot_date_iso, customer_code);
CREATE INDEX idx_express_ar_customer ON express_ar_outstanding(customer_id);
CREATE INDEX idx_express_ar_doc      ON express_ar_outstanding(doc_no);

-- 5) Remove the applied_migrations record so the runner re-applies 088 if replayed.
DELETE FROM applied_migrations WHERE filename = '088_express_entity_tag_and_ap.sql';

COMMIT;

PRAGMA foreign_keys = ON;
