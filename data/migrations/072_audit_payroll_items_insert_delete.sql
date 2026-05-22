-- ============================================================================
-- Migration 072 — audit_log: INSERT/DELETE triggers on payroll_items + note
--                  field added to UPDATE trigger
--
-- Why
--   Mig 071 added an UPDATE-only trigger for payroll_items, intentionally
--   tight to admin manual overrides. Scrutinize pass (2026-05-21) flagged
--   the gap: payroll_items INSERT (per-employee generate_run) and DELETE
--   (regenerate clears items: `DELETE FROM payroll_items WHERE run_id=?`)
--   went completely unaudited — the most material table in the new scope
--   has no creation/destruction trail.
--
--   Also: the UPDATE trigger's WHEN clause omitted `note`. Editing item
--   notes for context ("ทำงานไม่เต็มเดือน", "จ่ายเพิ่ม bonus") was silent.
--
-- Delta scope
--   1. CREATE audit_payroll_items_insert (AFTER INSERT)
--   2. CREATE audit_payroll_items_delete (BEFORE DELETE)
--   3. DROP + recreate audit_payroll_items_update with `note` in WHEN +
--      UNION ALL
--
-- Volume note
--   INSERTs fire per-employee per generate_run (~5 rows × however many
--   regenerates per month). DELETEs fire per-row when generate_run wipes
--   existing items before rebuild. Net: a regenerate produces 2×N audit
--   rows (N delete + N insert). Acceptable — generate_run is admin-only
--   and infrequent.
-- ============================================================================

BEGIN;

-- ── INSERT (generate_run + any manual insert) ───────────────────────────────
CREATE TRIGGER audit_payroll_items_insert
AFTER INSERT ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', NEW.id, 'INSERT',
        json_object(
            'run_id',                   NEW.run_id,
            'employee_id',              NEW.employee_id,
            'salary_rate',              NEW.salary_rate,
            'base_amount',              NEW.base_amount,
            'diligence_allowance',      NEW.diligence_allowance,
            'diligence_forfeited',      NEW.diligence_forfeited,
            'sso_employee',             NEW.sso_employee,
            'salary_advance_deduction', NEW.salary_advance_deduction,
            'gross',                    NEW.gross,
            'net_pay',                  NEW.net_pay
        )
    );
END;

-- ── DELETE (generate_run wipe-before-rebuild + admin delete) ────────────────
CREATE TRIGGER audit_payroll_items_delete
BEFORE DELETE ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', OLD.id, 'DELETE',
        json_object(
            'run_id',      OLD.run_id,
            'employee_id', OLD.employee_id,
            'gross',       OLD.gross,
            'net_pay',     OLD.net_pay
        )
    );
END;

-- ── UPDATE: add `note` to WHEN clause + UNION ALL ───────────────────────────
DROP TRIGGER IF EXISTS audit_payroll_items_update;
CREATE TRIGGER audit_payroll_items_update
AFTER UPDATE ON payroll_items
WHEN (
       OLD.bonus                IS NOT NEW.bonus
    OR OLD.other_additions      IS NOT NEW.other_additions
    OR OLD.other_deductions     IS NOT NEW.other_deductions
    OR OLD.diligence_allowance  IS NOT NEW.diligence_allowance
    OR OLD.diligence_forfeited  IS NOT NEW.diligence_forfeited
    OR OLD.sso_employee         IS NOT NEW.sso_employee
    OR OLD.salary_advance_deduction IS NOT NEW.salary_advance_deduction
    OR OLD.gross                IS NOT NEW.gross
    OR OLD.net_pay              IS NOT NEW.net_pay
    OR OLD.note                 IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_items', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'bonus'                AS field, OLD.bonus                AS old_v, NEW.bonus                AS new_v WHERE OLD.bonus                IS NOT NEW.bonus
        UNION ALL SELECT 'other_additions',      OLD.other_additions,      NEW.other_additions      WHERE OLD.other_additions      IS NOT NEW.other_additions
        UNION ALL SELECT 'other_deductions',     OLD.other_deductions,     NEW.other_deductions     WHERE OLD.other_deductions     IS NOT NEW.other_deductions
        UNION ALL SELECT 'diligence_allowance',  OLD.diligence_allowance,  NEW.diligence_allowance  WHERE OLD.diligence_allowance  IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',  OLD.diligence_forfeited,  NEW.diligence_forfeited  WHERE OLD.diligence_forfeited  IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',         OLD.sso_employee,         NEW.sso_employee         WHERE OLD.sso_employee         IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction', OLD.salary_advance_deduction, NEW.salary_advance_deduction WHERE OLD.salary_advance_deduction IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'gross',                OLD.gross,                NEW.gross                WHERE OLD.gross                IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',              OLD.net_pay,              NEW.net_pay              WHERE OLD.net_pay              IS NOT NEW.net_pay
        UNION ALL SELECT 'note',                 OLD.note,                 NEW.note                 WHERE OLD.note                 IS NOT NEW.note
    );
END;

COMMIT;
