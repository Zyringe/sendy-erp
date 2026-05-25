-- ============================================================================
-- Migration 082 — paid_invoices: rename iv_no → doc_no, add doc_kind
--
-- Background
--   `paid_invoices.iv_no` is misleadingly named. The column accepts BOTH:
--     - IV refs (~7,688 rows, +฿22.88M)  — receipt → invoice settlement
--     - SR refs (~143 rows, -฿277k)      — receipt → credit-note application
--   See [[project_2026_05_21_paid_invoices_sr_load_bearing]]: SR rows are
--   load-bearing for the `collected = ΣIV(+) − ΣSR(−)` math in
--   payments_alloc.get_payment_summary_v2. Dropping them caused the
--   ~฿446k phantom-overpay bug pre-mig-062.
--
--   The name `iv_no` makes naive audits ("orphan iv_no") flag SR rows as
--   junk. The fix used today is pattern-matching by string prefix:
--   `WHERE pi.iv_no LIKE 'SR%'` / `NOT LIKE 'SR%'`. This works but the
--   column name remains a permanent landmine — every audit author has to
--   re-learn "iv_no isn't really iv_no".
--
-- What this migration does
--   1. Rename `iv_no` → `doc_no` (the column holds doc numbers, not just IVs)
--   2. Add `doc_kind TEXT NOT NULL CHECK(doc_kind IN ('IV','SR'))` —
--      explicit enum replacing the implicit-prefix pattern
--   3. Backfill `doc_kind` from prefix: `CASE WHEN LIKE 'SR%' THEN 'SR' ELSE 'IV' END`
--   4. Recreate index + 3 audit triggers (mig 076) with new column names
--
--   SQLite ALTER COLUMN is limited, so this is a table-rebuild. Per
--   `sendy_erp/CLAUDE.md`: drop+recreate dependent triggers/views.
--
-- Why now
--   PR #66 (mig 081) closed one landmine (pid 771 ledger). This closes the
--   other one Put surfaced — `iv_no`'s misleading name. Both are documented
--   in [[project_2026_05_23_data_quality_077_078]] and [[project_2026_05_21_paid_invoices_sr_load_bearing]].
--
-- Backfill verification (pre-deploy survey, 2026-05-25)
--   7,688 IV + 143 SR + 0 NULL/other = 7,831 rows. The CHECK constraint will
--   block any future row that isn't IV/SR — if a new doc kind appears, fix
--   the migration before applying (forward-only schema discipline).
--
-- Rollback
--   Reads from the CURRENT `paid_invoices` (not the snapshot) so any rows
--   INSERTed after the forward mig are preserved through the rollback. The
--   `migration_082_snapshot` is kept as a forensic audit artifact only.
--   Safe because mig 082 is a pure column rename — `doc_no` values are
--   byte-for-byte identical to what `iv_no` held pre-mig.
-- ============================================================================

BEGIN;

-- ── 1. Snapshot original table for rollback ────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_082_snapshot AS
SELECT id, re_id, iv_no, amount FROM paid_invoices;

-- ── 2. Drop old audit triggers (mig 076) + index ───────────────────────────
DROP TRIGGER IF EXISTS audit_paid_invoices_insert;
DROP TRIGGER IF EXISTS audit_paid_invoices_update;
DROP TRIGGER IF EXISTS audit_paid_invoices_delete;
DROP INDEX  IF EXISTS idx_pi_iv_no;

-- ── 3. Build new table with renamed/added columns ──────────────────────────
CREATE TABLE paid_invoices_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    re_id      INTEGER NOT NULL REFERENCES received_payments(id),
    doc_no     TEXT    NOT NULL,
    doc_kind   TEXT    NOT NULL CHECK(doc_kind IN ('IV','SR')),
    amount     REAL,
    UNIQUE(re_id, doc_no)
);

-- ── 4. Copy data with doc_kind backfill from prefix ────────────────────────
INSERT INTO paid_invoices_new (id, re_id, doc_no, doc_kind, amount)
SELECT id,
       re_id,
       iv_no,
       CASE WHEN iv_no LIKE 'SR%' THEN 'SR' ELSE 'IV' END,
       amount
FROM paid_invoices;

-- ── 5. Swap tables ─────────────────────────────────────────────────────────
DROP TABLE paid_invoices;
ALTER TABLE paid_invoices_new RENAME TO paid_invoices;

-- ── 6. Recreate index on the renamed column ────────────────────────────────
CREATE INDEX idx_pi_doc_no ON paid_invoices(doc_no);

-- ── 7. Recreate audit triggers with new column names ───────────────────────
-- Mirrors mig 076 structure: INSERT (full payload), UPDATE (diff-only via
-- WHEN clause + json_group_object), DELETE (BEFORE so OLD is queryable).
-- All 3 columns now in payload: re_id, doc_no, doc_kind, amount.
CREATE TRIGGER audit_paid_invoices_insert
AFTER INSERT ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', NEW.id, 'INSERT',
        json_object(
            're_id',    NEW.re_id,
            'doc_no',   NEW.doc_no,
            'doc_kind', NEW.doc_kind,
            'amount',   NEW.amount
        )
    );
END;

CREATE TRIGGER audit_paid_invoices_update
AFTER UPDATE ON paid_invoices
WHEN (
       OLD.re_id    IS NOT NEW.re_id
    OR OLD.doc_no   IS NOT NEW.doc_no
    OR OLD.doc_kind IS NOT NEW.doc_kind
    OR OLD.amount   IS NOT NEW.amount
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'paid_invoices', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 're_id'    AS field, OLD.re_id    AS old_v, NEW.re_id    AS new_v WHERE OLD.re_id    IS NOT NEW.re_id
        UNION ALL SELECT 'doc_no',   OLD.doc_no,   NEW.doc_no   WHERE OLD.doc_no   IS NOT NEW.doc_no
        UNION ALL SELECT 'doc_kind', OLD.doc_kind, NEW.doc_kind WHERE OLD.doc_kind IS NOT NEW.doc_kind
        UNION ALL SELECT 'amount',   OLD.amount,   NEW.amount   WHERE OLD.amount   IS NOT NEW.amount
    );
END;

CREATE TRIGGER audit_paid_invoices_delete
BEFORE DELETE ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', OLD.id, 'DELETE',
        json_object(
            're_id',    OLD.re_id,
            'doc_no',   OLD.doc_no,
            'doc_kind', OLD.doc_kind,
            'amount',   OLD.amount
        )
    );
END;

COMMIT;
