-- Rollback 184 -- put every #525 row this migration touched back to its
-- ORIGINAL (wrong) discount/total from migration_184_snapshot, then drop the
-- snapshot so sqlite_master comes back byte-identical to the pre-184 schema.
--
-- From the SNAPSHOT, never from a pattern: there is no way to derive
-- "3480.00" was ORIGINALLY the parser's shift artifact from the corrected
-- row alone -- the snapshot is the only record of what the bug produced.
--
-- A cell goes back ONLY while it still holds exactly what 184 wrote
-- (discount/total IS the snapshot's correct_discount/correct_total). A row
-- someone corrected differently after 184 (e.g. by hand, or by a later
-- migration) is newer than the snapshot and stays as it is -- restoring the
-- shifted values there would silently undo a real edit, not just this one.
--
-- One UPDATE, so each restored row gets one audit_log row
-- (audit_sales_transactions_update); audit_log itself is append-only and
-- keeps both the forward and the rollback entries. The rollback UPDATE must
-- also satisfy sales_transactions_change_needs_declaration, so it carries
-- its own fresh change_token / change_source='manual' / change_actor /
-- change_reason.
--
-- Re-runnable: the IF NOT EXISTS keeps a second run a harmless no-op rather
-- than "no such table: migration_184_snapshot".
PRAGMA busy_timeout = 10000;

BEGIN;

CREATE TABLE IF NOT EXISTS migration_184_snapshot (
    id                INTEGER PRIMARY KEY,
    doc_no            TEXT NOT NULL,
    bsn_code          TEXT NOT NULL,
    old_discount      TEXT NOT NULL,
    old_total         REAL NOT NULL,
    correct_discount  TEXT NOT NULL,
    correct_total     REAL NOT NULL,
    net_at_migration  REAL NOT NULL,
    qty_at_migration  REAL NOT NULL
);

UPDATE sales_transactions
   SET discount      = (SELECT s.old_discount FROM migration_184_snapshot s WHERE s.id = sales_transactions.id),
       total         = (SELECT s.old_total    FROM migration_184_snapshot s WHERE s.id = sales_transactions.id),
       change_token  = 'mig184-525-rollback-' || id,
       change_source = 'manual',
       change_actor  = 'ranpo-express/mig184-rollback',
       change_reason = 'Rollback of mig 184 (GH #525 discount/total correction) -- restoring pre-migration values from migration_184_snapshot'
 WHERE id IN (SELECT id FROM migration_184_snapshot)
   AND COALESCE(discount, '') IS (SELECT s.correct_discount FROM migration_184_snapshot s WHERE s.id = sales_transactions.id)
   AND total IS (SELECT s.correct_total FROM migration_184_snapshot s WHERE s.id = sales_transactions.id);

DROP TABLE migration_184_snapshot;

COMMIT;
