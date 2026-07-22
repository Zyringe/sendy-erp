-- Rollback 139 — restore the pre-2026-07-22 leave policy.
--
-- Reverts the reversible policy bits: PERSONAL 6→3, ANNUAL basis
-- prorate_probation→after_1yr, the quota_basis CHECK back to its original
-- ('after_1yr'|NULL), and re-adds บอล's 1-day ANNUAL override. The one-time
-- employee data corrections (start_date fill, probation_days=0) are NOT
-- reverted — re-nulling a hire date would be destructive and is harmless to
-- keep; leave them in place if rolling back.

PRAGMA foreign_keys = OFF;

BEGIN;

-- Must set ANNUAL back to after_1yr BEFORE narrowing the CHECK, else the copy
-- into the narrow-CHECK table would violate it.
UPDATE leave_types SET quota_basis = 'after_1yr'   WHERE code = 'ANNUAL';
UPDATE leave_types SET default_quota_days = 3       WHERE code = 'PERSONAL';

DROP TABLE IF EXISTS leave_types_old;
CREATE TABLE leave_types_old (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    code                     TEXT    UNIQUE NOT NULL,
    name_th                  TEXT    NOT NULL,
    default_quota_days       REAL,
    is_paid                  INTEGER NOT NULL DEFAULT 1 CHECK(is_paid IN (0,1)),
    affects_diligence        INTEGER NOT NULL DEFAULT 0 CHECK(affects_diligence IN (0,1)),
    requires_cert_after_days INTEGER,
    quota_basis              TEXT    CHECK(quota_basis IN ('after_1yr') OR quota_basis IS NULL),
    max_paid_days            REAL,
    sort_order               INTEGER NOT NULL DEFAULT 100,
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                     TEXT
);

INSERT INTO leave_types_old
    (id, code, name_th, default_quota_days, is_paid, affects_diligence,
     requires_cert_after_days, quota_basis, max_paid_days, sort_order,
     is_active, note)
SELECT
     id, code, name_th, default_quota_days, is_paid, affects_diligence,
     requires_cert_after_days, quota_basis, max_paid_days, sort_order,
     is_active, note
FROM leave_types;

DROP TABLE leave_types;
ALTER TABLE leave_types_old RENAME TO leave_types;

-- Re-add บอล's 1-day ANNUAL override (only if absent).
INSERT INTO employee_leave_entitlements (employee_id, leave_type_id, year, quota_days, note)
SELECT e.id, lt.id, 2026, 1.0,
       '6/1 holiday mixup: 1 annual day granted in probation per Put 2026-06-30'
  FROM employees e, leave_types lt
 WHERE e.emp_code = 'EMP005' AND lt.code = 'ANNUAL'
   AND NOT EXISTS (
        SELECT 1 FROM employee_leave_entitlements x
         WHERE x.employee_id = e.id AND x.leave_type_id = lt.id AND x.year = 2026);

COMMIT;

PRAGMA foreign_keys = ON;
