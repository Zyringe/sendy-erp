-- ============================================================================
-- Rollback for 143 — drop commission_customer_reassign.
--
-- ⛔ CODE-COUPLED. Run this ONLY together with (or after) reverting the 143
-- application code. The engine does not degrade gracefully: `commission.py`
-- and `blueprints/accounting.py` reference this table in five queries, so
-- dropping it under the deployed code makes EVERY commission page and the AR
-- customer page fail with:
--     OperationalError: no such table: commission_customer_reassign
-- (verified 2026-07-30 by running this file against a copy of the live DB and
-- then calling get_commission_for_month). An earlier version of this header
-- claimed the engine "falls back to received_payments.salesperson verbatim" —
-- that was wrong, and it was wrong on the emergency path.
--
-- ✅ WANT THE FEATURE OFF *WITHOUT* A DEPLOY? Do NOT drop the table. Neutralise
-- the rules instead — this restores pre-143 BEHAVIOUR with the patched code
-- still running, takes effect on the next page load, and is reversible:
--     UPDATE commission_customer_reassign SET is_active = 0;
-- That is the lever to reach for during an incident. DROP TABLE is for after
-- the code revert, when the feature is genuinely being abandoned.
--
-- Once the code is reverted, dropping the table restores the pre-143 data
-- behaviour: the engine reads `received_payments.salesperson` verbatim again,
-- so the four reassigned customers feed rep 31's base.
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
