-- 194 — GH #637: make DBF purchase line identity match the weekly text writer.
--
-- Identity is (doc_no, bsn_code, line_seq), where line_seq is the line's
-- 1-based position among that product's lines on the document in Express
-- SEQNUM order. parse_weekly.py and the history loader already write that
-- value; express_dbf_source.py used raw SEQNUM, which counts every product on
-- the document. A text purchase upload over DBF-written rows therefore missed
-- the stored identity and inserted a second stock-posted line (#637).
--
-- This migration derives the importer's new value from the stored DBF-style
-- ordering: ROW_NUMBER() OVER (PARTITION BY doc_no, bsn_code ORDER BY
-- line_seq). Only rows whose value differs are snapshotted and updated. The
-- exact source identity is re-pointed in active transactions and in mig 156's
-- dormant deleted-ledger snapshot, which could otherwise restore a stale key.
--
-- No quantity, product, money, stock, cost, synced flag, or ledger movement is
-- changed. Updating transactions.source_line_seq fires neither the mig-080
-- stock trigger nor the audit trigger: both WHEN clauses ignore provenance.
-- Updating purchase_transactions.line_seq uses mig 173's declared import path.
--
-- The three persistent migration_194_* tables are rollback evidence, so they
-- are created IF NOT EXISTS and never dropped here: the drop-first convention
-- exists so a re-run cannot fail on an existing object, and DROPPING these
-- would instead hand a re-run an empty snapshot and silently disarm the
-- rollback. The transient TEMP table is the one object dropped first. A second
-- forward run therefore has an empty _mig194_remap, writes no business row,
-- and leaves the first run's snapshot intact.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TABLE IF EXISTS temp._mig194_remap;
CREATE TEMP TABLE _mig194_remap AS
WITH ranked AS (
    SELECT id,
           doc_no,
           bsn_code,
           line_seq AS old_line_seq,
           ROW_NUMBER() OVER (
               PARTITION BY doc_no, bsn_code
               ORDER BY line_seq, id
           ) AS new_line_seq
      FROM purchase_transactions
)
SELECT id, doc_no, bsn_code, old_line_seq, new_line_seq
  FROM ranked
 WHERE old_line_seq <> new_line_seq;

CREATE TABLE IF NOT EXISTS migration_194_purchase_line_seq (
    id                INTEGER PRIMARY KEY,
    old_line_seq      INTEGER NOT NULL,
    new_line_seq      INTEGER NOT NULL,
    old_change_source TEXT,
    old_change_actor  TEXT,
    old_change_reason TEXT,
    old_change_token  TEXT
);

CREATE TABLE IF NOT EXISTS migration_194_transaction_source_line_seq (
    id           INTEGER PRIMARY KEY,
    old_line_seq INTEGER NOT NULL,
    new_line_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_194_mig156_source_line_seq (
    id           INTEGER PRIMARY KEY,
    old_line_seq INTEGER NOT NULL,
    new_line_seq INTEGER NOT NULL
);

INSERT OR IGNORE INTO migration_194_purchase_line_seq
    (id, old_line_seq, new_line_seq, old_change_source, old_change_actor,
     old_change_reason, old_change_token)
SELECT p.id, r.old_line_seq, r.new_line_seq, p.change_source, p.change_actor,
       p.change_reason, p.change_token
  FROM purchase_transactions p
  JOIN _mig194_remap r ON r.id = p.id;

INSERT OR IGNORE INTO migration_194_transaction_source_line_seq
    (id, old_line_seq, new_line_seq)
SELECT t.id, t.source_line_seq, r.new_line_seq
  FROM transactions t
  JOIN _mig194_remap r
    ON r.doc_no = t.reference_no
   AND r.bsn_code IS t.source_bsn_code
   AND r.old_line_seq = t.source_line_seq
 WHERE t.source_line_seq <> r.new_line_seq;

INSERT OR IGNORE INTO migration_194_mig156_source_line_seq
    (id, old_line_seq, new_line_seq)
SELECT t.id, t.source_line_seq, r.new_line_seq
  FROM migration_156_deleted_ledger t
  JOIN _mig194_remap r
    ON r.doc_no = t.reference_no
   AND r.bsn_code IS t.source_bsn_code
   AND r.old_line_seq = t.source_line_seq
 WHERE t.source_line_seq <> r.new_line_seq;

UPDATE transactions
   SET source_line_seq = (
       SELECT s.new_line_seq
         FROM migration_194_transaction_source_line_seq s
        WHERE s.id = transactions.id
   )
 WHERE id IN (SELECT id FROM migration_194_transaction_source_line_seq)
   AND source_line_seq IS (
       SELECT s.old_line_seq
         FROM migration_194_transaction_source_line_seq s
        WHERE s.id = transactions.id
   );

UPDATE migration_156_deleted_ledger
   SET source_line_seq = (
       SELECT s.new_line_seq
         FROM migration_194_mig156_source_line_seq s
        WHERE s.id = migration_156_deleted_ledger.id
   )
 WHERE id IN (SELECT id FROM migration_194_mig156_source_line_seq)
   AND source_line_seq IS (
       SELECT s.old_line_seq
         FROM migration_194_mig156_source_line_seq s
        WHERE s.id = migration_156_deleted_ledger.id
   );

-- Avoid transient key collisions such as old {2,3} -> new {1,2}. The final
-- index is byte-for-byte the same definition as mig 148/schema.sql.
DROP INDEX IF EXISTS idx_purchase_txn_doc_code_line;

UPDATE purchase_transactions
   SET line_seq      = (
           SELECT r.new_line_seq FROM _mig194_remap r
            WHERE r.id = purchase_transactions.id
       ),
       change_source = 'import',
       change_actor  = 'migration-194',
       change_reason = NULL,
       change_token  = 'mig194-line-seq-' || id || '-' || hex(randomblob(8))
 WHERE id IN (SELECT id FROM _mig194_remap)
   AND line_seq IS (
       SELECT r.old_line_seq FROM _mig194_remap r
        WHERE r.id = purchase_transactions.id
   );

CREATE UNIQUE INDEX idx_purchase_txn_doc_code_line
    ON purchase_transactions(doc_no, bsn_code, line_seq)
 WHERE bsn_code IS NOT NULL;

DROP TABLE IF EXISTS temp._mig194_remap;

COMMIT;
