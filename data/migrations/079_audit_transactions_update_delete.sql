-- ============================================================================
-- Migration 079 — audit_log UPDATE/DELETE triggers for `transactions`
--
-- Background
--   Mig 070 added `audit_transactions_insert` only, with the rationale
--   "transactions: INSERT only (append-only ledger)". That convention held until
--   a typo cleanup on 2026-05-25 ran two ad-hoc UPDATEs on `transactions.note`
--   (10 rows, "แก้ไข้..." → "แก้ไข..."). Those UPDATEs left no audit-trail
--   because no UPDATE trigger existed. Same gap applies to any future DELETE.
--
-- What this migration adds
--   - `audit_transactions_update`  — AFTER UPDATE, fires only on real diffs
--   - `audit_transactions_delete`  — BEFORE DELETE, captures all OLD values
--
-- Style mirrors mig 070's received_payments triggers exactly:
--   - WHEN clause filters no-op UPDATEs (column-by-column IS NOT comparison)
--   - JSON diff via json_group_object + UNION ALL of changed columns
--   - DELETE uses BEFORE so OLD is queryable
--
-- ⚠ Stock-integrity reminder (NOT addressed here)
--   There is still no `after_transaction_update` / `after_transaction_delete`
--   business trigger. Updating `quantity_change` or deleting a row will
--   silently desync `stock_levels`. The audit triggers below CAPTURE such
--   events but do not PREVENT or REPAIR them. Callers must remain disciplined:
--   prefer appending a reversing transaction over mutating an existing row.
-- ============================================================================

BEGIN;

CREATE TRIGGER audit_transactions_update
AFTER UPDATE ON transactions
WHEN (
       OLD.product_id      IS NOT NEW.product_id
    OR OLD.txn_type        IS NOT NEW.txn_type
    OR OLD.quantity_change IS NOT NEW.quantity_change
    OR OLD.unit_mode       IS NOT NEW.unit_mode
    OR OLD.reference_no    IS NOT NEW.reference_no
    OR OLD.note            IS NOT NEW.note
    OR OLD.created_at      IS NOT NEW.created_at
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'transactions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_id'      AS field, OLD.product_id      AS old_v, NEW.product_id      AS new_v WHERE OLD.product_id      IS NOT NEW.product_id
        UNION ALL SELECT 'txn_type',        OLD.txn_type,        NEW.txn_type        WHERE OLD.txn_type        IS NOT NEW.txn_type
        UNION ALL SELECT 'quantity_change', OLD.quantity_change, NEW.quantity_change WHERE OLD.quantity_change IS NOT NEW.quantity_change
        UNION ALL SELECT 'unit_mode',       OLD.unit_mode,       NEW.unit_mode       WHERE OLD.unit_mode       IS NOT NEW.unit_mode
        UNION ALL SELECT 'reference_no',    OLD.reference_no,    NEW.reference_no    WHERE OLD.reference_no    IS NOT NEW.reference_no
        UNION ALL SELECT 'note',            OLD.note,            NEW.note            WHERE OLD.note            IS NOT NEW.note
        UNION ALL SELECT 'created_at',      OLD.created_at,      NEW.created_at      WHERE OLD.created_at      IS NOT NEW.created_at
    );
END;

CREATE TRIGGER audit_transactions_delete
BEFORE DELETE ON transactions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'transactions', OLD.id, 'DELETE',
        json_object(
            'product_id',      OLD.product_id,
            'txn_type',        OLD.txn_type,
            'quantity_change', OLD.quantity_change,
            'unit_mode',       OLD.unit_mode,
            'reference_no',    OLD.reference_no,
            'note',            OLD.note,
            'created_at',      OLD.created_at
        )
    );
END;

COMMIT;
