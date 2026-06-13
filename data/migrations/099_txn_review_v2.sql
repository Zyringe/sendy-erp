-- 099_txn_review_v2.sql
-- ตรวจบิล v2: document-keyed, decision-free, suspicious-only.
-- Replaces the batch-keyed 098 tables. No decisions to preserve (read-only check).
BEGIN;

DROP TABLE IF EXISTS txn_review_flags;   -- child first (FK)
DROP TABLE IF EXISTS txn_review_docs;

CREATE TABLE txn_review_docs (
    doc_base        TEXT PRIMARY KEY,
    date_iso        TEXT NOT NULL,
    customer        TEXT,
    customer_code   TEXT,
    line_count      INTEGER NOT NULL DEFAULT 0,
    flag_count      INTEGER NOT NULL DEFAULT 0,
    max_severity    TEXT,
    free_goods_note TEXT,
    scanned_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_txn_review_docs_date ON txn_review_docs(date_iso DESC);

CREATE TABLE txn_review_flags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_base      TEXT NOT NULL REFERENCES txn_review_docs(doc_base) ON DELETE CASCADE,
    txn_id        INTEGER,
    doc_no        TEXT NOT NULL,
    rule_code     TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
    message_th    TEXT NOT NULL,
    details_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_txn_review_flags_doc ON txn_review_flags(doc_base);

COMMIT;
