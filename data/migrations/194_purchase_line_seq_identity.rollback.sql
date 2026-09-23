-- Rollback 194 — restore every changed purchase/source line sequence from the
-- persistent snapshots, then remove the snapshots and migration stamp.
--
-- purchase_transactions provenance is restored too, in a second metadata-only
-- UPDATE. The first UPDATE declares the business-field change for mig 173; the
-- second fires neither its guard nor its audit trigger because no guarded
-- business field changes. Thus each source row itself returns byte-for-byte to
-- its pre-194 values (audit_log remains the append-only history, as designed).

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

UPDATE transactions
   SET source_line_seq = (
       SELECT s.old_line_seq
         FROM migration_194_transaction_source_line_seq s
        WHERE s.id = transactions.id
   )
 WHERE id IN (SELECT id FROM migration_194_transaction_source_line_seq);

UPDATE migration_156_deleted_ledger
   SET source_line_seq = (
       SELECT s.old_line_seq
         FROM migration_194_mig156_source_line_seq s
        WHERE s.id = migration_156_deleted_ledger.id
   )
 WHERE id IN (SELECT id FROM migration_194_mig156_source_line_seq);

DROP INDEX IF EXISTS idx_purchase_txn_doc_code_line;

UPDATE purchase_transactions
   SET line_seq      = (
           SELECT s.old_line_seq FROM migration_194_purchase_line_seq s
            WHERE s.id = purchase_transactions.id
       ),
       change_source = 'manual',
       change_actor  = 'migration-194-rollback',
       change_reason = 'Rollback of migration 194 (GH #637 purchase line identity)',
       change_token  = 'mig194-rollback-' || id || '-' || hex(randomblob(8))
 WHERE id IN (SELECT id FROM migration_194_purchase_line_seq);

-- Restore provenance exactly after the declared line_seq change above.
UPDATE purchase_transactions
   SET change_source = (
           SELECT s.old_change_source FROM migration_194_purchase_line_seq s
            WHERE s.id = purchase_transactions.id
       ),
       change_actor = (
           SELECT s.old_change_actor FROM migration_194_purchase_line_seq s
            WHERE s.id = purchase_transactions.id
       ),
       change_reason = (
           SELECT s.old_change_reason FROM migration_194_purchase_line_seq s
            WHERE s.id = purchase_transactions.id
       ),
       change_token = (
           SELECT s.old_change_token FROM migration_194_purchase_line_seq s
            WHERE s.id = purchase_transactions.id
       )
 WHERE id IN (SELECT id FROM migration_194_purchase_line_seq);

CREATE UNIQUE INDEX idx_purchase_txn_doc_code_line
    ON purchase_transactions(doc_no, bsn_code, line_seq)
 WHERE bsn_code IS NOT NULL;

DROP TABLE IF EXISTS migration_194_mig156_source_line_seq;
DROP TABLE IF EXISTS migration_194_transaction_source_line_seq;
DROP TABLE IF EXISTS migration_194_purchase_line_seq;

DELETE FROM applied_migrations
 WHERE filename = '194_purchase_line_seq_identity.sql';

COMMIT;
