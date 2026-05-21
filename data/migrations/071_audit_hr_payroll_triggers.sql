-- ============================================================================
-- Migration 071 — audit_log triggers for HR + payroll tables
--
-- Why
--   Mig 070 added triggers for transactions + received_payments. HR/payroll
--   tables remained unaudited — meaning bulk edits, payroll un-finalize, or
--   direct SQL touches to employees/salary_advances/etc. left no trace.
--   Discovered when reconciling May 2026 payroll: I un-finalized a run,
--   regenerated items, and tweaked diligence_allowance with no audit record.
--
-- Delta scope (6 tables, INSERT + watched-field UPDATE + DELETE):
--   - employees                  (start/end_date, sso_enrolled, diligence, active)
--   - employee_salary_history    (raises — append-only but we DELETE on correct)
--   - payroll_runs               (status transitions: draft↔finalized)
--   - payroll_items              (admin overrides of bonus/diligence/etc.)
--   - salary_advances            (creation + stamping via deducted_in_run_id)
--   - leave_requests             (status changes: pending → approved/cancelled)
--
-- Style: mirrors mig 070 — changed_by NULL (no session in SQLite triggers),
--   json_object for INSERT/DELETE, json_group_object diff for UPDATE,
--   BEFORE DELETE so OLD is still queryable.
-- ============================================================================

BEGIN;

-- ── employees ───────────────────────────────────────────────────────────────
CREATE TRIGGER audit_employees_insert
AFTER INSERT ON employees
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employees', NEW.id, 'INSERT',
        json_object(
            'emp_code',            NEW.emp_code,
            'full_name',           NEW.full_name,
            'nickname',            NEW.nickname,
            'company_id',          NEW.company_id,
            'employment_type',     NEW.employment_type,
            'start_date',          NEW.start_date,
            'end_date',            NEW.end_date,
            'sso_enrolled',        NEW.sso_enrolled,
            'diligence_allowance', NEW.diligence_allowance,
            'is_active',           NEW.is_active,
            'salesperson_code',    NEW.salesperson_code
        )
    );
END;

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

CREATE TRIGGER audit_employees_delete
BEFORE DELETE ON employees
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employees', OLD.id, 'DELETE',
        json_object('emp_code', OLD.emp_code, 'full_name', OLD.full_name, 'is_active', OLD.is_active)
    );
END;

-- ── employee_salary_history ─────────────────────────────────────────────────
CREATE TRIGGER audit_employee_salary_history_insert
AFTER INSERT ON employee_salary_history
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employee_salary_history', NEW.id, 'INSERT',
        json_object(
            'employee_id',     NEW.employee_id,
            'effective_date',  NEW.effective_date,
            'monthly_salary',  NEW.monthly_salary,
            'reason',          NEW.reason,
            'note',            NEW.note
        )
    );
END;

CREATE TRIGGER audit_employee_salary_history_delete
BEFORE DELETE ON employee_salary_history
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employee_salary_history', OLD.id, 'DELETE',
        json_object(
            'employee_id',     OLD.employee_id,
            'effective_date',  OLD.effective_date,
            'monthly_salary',  OLD.monthly_salary,
            'reason',          OLD.reason
        )
    );
END;

-- ── payroll_runs ────────────────────────────────────────────────────────────
CREATE TRIGGER audit_payroll_runs_insert
AFTER INSERT ON payroll_runs
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_runs', NEW.id, 'INSERT',
        json_object(
            'year_month', NEW.year_month,
            'company_id', NEW.company_id,
            'status',     NEW.status,
            'run_date',   NEW.run_date,
            'created_by', NEW.created_by
        )
    );
END;

CREATE TRIGGER audit_payroll_runs_update
AFTER UPDATE ON payroll_runs
WHEN (OLD.status IS NOT NEW.status OR OLD.finalized_at IS NOT NEW.finalized_at)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_runs', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'       AS field, OLD.status       AS old_v, NEW.status       AS new_v WHERE OLD.status       IS NOT NEW.status
        UNION ALL SELECT 'finalized_at', OLD.finalized_at, NEW.finalized_at WHERE OLD.finalized_at IS NOT NEW.finalized_at
    );
END;

CREATE TRIGGER audit_payroll_runs_delete
BEFORE DELETE ON payroll_runs
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_runs', OLD.id, 'DELETE',
        json_object('year_month', OLD.year_month, 'company_id', OLD.company_id, 'status', OLD.status)
    );
END;

-- ── payroll_items (admin manual overrides) ──────────────────────────────────
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
    );
END;

-- ── salary_advances ─────────────────────────────────────────────────────────
CREATE TRIGGER audit_salary_advances_insert
AFTER INSERT ON salary_advances
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salary_advances', NEW.id, 'INSERT',
        json_object(
            'employee_id',  NEW.employee_id,
            'advance_date', NEW.advance_date,
            'amount',       NEW.amount,
            'note',         NEW.note
        )
    );
END;

CREATE TRIGGER audit_salary_advances_update
AFTER UPDATE ON salary_advances
WHEN (
       OLD.amount             IS NOT NEW.amount
    OR OLD.advance_date       IS NOT NEW.advance_date
    OR OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
    OR OLD.note               IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'salary_advances', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'amount'             AS field, OLD.amount             AS old_v, NEW.amount             AS new_v WHERE OLD.amount             IS NOT NEW.amount
        UNION ALL SELECT 'advance_date',       OLD.advance_date,       NEW.advance_date       WHERE OLD.advance_date       IS NOT NEW.advance_date
        UNION ALL SELECT 'deducted_in_run_id', OLD.deducted_in_run_id, NEW.deducted_in_run_id WHERE OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
        UNION ALL SELECT 'note',               OLD.note,               NEW.note               WHERE OLD.note               IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_salary_advances_delete
BEFORE DELETE ON salary_advances
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salary_advances', OLD.id, 'DELETE',
        json_object('employee_id', OLD.employee_id, 'advance_date', OLD.advance_date, 'amount', OLD.amount)
    );
END;

-- ── leave_requests ──────────────────────────────────────────────────────────
CREATE TRIGGER audit_leave_requests_insert
AFTER INSERT ON leave_requests
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'leave_requests', NEW.id, 'INSERT',
        json_object(
            'employee_id',   NEW.employee_id,
            'leave_type_id', NEW.leave_type_id,
            'start_date',    NEW.start_date,
            'end_date',      NEW.end_date,
            'days',          NEW.days,
            'status',        NEW.status
        )
    );
END;

CREATE TRIGGER audit_leave_requests_update
AFTER UPDATE ON leave_requests
WHEN (
       OLD.status        IS NOT NEW.status
    OR OLD.start_date    IS NOT NEW.start_date
    OR OLD.end_date      IS NOT NEW.end_date
    OR OLD.days          IS NOT NEW.days
    OR OLD.leave_type_id IS NOT NEW.leave_type_id
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'leave_requests', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'        AS field, OLD.status        AS old_v, NEW.status        AS new_v WHERE OLD.status        IS NOT NEW.status
        UNION ALL SELECT 'start_date',    OLD.start_date,    NEW.start_date    WHERE OLD.start_date    IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',      OLD.end_date,      NEW.end_date      WHERE OLD.end_date      IS NOT NEW.end_date
        UNION ALL SELECT 'days',          OLD.days,          NEW.days          WHERE OLD.days          IS NOT NEW.days
        UNION ALL SELECT 'leave_type_id', OLD.leave_type_id, NEW.leave_type_id WHERE OLD.leave_type_id IS NOT NEW.leave_type_id
    );
END;

CREATE TRIGGER audit_leave_requests_delete
BEFORE DELETE ON leave_requests
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'leave_requests', OLD.id, 'DELETE',
        json_object('employee_id', OLD.employee_id, 'start_date', OLD.start_date, 'end_date', OLD.end_date, 'status', OLD.status)
    );
END;

COMMIT;
