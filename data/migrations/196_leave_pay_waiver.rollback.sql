-- Rollback for 196_leave_pay_waiver.sql.
--
-- Restores the mig 073 leave_requests UPDATE trigger byte-identical BEFORE
-- dropping the column, so no trigger ever references a missing column.
-- DROP COLUMN (not a rebuild) keeps the table's sqlite_master text
-- byte-identical, same reasoning as 178's rollback.
--
-- ⚠ Dropping the column forgets every waiver. A draft month then deducts
-- those days again on its next regenerate; a finalized month is untouched.
-- Export `SELECT id FROM leave_requests WHERE pay_waived = 1` first if the
-- waivers must survive.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

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
    );
END;

ALTER TABLE leave_requests DROP COLUMN pay_waived;

DELETE FROM applied_migrations WHERE filename = '196_leave_pay_waiver.sql';

COMMIT;
