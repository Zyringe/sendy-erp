-- Rollback 089: restore express_import_log without 'ap_snapshot' in CHECK.
-- ap_snapshot rows (if any) must be deleted first — they violate the original constraint.

PRAGMA foreign_keys = OFF;

BEGIN;

-- Remove any ap_snapshot batch rows (cascades to express_ap_outstanding via batch_id FK).
DELETE FROM express_import_log WHERE file_type = 'ap_snapshot';

CREATE TABLE express_import_log_old (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_type         TEXT    NOT NULL CHECK(file_type IN
                                ('credit_notes','payments_in','ar_snapshot',
                                 'payments_out','sales')),
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

INSERT INTO express_import_log_old
    SELECT id, file_type, source_filename, record_count, line_count,
           snapshot_date_iso, company_id, note, status, imported_at
    FROM express_import_log;

DROP TABLE express_import_log;
ALTER TABLE express_import_log_old RENAME TO express_import_log;

CREATE INDEX idx_express_import_log_type ON express_import_log(file_type, imported_at DESC);

COMMIT;

PRAGMA foreign_keys = ON;

DELETE FROM applied_migrations WHERE filename = '089_express_import_log_ap_snapshot.sql';
