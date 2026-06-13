-- 099_txn_review_v2.rollback.sql  — restore 098 batch-keyed shape (empty)
BEGIN;
DROP TABLE IF EXISTS txn_review_flags;
DROP TABLE IF EXISTS txn_review_docs;
CREATE TABLE txn_review_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_log(id),
    doc_base TEXT NOT NULL, date_iso TEXT NOT NULL, customer TEXT, customer_code TEXT,
    line_count INTEGER NOT NULL DEFAULT 0, flag_count INTEGER NOT NULL DEFAULT 0,
    max_severity TEXT, flags_fingerprint TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending','ok','wrong','auto_passed')),
    reviewed_by TEXT, reviewed_at TEXT, note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(batch_id, doc_base)
);
CREATE INDEX idx_txn_review_docs_batch ON txn_review_docs(batch_id, review_status);
CREATE TABLE txn_review_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_review_id INTEGER NOT NULL REFERENCES txn_review_docs(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL, txn_id INTEGER, doc_no TEXT NOT NULL,
    rule_code TEXT NOT NULL, severity TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
    message_th TEXT NOT NULL, details_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_txn_review_flags_doc ON txn_review_flags(doc_review_id);
CREATE INDEX idx_txn_review_flags_batch ON txn_review_flags(batch_id, rule_code);
COMMIT;
