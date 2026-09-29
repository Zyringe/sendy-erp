-- ============================================================================
-- 196 — leave_requests.pay_waived (อนุโลม: forgive one approved request's
-- salary deduction).
--
-- Spec: leave pay waiver v2 (Put, decision log 2026-09-28 + answers
-- 2026-09-29). The flag sits on ONE approved leave_requests row; payroll
-- (hr._compute_unpaid_days) then adds 0 unpaid days for it while the request
-- still consumes its allowance share, still counts in leave_balance and still
-- forfeits เบี้ยขยัน. Written only by hr.set_pay_waiver (and cleared by a
-- generic edit that changes who/what/when); audit_log is the record, so there
-- are no *_by / *_at columns.
--
-- Existing rows take DEFAULT 0, so applying this changes no money.
--
-- The leave_requests UPDATE audit trigger (mig 073 shape) is recreated with
-- pay_waived in its WHEN clause and column list. The rollback restores the
-- 073 body byte-identical before dropping the column.
--
-- ⚠ NOT re-runnable on its own: ADD COLUMN has no IF NOT EXISTS, so a second
-- apply fails with `duplicate column name: pay_waived`. Re-apply only after
-- 196_leave_pay_waiver.rollback.sql (tests/test_mig196_leave_pay_waiver.py).
-- Self-records inside the transaction, same reasoning as 178.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE leave_requests ADD COLUMN pay_waived INTEGER NOT NULL DEFAULT 0
    CHECK (pay_waived IN (0,1));

DROP TRIGGER IF EXISTS audit_leave_requests_update;
CREATE TRIGGER audit_leave_requests_update
AFTER UPDATE ON leave_requests
WHEN (
       OLD.status            IS NOT NEW.status
    OR OLD.start_date        IS NOT NEW.start_date
    OR OLD.end_date          IS NOT NEW.end_date
    OR OLD.days              IS NOT NEW.days
    OR OLD.leave_type_id     IS NOT NEW.leave_type_id
    OR OLD.reason            IS NOT NEW.reason
    OR OLD.has_medical_cert  IS NOT NEW.has_medical_cert
    OR OLD.pay_waived        IS NOT NEW.pay_waived
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'leave_requests', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'            AS field, OLD.status            AS old_v, NEW.status            AS new_v WHERE OLD.status            IS NOT NEW.status
        UNION ALL SELECT 'start_date',        OLD.start_date,        NEW.start_date        WHERE OLD.start_date        IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',          OLD.end_date,          NEW.end_date          WHERE OLD.end_date          IS NOT NEW.end_date
        UNION ALL SELECT 'days',              OLD.days,              NEW.days              WHERE OLD.days              IS NOT NEW.days
        UNION ALL SELECT 'leave_type_id',     OLD.leave_type_id,     NEW.leave_type_id     WHERE OLD.leave_type_id     IS NOT NEW.leave_type_id
        UNION ALL SELECT 'reason',            OLD.reason,            NEW.reason            WHERE OLD.reason            IS NOT NEW.reason
        UNION ALL SELECT 'has_medical_cert',  OLD.has_medical_cert,  NEW.has_medical_cert  WHERE OLD.has_medical_cert  IS NOT NEW.has_medical_cert
        UNION ALL SELECT 'pay_waived',        OLD.pay_waived,        NEW.pay_waived        WHERE OLD.pay_waived        IS NOT NEW.pay_waived
    );
END;

INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('196_leave_pay_waiver.sql', 'auto');

COMMIT;
