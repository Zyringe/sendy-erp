-- 098_txn_review.rollback.sql
-- Rollback migration 098.

BEGIN;

DROP TABLE IF EXISTS txn_review_flags;
DROP TABLE IF EXISTS txn_review_docs;

DELETE FROM applied_migrations WHERE filename = '098_txn_review.sql';

COMMIT;
