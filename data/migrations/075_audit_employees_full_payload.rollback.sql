-- Rollback for mig 075 — restore mig 071's narrower audit_employees_update.

BEGIN;

DROP TRIGGER IF EXISTS audit_employees_update;
CREATE TRIGGER audit_employees_update
AFTER UPDATE ON employees
WHEN (
       OLD.full_name           IS NOT NEW.full_name
    OR OLD.nickname             IS NOT NEW.nickname
    OR OLD.start_date           IS NOT NEW.start_date
    OR OLD.end_date             IS NOT NEW.end_date
    OR OLD.probation_end_date   IS NOT NEW.probation_end_date
    OR OLD.sso_enrolled         IS NOT NEW.sso_enrolled
    OR OLD.diligence_allowance  IS NOT NEW.diligence_allowance
    OR OLD.is_active            IS NOT NEW.is_active
    OR OLD.salesperson_code     IS NOT NEW.salesperson_code
    OR OLD.position             IS NOT NEW.position
    OR OLD.note                 IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'employees', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'full_name'           AS field, OLD.full_name           AS old_v, NEW.full_name           AS new_v WHERE OLD.full_name           IS NOT NEW.full_name
        UNION ALL SELECT 'nickname',            OLD.nickname,            NEW.nickname            WHERE OLD.nickname             IS NOT NEW.nickname
        UNION ALL SELECT 'start_date',          OLD.start_date,          NEW.start_date          WHERE OLD.start_date           IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',            OLD.end_date,            NEW.end_date            WHERE OLD.end_date             IS NOT NEW.end_date
        UNION ALL SELECT 'probation_end_date',  OLD.probation_end_date,  NEW.probation_end_date  WHERE OLD.probation_end_date   IS NOT NEW.probation_end_date
        UNION ALL SELECT 'sso_enrolled',        OLD.sso_enrolled,        NEW.sso_enrolled        WHERE OLD.sso_enrolled         IS NOT NEW.sso_enrolled
        UNION ALL SELECT 'diligence_allowance', OLD.diligence_allowance, NEW.diligence_allowance WHERE OLD.diligence_allowance  IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'is_active',           OLD.is_active,           NEW.is_active           WHERE OLD.is_active            IS NOT NEW.is_active
        UNION ALL SELECT 'salesperson_code',    OLD.salesperson_code,    NEW.salesperson_code    WHERE OLD.salesperson_code     IS NOT NEW.salesperson_code
        UNION ALL SELECT 'position',            OLD.position,            NEW.position            WHERE OLD.position             IS NOT NEW.position
        UNION ALL SELECT 'note',                OLD.note,                NEW.note                WHERE OLD.note                 IS NOT NEW.note
    );
END;

DELETE FROM applied_migrations WHERE filename = '075_audit_employees_full_payload.sql';
COMMIT;
