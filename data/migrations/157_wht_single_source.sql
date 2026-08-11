-- ============================================================================
-- 157 — WHT (ภาษีหัก ณ ที่จ่าย) single source of truth.
--
-- See projects/sendy-wht-single-source/plan.md for the full design. Put's
-- ฿32/month PIT withholding lived in two unconnected places: the payroll
-- report's `company_profile.json.tax_schedule` config (report path) and
-- `payroll_items.other_deductions` (payslip path) — nothing tied them
-- together, and they drifted (2026-07: report said ฿32, payroll ran ฿0).
--
-- This migration:
--   1. Adds `employee_wht_history` — mirrors `employee_salary_history`'s
--      effective-dated shape exactly (same resolve-by-month pattern via the
--      new `resolve_wht()` in hr.py, copied from `resolve_salary`).
--   2. Adds `payroll_items.wht_amount` — a DEDICATED column, not a reuse of
--      `other_deductions` (plan §"Design decisions" #2 — folding both into
--      one field makes "is this deduction tax?" depend on a free-text note,
--      and the real July row's note was NULL).
--   3. Backfills, DATA-DRIVEN (never by id or row count — the local dev DB
--      lags prod on this exact field, so a hardcoded backfill would behave
--      differently per env — see plan.md "Live data the backfill must
--      handle"): every payroll_items row manually keyed as ภาษีหัก ณ ที่จ่าย
--      via other_deductions moves that amount into wht_amount and clears the
--      free-text field.
--   4. Seeds employee_wht_history from the EARLIEST backfilled row per
--      employee, so `resolve_wht` finds a standing rate going forward.
--
-- payroll_items' three audit triggers (insert/update/delete) are recreated
-- to also cover wht_amount — it is money on the same row, split out of a
-- field those triggers already audit.
--
-- ⚠ NOT re-runnable on its own: `ALTER TABLE ... ADD COLUMN` has no IF NOT
-- EXISTS form, so a second apply fails with `duplicate column name:
-- wht_amount`. To re-apply, run 157_wht_single_source.rollback.sql FIRST
-- (that is exactly what the `pre157_conn` fixture in
-- tests/test_mig157_wht_single_source.py does).
--
-- Drop-first is used for the INDEX and TRIGGERS (derived objects, safe to
-- recreate) but deliberately NOT for `employee_wht_history`, which CARRIES
-- DATA: a hand-entered future rate (e.g. the 2027 rate, reason='adjust') is
-- not reconstructible from payroll_items, so a `DROP TABLE IF EXISTS` here
-- would silently destroy it on any re-apply. `CREATE TABLE` failing loudly on
-- an existing table is the correct behaviour. Same reason the seed below is
-- INSERT OR IGNORE.
--
-- Rehearsed forward + rollback on a `sqlite3 .backup` copy before commit
-- (never mutated a real DB directly — see
-- projects/sendy-wht-single-source/p1-progress.md).
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

-- ── employee_wht_history (mirrors employee_salary_history) ─────────────────
DROP TRIGGER IF EXISTS audit_employee_wht_history_delete;
DROP TRIGGER IF EXISTS audit_employee_wht_history_insert;
DROP TRIGGER IF EXISTS audit_employee_wht_history_update;
DROP INDEX IF EXISTS idx_wht_hist_emp;
-- NO `DROP TABLE IF EXISTS` here — see the header. This table carries data
-- that cannot be rebuilt from payroll_items.

CREATE TABLE employee_wht_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id),
    effective_date  TEXT    NOT NULL,
    -- >= 0: _recompute_totals SUBTRACTS this, so a negative standing rate would
    -- silently INCREASE net_pay. Constrained at the source rather than trusting
    -- the form's min=0 (Codex review 2026-08-11).
    monthly_wht     REAL    NOT NULL CHECK (monthly_wht >= 0),
    reason          TEXT    NOT NULL DEFAULT 'initial'
                            CHECK(reason IN ('initial','adjust')),
    note            TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(employee_id, effective_date)
);

CREATE INDEX idx_wht_hist_emp ON employee_wht_history(employee_id);

CREATE TRIGGER audit_employee_wht_history_delete
BEFORE DELETE ON employee_wht_history
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employee_wht_history', OLD.id, 'DELETE',
        json_object(
            'employee_id',     OLD.employee_id,
            'effective_date',  OLD.effective_date,
            'monthly_wht',     OLD.monthly_wht,
            'reason',          OLD.reason
        )
    );
END;

CREATE TRIGGER audit_employee_wht_history_insert
AFTER INSERT ON employee_wht_history
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'employee_wht_history', NEW.id, 'INSERT',
        json_object(
            'employee_id',     NEW.employee_id,
            'effective_date',  NEW.effective_date,
            'monthly_wht',     NEW.monthly_wht,
            'reason',          NEW.reason,
            'note',            NEW.note
        )
    );
END;

CREATE TRIGGER audit_employee_wht_history_update
AFTER UPDATE ON employee_wht_history
WHEN (
       OLD.monthly_wht     IS NOT NEW.monthly_wht
    OR OLD.effective_date  IS NOT NEW.effective_date
    OR OLD.reason          IS NOT NEW.reason
    OR OLD.note            IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'employee_wht_history', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'monthly_wht'     AS field, OLD.monthly_wht     AS old_v, NEW.monthly_wht     AS new_v WHERE OLD.monthly_wht     IS NOT NEW.monthly_wht
        UNION ALL SELECT 'effective_date',  OLD.effective_date,  NEW.effective_date  WHERE OLD.effective_date  IS NOT NEW.effective_date
        UNION ALL SELECT 'reason',          OLD.reason,          NEW.reason          WHERE OLD.reason          IS NOT NEW.reason
        UNION ALL SELECT 'note',            OLD.note,            NEW.note            WHERE OLD.note            IS NOT NEW.note
    );
END;

-- ── payroll_items.wht_amount ────────────────────────────────────────────────
-- CHECK (>= 0): _recompute_totals SUBTRACTS this, so a negative value would pay
-- the employee MORE than gross. SQLite DOES accept a CHECK on ADD COLUMN
-- (verified on 3.51.0 local / 3.46.1 prod: existing rows take the default and
-- pass, later INSERT/UPDATE of a negative is rejected) — an earlier note here
-- claimed this needed a full table rebuild, which was wrong. The constraint is
-- what makes the invariant true for direct SQL, migrations and future write
-- paths; hr.update_payroll_item keeps its ValueError as the friendly half.
ALTER TABLE payroll_items ADD COLUMN wht_amount REAL NOT NULL DEFAULT 0
    CHECK (wht_amount >= 0);

-- Recreate the 3 payroll_items audit triggers to also cover wht_amount
-- (money split out of other_deductions, which they already audit).
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

-- ── backfill: move manually-keyed ภาษีหัก ณ ที่จ่าย out of other_deductions ──
-- Data-driven match (value + note), never by id or row count.
UPDATE payroll_items
   SET wht_amount = other_deductions,
       other_deductions = 0,
       other_deductions_note = NULL
 WHERE other_deductions_note = 'ภาษีหัก ณ ที่จ่าย'
   AND other_deductions > 0;

-- ── seed employee_wht_history from the EARLIEST backfilled row per employee ─
-- Window-function rank (not a correlated MIN subquery) so a same-month tie
-- for one employee (two runs in different companies) is broken deterministically
-- by payroll_items.id rather than raising a UNIQUE(employee_id, effective_date)
-- conflict.
INSERT OR IGNORE INTO employee_wht_history (employee_id, effective_date, monthly_wht, reason)
SELECT employee_id, effective_date, wht_amount, 'initial'
  FROM (
        SELECT pi.employee_id                AS employee_id,
               pr.year_month || '-01'         AS effective_date,
               pi.wht_amount                  AS wht_amount,
               ROW_NUMBER() OVER (
                   PARTITION BY pi.employee_id
                   ORDER BY pr.year_month ASC, pi.id ASC
               ) AS rn
          FROM payroll_items pi
          JOIN payroll_runs pr ON pr.id = pi.run_id
         WHERE pi.wht_amount > 0
       )
 WHERE rn = 1;

-- Self-record INSIDE this transaction. `run_pending_migrations` records the
-- filename in a SEPARATE statement AFTER executescript has already committed
-- (database.py), so a crash in that window would leave the schema applied but
-- unrecorded — and because this file is deliberately not re-runnable, the next
-- boot would retry it and die on `duplicate column name: wht_amount`. The
-- runner uses INSERT OR IGNORE precisely so a migration may self-record (legacy
-- files 025-052 do); this makes schema + bookkeeping atomic together.
INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('157_wht_single_source.sql', 'auto');

COMMIT;
