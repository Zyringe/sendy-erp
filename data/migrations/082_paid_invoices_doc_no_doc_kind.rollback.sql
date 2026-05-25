-- 082_paid_invoices_doc_no_doc_kind.rollback.sql
-- Restores paid_invoices to its pre-mig-082 shape (column rename only —
-- doc_kind dropped, iv_no restored). Run manually; runner does not auto-rollback.
--
-- Rollback strategy: read from CURRENT `paid_invoices` (not from
-- `migration_082_snapshot`), so rows INSERTed after the forward mig was
-- applied are preserved through the rollback. The snapshot is kept as a
-- forensic audit artifact — compare it to the post-rollback table if you
-- need to detect drift between forward-mig time and rollback time.
--
-- Safe because mig 082 was a pure column rename — `doc_no` values byte-for-
-- byte match what `iv_no` held pre-mig, so copying `doc_no → iv_no` loses
-- nothing.

BEGIN;

-- ── 1. Drop the new audit triggers + index ────────────────────────────────
DROP TRIGGER IF EXISTS audit_paid_invoices_insert;
DROP TRIGGER IF EXISTS audit_paid_invoices_update;
DROP TRIGGER IF EXISTS audit_paid_invoices_delete;
DROP INDEX  IF EXISTS idx_pi_doc_no;

-- ── 2. Rebuild original table from CURRENT paid_invoices (not snapshot) ───
-- This preserves any rows that were INSERTed after the forward mig applied.
CREATE TABLE paid_invoices_old (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    re_id      INTEGER NOT NULL REFERENCES received_payments(id),
    iv_no      TEXT    NOT NULL,
    amount     REAL,
    UNIQUE(re_id, iv_no)
);

INSERT INTO paid_invoices_old (id, re_id, iv_no, amount)
SELECT id, re_id, doc_no, amount FROM paid_invoices;

DROP TABLE paid_invoices;
ALTER TABLE paid_invoices_old RENAME TO paid_invoices;

-- ── 3. Recreate original index on iv_no ───────────────────────────────────
CREATE INDEX idx_pi_iv_no ON paid_invoices(iv_no);

-- ── 4. Recreate original audit triggers (mig 076 shape) ───────────────────
CREATE TRIGGER audit_paid_invoices_insert
AFTER INSERT ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', NEW.id, 'INSERT',
        json_object(
            're_id',  NEW.re_id,
            'iv_no',  NEW.iv_no,
            'amount', NEW.amount
        )
    );
END;

CREATE TRIGGER audit_paid_invoices_update
AFTER UPDATE ON paid_invoices
WHEN (
       OLD.re_id  IS NOT NEW.re_id
    OR OLD.iv_no  IS NOT NEW.iv_no
    OR OLD.amount IS NOT NEW.amount
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'paid_invoices', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 're_id'  AS field, OLD.re_id  AS old_v, NEW.re_id  AS new_v WHERE OLD.re_id  IS NOT NEW.re_id
        UNION ALL SELECT 'iv_no',  OLD.iv_no,  NEW.iv_no  WHERE OLD.iv_no  IS NOT NEW.iv_no
        UNION ALL SELECT 'amount', OLD.amount, NEW.amount WHERE OLD.amount IS NOT NEW.amount
    );
END;

CREATE TRIGGER audit_paid_invoices_delete
BEFORE DELETE ON paid_invoices
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'paid_invoices', OLD.id, 'DELETE',
        json_object('re_id', OLD.re_id, 'iv_no', OLD.iv_no, 'amount', OLD.amount)
    );
END;

-- ── 5. Drop snapshot ──────────────────────────────────────────────────────
DROP TABLE IF EXISTS migration_082_snapshot;

COMMIT;
