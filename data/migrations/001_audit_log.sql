-- 001_audit_log.sql
-- Adds the audit_log table + composite index for table_name/row_id lookups.
-- Per-table triggers (products, transactions, received_payments) will be
-- rolled out in a follow-up migration once the table shape is in place.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/001_audit_log.sql
--
-- Verify:
--   sqlite3 .../inventory.db ".schema audit_log"
--   sqlite3 .../inventory.db "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_log';"
--
-- Rollback: 001_audit_log.rollback.sql

BEGIN;

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT    NOT NULL,
    row_id          INTEGER NOT NULL,
    action          TEXT    NOT NULL CHECK(action IN ('INSERT','UPDATE','DELETE')),
    changed_fields  TEXT,           -- JSON: {"field": [old, new], ...}
    user            TEXT,           -- session username (nullable for system writes)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audit_table_row ON audit_log(table_name, row_id);

COMMIT;
