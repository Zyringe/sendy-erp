-- 181 — employee identity values stored canonical: bare digits, NULL = not recorded.
--
-- WHAT (#464, spec #460)
-- employees.national_id, employees.phone and employees.bank_account_no become
-- bare digits, and every '' among them becomes NULL. From this change on the
-- write path produces exactly that shape (hr_queries._identity_to_digits, called
-- by both _insert_employee and update_employee); this migration brings the rows
-- written before it to the same shape, so the inconsistency is gone rather than
-- merely hidden by the display filters #463 shipped.
--
-- WHY
-- Bank accounts sat in two shapes (some with the bank's dashes, some bare), and
-- "not recorded" had two spellings (NULL and ''), so no reader or query could
-- tell "never entered" from "entered blank". Display is filters.py's job
-- (bank_account / thai_phone / mask_national_id already ignore separators), so
-- nothing on screen changes when the dashes leave storage.
--
-- MEASURED on the PROD snapshot pulled 2026-09-14 (9 employees). The parent
-- spec's counts came from the dev DB, which lags prod; these are prod's:
--   national_id      3 NULL · 2 '' · 4 bare 13-digit          -> 2 cells change
--   phone            4 NULL · 2 '' · 3 bare 10-digit          -> 2 cells change
--   bank_account_no  0 NULL · 3 '' · 4 bare · 2 dashed 3-1-5-1 -> 5 cells change
-- The only non-digit character in any of the three columns was '-' (U+002D).
--
-- PRECONDITION: ABORT on a value this migration would have to destroy.
-- Only four characters are separators here: '-', ' ', '(' and ')'. That is
-- deliberately NARROWER than the write path, which drops every non-digit —
-- at write time a person is typing ONE number into ONE box, while a stored
-- value may carry meaning a strip would erase: two numbers comma-joined (the
-- strip glues them into one run of nonsense), a '+66' prefix (the strip leaves
-- 11 digits), prose ('ยังไม่มี' strips to nothing), Thai numerals. Any of those
-- aborts the whole migration instead of skipping the row or mangling it.
--
-- Mechanism (as in 177/180): BEFORE DELETE fires once per row, so DELETE on an
-- EMPTY precheck table is a no-op and on a NON-empty one aborts. The runner
-- (database.py::run_pending_migrations) rolls back and does NOT stamp
-- applied_migrations — a failed precondition leaves the DB untouched and the
-- migration pending.
--
-- RECOVERY — list the offending cells (ids and field names, no values):
--   SELECT id, emp_code, 'national_id' AS field FROM employees
--    WHERE REPLACE(REPLACE(REPLACE(REPLACE(national_id,'-',''),' ',''),'(',''),')','') GLOB '*[^0-9]*'
--   UNION ALL
--   SELECT id, emp_code, 'phone' FROM employees
--    WHERE REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')','') GLOB '*[^0-9]*'
--   UNION ALL
--   SELECT id, emp_code, 'bank_account_no' FROM employees
--    WHERE REPLACE(REPLACE(REPLACE(REPLACE(bank_account_no,'-',''),' ',''),'(',''),')','') GLOB '*[^0-9]*';
-- then open each employee in /hr, type the ONE number that should be kept
-- (the page stores it as bare digits), save, and restart.
--
-- ROLLBACK is exact, not derived: a stripped number cannot say where its dashes
-- were, so migration_181_snapshot records every cell this migration rewrites —
-- old value and new value — before anything is written. The rollback restores
-- a cell only while it still holds what 181 wrote, so an edit made after 181 is
-- never undone. The snapshot is named migration_% on purpose:
-- scripts/dump_schema.py and the schema-sync test both exclude that prefix, so
-- data/schema.sql does not change. It holds the same personal data the
-- employees row already holds, spelled with its old separators.
--
-- AUDIT: the UPDATE below fires audit_employees_update (mig 075 watches all
-- three columns), so every rewritten employee gets one audit_log row carrying
-- old and new values. The rollback writes one more. audit_log is append-only by
-- design and is not rolled back.
--
-- RE-RUNNABLE: a second run finds nothing non-canonical and changes nothing;
-- the snapshot is CREATE IF NOT EXISTS + INSERT OR IGNORE, so a re-run can
-- never overwrite the original spellings with already-stripped ones.
PRAGMA busy_timeout = 10000;

BEGIN;

-- ── precondition ────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS temp._mig181_precheck;
CREATE TEMP TABLE _mig181_precheck AS
SELECT id FROM employees
 WHERE REPLACE(REPLACE(REPLACE(REPLACE(national_id,'-',''),' ',''),'(',''),')','') GLOB '*[^0-9]*'
UNION ALL
SELECT id FROM employees
 WHERE REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')','') GLOB '*[^0-9]*'
UNION ALL
SELECT id FROM employees
 WHERE REPLACE(REPLACE(REPLACE(REPLACE(bank_account_no,'-',''),' ',''),'(',''),')','') GLOB '*[^0-9]*';

CREATE TEMP TRIGGER _mig181_precondition_guard
BEFORE DELETE ON _mig181_precheck
BEGIN
  SELECT RAISE(ABORT, 'mig 181 precondition FAILED: an employee national_id / phone / bank_account_no holds a character other than digits and - ( ) or space, which stripping would destroy. See the RECOVERY query in this migration header.');
END;

DELETE FROM _mig181_precheck;
DROP TRIGGER _mig181_precondition_guard;
DROP TABLE _mig181_precheck;

-- ── forensic snapshot (the rollback reads from here, never from a pattern) ──
CREATE TABLE IF NOT EXISTS migration_181_snapshot (
    employee_id INTEGER NOT NULL,
    field       TEXT    NOT NULL
                CHECK (field IN ('national_id', 'phone', 'bank_account_no')),
    old_value   TEXT    NOT NULL,   -- as stored before 181 ('' or with separators)
    new_value   TEXT,               -- what 181 wrote: bare digits, or NULL
    PRIMARY KEY (employee_id, field)
);

INSERT OR IGNORE INTO migration_181_snapshot (employee_id, field, old_value, new_value)
SELECT id, 'national_id', national_id,
       NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(national_id,'-',''),' ',''),'(',''),')',''), '')
  FROM employees
 WHERE national_id IS NOT
       NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(national_id,'-',''),' ',''),'(',''),')',''), '');

INSERT OR IGNORE INTO migration_181_snapshot (employee_id, field, old_value, new_value)
SELECT id, 'phone', phone,
       NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')',''), '')
  FROM employees
 WHERE phone IS NOT
       NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')',''), '');

INSERT OR IGNORE INTO migration_181_snapshot (employee_id, field, old_value, new_value)
SELECT id, 'bank_account_no', bank_account_no,
       NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(bank_account_no,'-',''),' ',''),'(',''),')',''), '')
  FROM employees
 WHERE bank_account_no IS NOT
       NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(bank_account_no,'-',''),' ',''),'(',''),')',''), '');

-- ── the transform: one UPDATE, so each employee gets one audit row ──────────
UPDATE employees
   SET national_id =
         NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(national_id,'-',''),' ',''),'(',''),')',''), ''),
       phone =
         NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')',''), ''),
       bank_account_no =
         NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(bank_account_no,'-',''),' ',''),'(',''),')',''), '')
 WHERE national_id IS NOT
         NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(national_id,'-',''),' ',''),'(',''),')',''), '')
    OR phone IS NOT
         NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')',''), '')
    OR bank_account_no IS NOT
         NULLIF(REPLACE(REPLACE(REPLACE(REPLACE(bank_account_no,'-',''),' ',''),'(',''),')',''), '');

-- ── postcondition: nothing non-canonical may survive ────────────────────────
-- Stated as the invariant itself, not a row count, so it holds on prod and on
-- any dev clone however many of these rows that DB happens to carry.
DROP TABLE IF EXISTS temp._mig181_postcheck;
CREATE TEMP TABLE _mig181_postcheck AS
SELECT id FROM employees
 WHERE national_id = '' OR national_id GLOB '*[^0-9]*'
    OR phone = '' OR phone GLOB '*[^0-9]*'
    OR bank_account_no = '' OR bank_account_no GLOB '*[^0-9]*';

CREATE TEMP TRIGGER _mig181_postcondition_guard
BEFORE DELETE ON _mig181_postcheck
BEGIN
  SELECT RAISE(ABORT, 'mig 181 postcondition FAILED: an employee identity value is still empty-string or still holds a non-digit after the UPDATE.');
END;

DELETE FROM _mig181_postcheck;
DROP TRIGGER _mig181_postcondition_guard;
DROP TABLE _mig181_postcheck;

COMMIT;
