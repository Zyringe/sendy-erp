-- 098_txn_review.sql
-- ตรวจบิล detection engine — two tables for per-batch document review.
--
-- txn_review_docs  — one row per (batch, doc_base): review decision + fingerprint
-- txn_review_flags — one row per rule-flag on a specific txn line
--
-- review_status lifecycle:
--   'pending'     — has flags, awaiting human review
--   'ok'          — staff marked as correct
--   'wrong'       — staff marked as incorrect
--   'auto_passed' — zero flags, auto-approved at scan time
--
-- No UI, no routes in this phase. Detection engine only (review_rules.py).
--
-- Apply:    sqlite3 .../inventory.db < 098_txn_review.sql
-- Rollback: 098_txn_review.rollback.sql

BEGIN;

CREATE TABLE txn_review_docs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES import_log(id),
    doc_base          TEXT    NOT NULL,
    date_iso          TEXT    NOT NULL,
    customer          TEXT,
    customer_code     TEXT,
    line_count        INTEGER NOT NULL DEFAULT 0,
    flag_count        INTEGER NOT NULL DEFAULT 0,       -- 0 = clean
    max_severity      TEXT,                             -- high|medium|low|NULL
    flags_fingerprint TEXT,                             -- sha1 of sorted (rule_code|doc_no|qty|unit_price)
    review_status     TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (review_status IN ('pending','ok','wrong','auto_passed')),
    reviewed_by       TEXT,
    reviewed_at       TEXT,
    note              TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(batch_id, doc_base)
);

CREATE INDEX idx_txn_review_docs_batch ON txn_review_docs(batch_id, review_status);

CREATE TABLE txn_review_flags (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_review_id  INTEGER NOT NULL REFERENCES txn_review_docs(id) ON DELETE CASCADE,
    batch_id       INTEGER NOT NULL,
    txn_id         INTEGER,       -- sales_transactions.id, advisory only, NO FK (rows replaced on re-import)
    doc_no         TEXT    NOT NULL,
    rule_code      TEXT    NOT NULL,
    severity       TEXT    NOT NULL CHECK (severity IN ('high','medium','low')),
    message_th     TEXT    NOT NULL,   -- pre-rendered Thai explanation
    details_json   TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_txn_review_flags_doc   ON txn_review_flags(doc_review_id);
CREATE INDEX idx_txn_review_flags_batch ON txn_review_flags(batch_id, rule_code);

COMMIT;
