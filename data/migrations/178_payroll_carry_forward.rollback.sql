-- Rollback for 178_payroll_carry_forward.sql.
--
-- Drops payroll_items.carried_in / carried_out via `ALTER TABLE ... DROP
-- COLUMN`, NOT a table rebuild-and-rename.
--
-- ⚠ DEVIATION from the plan's original note ("use a table-rebuild in the
-- rollback anyway because of the CHECK constraints") — that assumption did
-- not hold up under test:
--   1. Verified empirically (sqlite3.sqlite_version 3.51.0, this repo's
--      python venv): `ALTER TABLE payroll_items DROP COLUMN carried_out`
--      SUCCEEDS despite carried_out carrying a column-level `CHECK (>= 0)`.
--   2. This codebase already established the identical precedent in
--      158_conversion_input_role.rollback.sql: "`role` ... is referenced
--      only by its own CHECK constraint (which does not block DROP COLUMN
--      — verified)."
--   3. DROP COLUMN also round-trips truly BYTE-IDENTICAL sqlite_master text
--      for the table, which a rebuild-and-rename does NOT: `ALTER TABLE …
--      RENAME TO` re-serializes the CREATE TABLE statement with the new
--      name double-quoted (`CREATE TABLE "payroll_items"` vs the original
--      unquoted `CREATE TABLE payroll_items`) — verified by direct
--      comparison. DROP COLUMN never touches the table name, so no such
--      drift is possible.
-- DROP COLUMN has been supported since SQLite 3.35.0 (2021-03); nothing
-- older is running any part of this migration chain (see 158, already
-- shipped and relying on the same feature).
--
-- ⚠ GUARD FIRST: refuse if this would destroy real carry data. Unlike the
-- WHT rollback (157), there is no other column to fold carried_in/
-- carried_out back into — they have no pre-178 equivalent representation,
-- so a non-zero row here is unrecoverable the moment the columns are gone.
-- RAISE(ROLLBACK), not ABORT — ABORT would leave the transaction open, the
-- write lock held, and the TEMP table+trigger stranded for the life of the
-- connection (same reasoning as 157's rollback guard).

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

CREATE TEMP TABLE _carry_rollback_check (ok INTEGER);
CREATE TEMP TRIGGER _carry_rollback_guard BEFORE INSERT ON _carry_rollback_check
WHEN (SELECT COUNT(*) FROM payroll_items
       WHERE carried_in <> 0 OR carried_out <> 0) > 0
BEGIN
    SELECT RAISE(ROLLBACK,
        'rollback refused: payroll_items rows carry non-zero carried_in/'
        || 'carried_out. Dropping these columns destroys that data -- there '
        || 'is no other column to fold it into (unlike migration 157''s WHT '
        || 'merge). Export the affected rows (id, employee_id, run_id, '
        || 'carried_in, carried_out) BEFORE rolling back if this data must '
        || 'be preserved.');
END;
INSERT INTO _carry_rollback_check (ok) VALUES (1);
DROP TRIGGER _carry_rollback_guard;
DROP TABLE _carry_rollback_check;

-- Restore payroll_items' 3 audit triggers to their pre-178 bodies (no
-- carried_in/carried_out) BEFORE dropping the columns, so the schema is
-- self-consistent at every point in this script — a trigger still
-- referencing a dropped column would error the moment it next fires.
DROP TRIGGER IF EXISTS audit_payroll_items_delete;
DROP TRIGGER IF EXISTS audit_payroll_items_insert;
DROP TRIGGER IF EXISTS audit_payroll_items_update;

CREATE TRIGGER audit_payroll_items_delete
BEFORE DELETE ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', OLD.id, 'DELETE',
        json_object(
            'run_id',                    OLD.run_id,
            'employee_id',               OLD.employee_id,
            'salary_rate',               OLD.salary_rate,
            'base_amount',               OLD.base_amount,
            'unpaid_leave_days',         OLD.unpaid_leave_days,
            'unpaid_leave_deduction',    OLD.unpaid_leave_deduction,
            'diligence_allowance',       OLD.diligence_allowance,
            'diligence_forfeited',       OLD.diligence_forfeited,
            'diligence_forfeit_reason',  OLD.diligence_forfeit_reason,
            'bonus',                     OLD.bonus,
            'other_additions',           OLD.other_additions,
            'other_additions_note',      OLD.other_additions_note,
            'other_deductions',          OLD.other_deductions,
            'other_deductions_note',     OLD.other_deductions_note,
            'wht_amount',                OLD.wht_amount,
            'sso_employee',              OLD.sso_employee,
            'sso_employer',              OLD.sso_employer,
            'commission_amount',         OLD.commission_amount,
            'salary_advance_deduction',  OLD.salary_advance_deduction,
            'gross',                     OLD.gross,
            'net_pay',                   OLD.net_pay,
            'note',                      OLD.note
        )
    );
END;

CREATE TRIGGER audit_payroll_items_insert
AFTER INSERT ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', NEW.id, 'INSERT',
        json_object(
            'run_id',                    NEW.run_id,
            'employee_id',               NEW.employee_id,
            'salary_rate',               NEW.salary_rate,
            'base_amount',               NEW.base_amount,
            'unpaid_leave_days',         NEW.unpaid_leave_days,
            'unpaid_leave_deduction',    NEW.unpaid_leave_deduction,
            'diligence_allowance',       NEW.diligence_allowance,
            'diligence_forfeited',       NEW.diligence_forfeited,
            'diligence_forfeit_reason',  NEW.diligence_forfeit_reason,
            'bonus',                     NEW.bonus,
            'other_additions',           NEW.other_additions,
            'other_additions_note',      NEW.other_additions_note,
            'other_deductions',          NEW.other_deductions,
            'other_deductions_note',     NEW.other_deductions_note,
            'wht_amount',                NEW.wht_amount,
            'sso_employee',              NEW.sso_employee,
            'sso_employer',              NEW.sso_employer,
            'commission_amount',         NEW.commission_amount,
            'salary_advance_deduction',  NEW.salary_advance_deduction,
            'gross',                     NEW.gross,
            'net_pay',                   NEW.net_pay,
            'note',                      NEW.note
        )
    );
END;

CREATE TRIGGER audit_payroll_items_update
AFTER UPDATE ON payroll_items
WHEN (
       OLD.bonus                       IS NOT NEW.bonus
    OR OLD.other_additions             IS NOT NEW.other_additions
    OR OLD.other_deductions            IS NOT NEW.other_deductions
    OR OLD.wht_amount                  IS NOT NEW.wht_amount
    OR OLD.diligence_allowance         IS NOT NEW.diligence_allowance
    OR OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
    OR OLD.sso_employee                IS NOT NEW.sso_employee
    OR OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
    OR OLD.gross                       IS NOT NEW.gross
    OR OLD.net_pay                     IS NOT NEW.net_pay
    OR OLD.note                        IS NOT NEW.note
    OR OLD.other_additions_note        IS NOT NEW.other_additions_note
    OR OLD.other_deductions_note       IS NOT NEW.other_deductions_note
    OR OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_items', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'bonus'                       AS field, OLD.bonus                       AS old_v, NEW.bonus                       AS new_v WHERE OLD.bonus                       IS NOT NEW.bonus
        UNION ALL SELECT 'other_additions',             OLD.other_additions,             NEW.other_additions             WHERE OLD.other_additions             IS NOT NEW.other_additions
        UNION ALL SELECT 'other_deductions',            OLD.other_deductions,            NEW.other_deductions            WHERE OLD.other_deductions            IS NOT NEW.other_deductions
        UNION ALL SELECT 'wht_amount',                  OLD.wht_amount,                  NEW.wht_amount                  WHERE OLD.wht_amount                  IS NOT NEW.wht_amount
        UNION ALL SELECT 'diligence_allowance',         OLD.diligence_allowance,         NEW.diligence_allowance         WHERE OLD.diligence_allowance         IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',         OLD.diligence_forfeited,         NEW.diligence_forfeited         WHERE OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',                OLD.sso_employee,                NEW.sso_employee                WHERE OLD.sso_employee                IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction',    OLD.salary_advance_deduction,    NEW.salary_advance_deduction    WHERE OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'gross',                       OLD.gross,                       NEW.gross                       WHERE OLD.gross                       IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',                     OLD.net_pay,                     NEW.net_pay                     WHERE OLD.net_pay                     IS NOT NEW.net_pay
        UNION ALL SELECT 'note',                        OLD.note,                        NEW.note                        WHERE OLD.note                        IS NOT NEW.note
        UNION ALL SELECT 'other_additions_note',        OLD.other_additions_note,        NEW.other_additions_note        WHERE OLD.other_additions_note        IS NOT NEW.other_additions_note
        UNION ALL SELECT 'other_deductions_note',       OLD.other_deductions_note,       NEW.other_deductions_note       WHERE OLD.other_deductions_note       IS NOT NEW.other_deductions_note
        UNION ALL SELECT 'diligence_forfeit_reason',    OLD.diligence_forfeit_reason,    NEW.diligence_forfeit_reason    WHERE OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
    );
END;

-- Drop the two columns. Order doesn't matter (independent columns, no FK,
-- no index of their own) — carried_out first purely to mirror how the
-- forward migration added carried_in first.
ALTER TABLE payroll_items DROP COLUMN carried_out;
ALTER TABLE payroll_items DROP COLUMN carried_in;

DELETE FROM applied_migrations WHERE filename='178_payroll_carry_forward.sql';

COMMIT;
