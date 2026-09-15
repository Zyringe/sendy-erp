-- data/migrations/182_cashbook_payout_mirror.sql
--
-- Issue #533: mirror Shopee/Lazada marketplace_payouts into locked cashbook
-- income rows on LEX (Lazada) / SPX (Shopee), so nobody hand-keys them.
--
-- marketplace_payouts is DELETED and REBUILT on every reconcile (its `id` is
-- not stable — see marketplace_reconcile.py), so a cashbook row cannot FK to
-- a payout row. Instead the natural key that identifies a payout
-- (platform + deposit_date + amount, plus an occurrence number when two
-- payouts share all three — see 108_marketplace_payouts_drop_unique.sql for
-- why duplicates are real) is stored directly on the cashbook row. A row is
-- "payout-sourced" (locked, mirror-owned) iff payout_platform IS NOT NULL —
-- same idea as payroll_item_id / salary_advance_id / commission_payout_id,
-- just keyed by value instead of by id.
--
-- The partial UNIQUE index is a DB-level backstop against the mirror (or a
-- concurrent gunicorn worker) ever inserting the same payout twice; it does
-- NOT constrain ordinary manual rows (payout_platform IS NULL there, and
-- SQLite treats every NULL as distinct so they're outside the index).

BEGIN;

ALTER TABLE cashbook_transactions ADD COLUMN payout_platform TEXT;
ALTER TABLE cashbook_transactions ADD COLUMN payout_deposit_date TEXT;
ALTER TABLE cashbook_transactions ADD COLUMN payout_amount REAL;
ALTER TABLE cashbook_transactions ADD COLUMN payout_occurrence INTEGER;

CREATE UNIQUE INDEX idx_cashbook_txn_payout_natural_key
    ON cashbook_transactions(payout_platform, payout_deposit_date,
                              payout_amount, payout_occurrence)
    WHERE payout_platform IS NOT NULL;

COMMIT;
