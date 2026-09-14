-- Rollback 181 — put every employee identity cell 181 rewrote back to its
-- ORIGINAL spelling from migration_181_snapshot, then drop the snapshot so
-- sqlite_master comes back byte-identical to the pre-migration schema.
--
-- From the SNAPSHOT, never from a pattern: '1234567890' cannot say whether it
-- was stored '123-4-56789-0' or bare, nor NULL whether it was ''.
--
-- A cell goes back ONLY while it still holds exactly what 181 wrote
-- (new_value IS the current value). A cell HR edited after 181 is newer than
-- the snapshot and stays as it is — restoring the old spelling there would
-- silently undo a real edit. old_value is NOT NULL by the snapshot's own DDL,
-- which is what lets COALESCE mean "no restorable snapshot row -> keep".
--
-- One UPDATE, so each restored employee gets one audit_log row
-- (audit_employees_update); audit_log itself is append-only and keeps both the
-- forward and the rollback entries.
--
-- Re-runnable: the IF NOT EXISTS keeps a second run a harmless no-op rather
-- than "no such table: migration_181_snapshot".
PRAGMA busy_timeout = 10000;

BEGIN;

CREATE TABLE IF NOT EXISTS migration_181_snapshot (
    employee_id INTEGER NOT NULL,
    field       TEXT    NOT NULL
                CHECK (field IN ('national_id', 'phone', 'bank_account_no')),
    old_value   TEXT    NOT NULL,
    new_value   TEXT,
    PRIMARY KEY (employee_id, field)
);

UPDATE employees
   SET national_id = COALESCE(
         (SELECT s.old_value FROM migration_181_snapshot s
           WHERE s.employee_id = employees.id AND s.field = 'national_id'
             AND s.new_value IS employees.national_id),
         national_id),
       phone = COALESCE(
         (SELECT s.old_value FROM migration_181_snapshot s
           WHERE s.employee_id = employees.id AND s.field = 'phone'
             AND s.new_value IS employees.phone),
         phone),
       bank_account_no = COALESCE(
         (SELECT s.old_value FROM migration_181_snapshot s
           WHERE s.employee_id = employees.id AND s.field = 'bank_account_no'
             AND s.new_value IS employees.bank_account_no),
         bank_account_no)
 WHERE id IN (SELECT employee_id FROM migration_181_snapshot);

DROP TABLE migration_181_snapshot;

COMMIT;
