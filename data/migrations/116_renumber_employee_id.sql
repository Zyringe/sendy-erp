-- ONE-TIME: align employees.id to emp_code number (id == CAST(SUBSTR(emp_code,4) AS INT)).
-- Verified bijection on local+prod 2026-06-25: id 1→EMP005, 2→EMP004, 3→EMP003,
--   4→EMP002, 5→EMP006, 6→EMP001 (identical on both). New id = code suffix:
--   EMP001→1, EMP002→2, EMP003→3, EMP004→4, EMP005→5, EMP006→6.
--
-- FK surface (only these reference employees.id):
--   employee_salary_history (7), leave_requests (6), payroll_items (10),
--   salary_advances (11), employee_leave_entitlements (0).
-- FK enforcement is OFF (PRAGMA foreign_keys=0) — no cascade mid-renumber.
-- audit_salary_advances_update watches employee_id; the other audit triggers
--   ignore key columns and do NOT fire on a key-only renumber.
--
-- Offset technique: tables with UNIQUE on employee_id (or the PK) get shifted
--   into a non-colliding range (+100000) then mapped back. Tables without
--   UNIQUE constraints on employee_id get a direct single-pass map.
-- NOTE: do NOT self-insert into applied_migrations (runner records it).
BEGIN;

CREATE TEMP TABLE _idmap AS
    SELECT id AS old_id, CAST(SUBSTR(emp_code, 4) AS INTEGER) AS new_id
      FROM employees;

-- Offset PK + tables with UNIQUE(employee_id, ...) into non-colliding range
UPDATE employees               SET id          = id          + 100000;
UPDATE payroll_items           SET employee_id = employee_id + 100000;
UPDATE employee_salary_history SET employee_id = employee_id + 100000;

-- Map offset → final (look up original id via offset reversal)
UPDATE employees SET id =
    (SELECT new_id FROM _idmap WHERE old_id = employees.id - 100000);
UPDATE payroll_items SET employee_id =
    (SELECT new_id FROM _idmap WHERE old_id = payroll_items.employee_id - 100000);
UPDATE employee_salary_history SET employee_id =
    (SELECT new_id FROM _idmap WHERE old_id = employee_salary_history.employee_id - 100000);

-- Direct map for tables with no UNIQUE on employee_id
UPDATE leave_requests SET employee_id =
    (SELECT new_id FROM _idmap WHERE old_id = leave_requests.employee_id);
UPDATE salary_advances SET employee_id =
    (SELECT new_id FROM _idmap WHERE old_id = salary_advances.employee_id);
UPDATE employee_leave_entitlements SET employee_id =
    (SELECT new_id FROM _idmap WHERE old_id = employee_leave_entitlements.employee_id);

-- Keep AUTOINCREMENT consistent so the next hire gets MAX(id)+1
UPDATE sqlite_sequence SET seq = (SELECT MAX(id) FROM employees)
 WHERE name = 'employees';

DROP TABLE _idmap;
COMMIT;
