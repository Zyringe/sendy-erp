-- ============================================================================
-- Migration 075 — audit_employees_update: add bank + PII + operational fields
--
-- Codex pass 3 finding: audit_employees_update watched only 11 of 27 columns.
-- Material gaps:
--   - bank_name, bank_branch, bank_account_no, bank_account_name: an admin
--     can silently re-route salary payouts with no audit trail. **money path**
--   - national_id, phone, address: PII corrections need accountability
--   - employment_type, company_id: operational moves
--   - user_id: links employee record to login user (security-relevant)
--
-- Approach
--   Forward-only fix (mig 071 already applied on dev DB).
--   DROP + recreate audit_employees_update with the expanded field set.
--   Rollback restores mig 071's narrower trigger.
-- ============================================================================

BEGIN;

DROP TRIGGER IF EXISTS audit_employees_update;
CREATE TRIGGER audit_employees_update
AFTER UPDATE ON employees
WHEN (
       OLD.full_name             IS NOT NEW.full_name
    OR OLD.nickname               IS NOT NEW.nickname
    OR OLD.national_id            IS NOT NEW.national_id
    OR OLD.phone                  IS NOT NEW.phone
    OR OLD.address                IS NOT NEW.address
    OR OLD.position               IS NOT NEW.position
    OR OLD.company_id             IS NOT NEW.company_id
    OR OLD.employment_type        IS NOT NEW.employment_type
    OR OLD.start_date             IS NOT NEW.start_date
    OR OLD.end_date               IS NOT NEW.end_date
    OR OLD.probation_end_date     IS NOT NEW.probation_end_date
    OR OLD.sso_enrolled           IS NOT NEW.sso_enrolled
    OR OLD.diligence_allowance    IS NOT NEW.diligence_allowance
    OR OLD.bank_name              IS NOT NEW.bank_name
    OR OLD.bank_branch            IS NOT NEW.bank_branch
    OR OLD.bank_account_no        IS NOT NEW.bank_account_no
    OR OLD.bank_account_name      IS NOT NEW.bank_account_name
    OR OLD.salesperson_code       IS NOT NEW.salesperson_code
    OR OLD.user_id                IS NOT NEW.user_id
    OR OLD.is_active              IS NOT NEW.is_active
    OR OLD.note                   IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'employees', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'full_name'             AS field, OLD.full_name             AS old_v, NEW.full_name             AS new_v WHERE OLD.full_name             IS NOT NEW.full_name
        UNION ALL SELECT 'nickname',              OLD.nickname,              NEW.nickname              WHERE OLD.nickname               IS NOT NEW.nickname
        UNION ALL SELECT 'national_id',           OLD.national_id,           NEW.national_id           WHERE OLD.national_id            IS NOT NEW.national_id
        UNION ALL SELECT 'phone',                 OLD.phone,                 NEW.phone                 WHERE OLD.phone                  IS NOT NEW.phone
        UNION ALL SELECT 'address',               OLD.address,               NEW.address               WHERE OLD.address                IS NOT NEW.address
        UNION ALL SELECT 'position',              OLD.position,              NEW.position              WHERE OLD.position               IS NOT NEW.position
        UNION ALL SELECT 'company_id',            OLD.company_id,            NEW.company_id            WHERE OLD.company_id             IS NOT NEW.company_id
        UNION ALL SELECT 'employment_type',       OLD.employment_type,       NEW.employment_type       WHERE OLD.employment_type        IS NOT NEW.employment_type
        UNION ALL SELECT 'start_date',            OLD.start_date,            NEW.start_date            WHERE OLD.start_date             IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',              OLD.end_date,              NEW.end_date              WHERE OLD.end_date               IS NOT NEW.end_date
        UNION ALL SELECT 'probation_end_date',    OLD.probation_end_date,    NEW.probation_end_date    WHERE OLD.probation_end_date     IS NOT NEW.probation_end_date
        UNION ALL SELECT 'sso_enrolled',          OLD.sso_enrolled,          NEW.sso_enrolled          WHERE OLD.sso_enrolled           IS NOT NEW.sso_enrolled
        UNION ALL SELECT 'diligence_allowance',   OLD.diligence_allowance,   NEW.diligence_allowance   WHERE OLD.diligence_allowance    IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'bank_name',             OLD.bank_name,             NEW.bank_name             WHERE OLD.bank_name              IS NOT NEW.bank_name
        UNION ALL SELECT 'bank_branch',           OLD.bank_branch,           NEW.bank_branch           WHERE OLD.bank_branch            IS NOT NEW.bank_branch
        UNION ALL SELECT 'bank_account_no',       OLD.bank_account_no,       NEW.bank_account_no       WHERE OLD.bank_account_no        IS NOT NEW.bank_account_no
        UNION ALL SELECT 'bank_account_name',     OLD.bank_account_name,     NEW.bank_account_name     WHERE OLD.bank_account_name      IS NOT NEW.bank_account_name
        UNION ALL SELECT 'salesperson_code',      OLD.salesperson_code,      NEW.salesperson_code      WHERE OLD.salesperson_code       IS NOT NEW.salesperson_code
        UNION ALL SELECT 'user_id',               OLD.user_id,               NEW.user_id               WHERE OLD.user_id                IS NOT NEW.user_id
        UNION ALL SELECT 'is_active',             OLD.is_active,             NEW.is_active             WHERE OLD.is_active              IS NOT NEW.is_active
        UNION ALL SELECT 'note',                  OLD.note,                  NEW.note                  WHERE OLD.note                   IS NOT NEW.note
    );
END;

COMMIT;
