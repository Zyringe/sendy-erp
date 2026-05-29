-- Migration 089: extend express_import_log.file_type CHECK to include 'ap_snapshot'.
-- SQLite doesn't support ALTER TABLE to modify constraints, so this is a table rebuild.
-- All existing rows and FKs are preserved; child tables reference by id (INTEGER PK)
-- so the rebuild is safe.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE express_import_log_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_type         TEXT    NOT NULL CHECK(file_type IN
                                ('credit_notes','payments_in','ar_snapshot',
                                 'ap_snapshot','payments_out','sales')),
    source_filename   TEXT,
    record_count      INTEGER NOT NULL DEFAULT 0,
    line_count        INTEGER NOT NULL DEFAULT 0,
    snapshot_date_iso TEXT,
    company_id        INTEGER REFERENCES companies(id),
    note              TEXT,
    status            TEXT    NOT NULL DEFAULT 'imported'
                              CHECK(status IN ('imported','failed','partial','superseded')),
    imported_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO express_import_log_new
    SELECT id, file_type, source_filename, record_count, line_count,
           snapshot_date_iso, company_id, note, status, imported_at
    FROM express_import_log;

DROP TABLE express_import_log;
ALTER TABLE express_import_log_new RENAME TO express_import_log;

CREATE INDEX idx_express_import_log_type ON express_import_log(file_type, imported_at DESC);

COMMIT;

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO applied_migrations(filename) VALUES ('089_express_import_log_ap_snapshot.sql');
