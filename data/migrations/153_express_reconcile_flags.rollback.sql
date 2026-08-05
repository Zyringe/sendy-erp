-- ============================================================================
-- Rollback for 153 — drop the reconcile-scan tables/indexes/triggers.
-- Lossy: any open/applied/dismissed flag history is discarded. That is
-- acceptable for a rollback (the underlying sales_transactions/transactions
-- rows this feature only ever READS from before an explicit apply are
-- untouched either way).
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS express_reconcile_events_no_update;
DROP TRIGGER IF EXISTS express_reconcile_events_no_delete;
DROP TRIGGER IF EXISTS express_reconcile_flags_first_payload_immutable;

DROP INDEX IF EXISTS idx_ere_flag_id;
DROP TABLE IF EXISTS express_reconcile_events;

DROP INDEX IF EXISTS idx_erf_suppression_lookup;
DROP INDEX IF EXISTS idx_erf_state;
DROP INDEX IF EXISTS idx_erf_doc_base;
DROP INDEX IF EXISTS idx_erf_open_doc_base;
DROP TABLE IF EXISTS express_reconcile_flags;

COMMIT;
