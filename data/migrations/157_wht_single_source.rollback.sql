PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

-- Reverse the backfill on the CURRENT table (not a snapshot), so any row
-- written AFTER the forward migration (e.g. resolve_wht auto-applying WHT to
-- a newly-generated run) also rolls back cleanly — same rule as the rename/
-- pure-cosmetic migration pattern (.claude/rules/erp-engineering-discipline.md).
-- ⚠ GUARD FIRST: refuse rather than write a representation 157 cannot re-read.
-- Summing preserves the money, but the composite note ('ค่าปรับ + ภาษีหัก ณ ที่จ่าย')
-- no longer matches the forward migration's exact-match backfill, so a later
-- forward→rollback→forward cycle would leave the whole ฿532 stuck in
-- other_deductions with wht_amount 0 and NO employee_wht_history seed — i.e.
-- payroll silently stops withholding. Verified 2026-08-11 (Codex review).
--
-- ⚠ RAISE(**ROLLBACK**), not ABORT. ABORT undoes only the failing statement: it
-- leaves `BEGIN IMMEDIATE` open, the write lock held, and the TEMP table+trigger
-- below stranded for the life of the connection — a retry then dies on
-- `table _wht_rollback_check already exists`, and the next executescript()
-- implicitly commits the abandoned transaction. Measured both ways 2026-08-11
-- (Codex round 2): ROLLBACK leaves in_transaction False, sqlite_temp_master
-- empty, the lock released, and a retry reports this same refusal.
--
-- ⚠ REMEDY — do NOT simply zero one of the two columns. Each column is a
-- separate categorised amount and both are subtracted from net_pay, so zeroing
-- either drops the total deducted (by the WHT, or by the other deduction) and
-- the row stops reconciling unless net_pay is recomputed — which changes what
-- the employee was paid. The safe procedure is:
--   1. export the affected rows' (id, wht_amount, other_deductions,
--      other_deductions_note, net_pay) BEFORE touching anything;
--   2. consolidate into a single other_deductions that PRESERVES the total
--      (other_deductions + wht_amount) with a note naming both parts, and set
--      wht_amount = 0 — net_pay is then unchanged;
--   3. run this rollback;
--   4. after any later re-apply of 157, restore the split from the export.
CREATE TEMP TABLE _wht_rollback_check (ok INTEGER);
CREATE TEMP TRIGGER _wht_rollback_guard BEFORE INSERT ON _wht_rollback_check
WHEN (SELECT COUNT(*) FROM payroll_items
       WHERE wht_amount > 0 AND other_deductions > 0) > 0
BEGIN
    SELECT RAISE(ROLLBACK,
        'rollback refused: payroll_items rows carry BOTH wht_amount and other_deductions. '
        || 'Merging them is not reversible by a later re-apply of migration 157 '
        || '(the combined note stops matching its backfill, and WHT would be lost). '
        || 'Do NOT just zero a column — that changes the total deducted. Export the '
        || 'affected rows, consolidate into other_deductions PRESERVING the total '
        || '(other_deductions + wht_amount, wht_amount = 0, note naming both parts), '
        || 're-run this rollback, and restore the split after any later re-apply.');
END;
INSERT INTO _wht_rollback_check (ok) VALUES (1);
DROP TRIGGER _wht_rollback_guard;
DROP TABLE _wht_rollback_check;

-- ⚠ ADD, do not overwrite. Post-157 a row can carry BOTH a real deduction
-- (e.g. ค่าปรับ ฿500 keyed by an admin) AND the standing WHT ฿32 in separate
-- columns; pre-157 that same row would have been ONE other_deductions of
-- ฿532, so summing is the faithful inverse. An earlier version assigned
-- `other_deductions = wht_amount`, which silently destroyed the ฿500 —
-- verified by probe, 2026-08-10. net_pay stays consistent either way because
-- the total deducted is unchanged (pre-157 net subtracts other_deductions
-- only; post-157 it subtracts other_deductions + wht_amount).
UPDATE payroll_items
   SET other_deductions = other_deductions + wht_amount,
       other_deductions_note = CASE
           WHEN other_deductions_note IS NULL OR other_deductions_note = ''
               THEN 'ภาษีหัก ณ ที่จ่าย'
           ELSE other_deductions_note || ' + ภาษีหัก ณ ที่จ่าย'
       END
 WHERE wht_amount > 0;

-- Restore payroll_items' 3 audit triggers to their pre-157 bodies (no
-- wht_amount) — byte-identical to origin/main @ 8e53dfc.
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

ALTER TABLE payroll_items DROP COLUMN wht_amount;

DROP TRIGGER IF EXISTS audit_employee_wht_history_delete;
DROP TRIGGER IF EXISTS audit_employee_wht_history_insert;
DROP TRIGGER IF EXISTS audit_employee_wht_history_update;
DROP INDEX IF EXISTS idx_wht_hist_emp;
DROP TABLE IF EXISTS employee_wht_history;

DELETE FROM applied_migrations WHERE filename='157_wht_single_source.sql';

COMMIT;
