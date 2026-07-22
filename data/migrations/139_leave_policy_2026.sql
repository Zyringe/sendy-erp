-- ============================================================================
-- Migration 139 — leave policy update (Put 2026-07-22)
--
-- Two policy changes, both MORE generous than the Thai statutory floor:
--   1) ลากิจ (PERSONAL): default quota 3 → 6 days, still granted from day one,
--      no probation gate (unchanged gate).
--   2) ลาพักผ่อนประจำปี (ANNUAL): replace the all-or-nothing `after_1yr` gate
--      with `prorate_probation` — prorated from HIRE date, granted after
--      probation. The proration math lives in hr.py::_prorate_annual
--      (0 while still on probation at year-end; else
--      round_half(6 × days_worked_in_year / days_in_year); a full calendar
--      year → 6). Override rows in employee_leave_entitlements still win.
--
-- Schema: `quota_basis` has a CHECK that only allows 'after_1yr' | NULL, so
-- SQLite needs a table rebuild to widen it to add 'prorate_probation'.
-- leave_types has NO triggers (verified) and 2 inbound FKs
-- (employee_leave_entitlements, leave_requests) → PRAGMA foreign_keys=OFF
-- around the rebuild so the FKs resolve against the renamed table. 5 rows,
-- copied verbatim with an explicit column list.
--
-- One-time data corrections (guarded by business key emp_code; no-op on a
-- fresh/test DB that lacks these employees):
--   - EMP005 (บอล): drop the obsolete 1-day ANNUAL override — the new prorate
--     grants him 4.5, and the override (a workaround for the old gate) would
--     now cap him below what he already used (3) → phantom over-quota.
--   - EMP008 (เซี้ยม) / EMP009 (ปู้): fill missing hire date = 2026-02-01.
--   - EMP002/003/008/009: no probation (Put 6a) — stamp probation_end = hire.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- 1) Rebuild leave_types with the widened quota_basis CHECK.
DROP TABLE IF EXISTS leave_types_new;
CREATE TABLE leave_types_new (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    code                     TEXT    UNIQUE NOT NULL,
    name_th                  TEXT    NOT NULL,
    default_quota_days       REAL,
    is_paid                  INTEGER NOT NULL DEFAULT 1 CHECK(is_paid IN (0,1)),
    affects_diligence        INTEGER NOT NULL DEFAULT 0 CHECK(affects_diligence IN (0,1)),
    requires_cert_after_days INTEGER,
    quota_basis              TEXT    CHECK(quota_basis IN ('after_1yr','prorate_probation') OR quota_basis IS NULL),
    max_paid_days            REAL,
    sort_order               INTEGER NOT NULL DEFAULT 100,
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                     TEXT
);

INSERT INTO leave_types_new
    (id, code, name_th, default_quota_days, is_paid, affects_diligence,
     requires_cert_after_days, quota_basis, max_paid_days, sort_order,
     is_active, note)
SELECT
     id, code, name_th, default_quota_days, is_paid, affects_diligence,
     requires_cert_after_days, quota_basis, max_paid_days, sort_order,
     is_active, note
FROM leave_types;

DROP TABLE leave_types;
ALTER TABLE leave_types_new RENAME TO leave_types;

-- 2) ลากิจ 3 → 6.
UPDATE leave_types SET default_quota_days = 6 WHERE code = 'PERSONAL';

-- 3) ลาพักผ่อน: after_1yr → prorate_probation (now allowed by the new CHECK).
UPDATE leave_types SET quota_basis = 'prorate_probation' WHERE code = 'ANNUAL';

-- 4) One-time data corrections (guarded by emp_code).
DELETE FROM employee_leave_entitlements
 WHERE employee_id  = (SELECT id FROM employees   WHERE emp_code = 'EMP005')
   AND leave_type_id = (SELECT id FROM leave_types WHERE code = 'ANNUAL')
   AND year = 2026 AND quota_days = 1.0;

UPDATE employees SET start_date = '2026-02-01'
 WHERE emp_code IN ('EMP008','EMP009') AND start_date IS NULL;

UPDATE employees SET probation_days = 0, probation_end_date = start_date
 WHERE emp_code IN ('EMP002','EMP003','EMP008','EMP009');

COMMIT;

PRAGMA foreign_keys = ON;
