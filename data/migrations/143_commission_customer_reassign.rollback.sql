-- ============================================================================
-- Rollback for 143 — drop commission_customer_reassign.
--
-- Dropping the table restores the pre-143 behaviour exactly: the commission
-- engine falls back to `received_payments.salesperson` verbatim, so the four
-- reassigned customers feed rep 31's base again.
--
-- No snapshot to restore from: the table is created by the forward migration,
-- so rows written after it are the only rows it has ever had. They are
-- decisions, not derived data — if this rollback is run to fix an engine bug
-- rather than to abandon the feature, capture the rows first:
--     SELECT * FROM commission_customer_reassign;
--
-- ⚠ NOT rolled back: the `customers.salesperson` update for 62ค003. That row
-- brought ร้าน คูณมีวัสดุ into line with the three customers already set to
-- '00' on 2026-04-24, and it is correct independently of this feature — the
-- commission engine never reads that column. Reverting it would restore a
-- known-stale value. Undo by hand if genuinely wanted:
--     UPDATE customers SET salesperson = '31' WHERE code = '62ค003';
--
-- Triggers and the index are dropped by DROP TABLE.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TABLE IF EXISTS commission_customer_reassign;

COMMIT;
