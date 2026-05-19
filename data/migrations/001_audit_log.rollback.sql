-- 001_audit_log.rollback.sql
-- Rolls back 001_audit_log.sql.
--
-- Reversibility:
--   The forward migration only creates a new table and a new index — it does
--   not touch any existing data, columns, or constraints. Dropping the index
--   and the table restores the DB to its pre-migration state cleanly. Any
--   audit rows captured between forward+rollback are discarded (intended).
--
-- Pre-flight (recommended):
--   1. Stop the Flask app (avoid lock contention).
--   2. Take a backup:
--        /Users/putty/Sendai-Boonsawat/sendy_erp/scripts/backup_db.sh
--   3. Confirm no triggers reference audit_log yet:
--        sqlite3 .../inventory.db "SELECT name, sql FROM sqlite_master WHERE type='trigger';"
--   4. (If follow-up trigger migration was applied) drop those triggers FIRST,
--      otherwise SQLite will refuse to drop the table they reference.
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/001_audit_log.rollback.sql
--
-- Verify:
--   sqlite3 .../inventory.db ".tables" | tr ' ' '\n' | grep -c '^audit_log$'   # expect 0
--
-- Restore from backup (full DB-level rollback, if rollback above fails):
--   See /Users/putty/Sendai-Boonsawat/sendy_erp/scripts/RESTORE.md

BEGIN;

DROP INDEX IF EXISTS idx_audit_table_row;
DROP TABLE IF EXISTS audit_log;

COMMIT;
