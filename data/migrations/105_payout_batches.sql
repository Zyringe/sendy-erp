-- 105_payout_batches.sql
-- Bank-deposit batch tracker for Shopee marketplace payouts.
--
-- Put receives a weekly Shopee payout deposited to his bank account.
-- This table lets the team record each real bank deposit (deposit_date +
-- deposit_amount) and then match a set of marketplace_orders to it so the
-- total Σ actual_payout ties to the bank transfer before recording
-- รับชำระหนี้ in Express.
--
-- KEY CONSTRAINT: marketplace_orders.settled_at is DATE-ONLY (no time).
-- Because multiple orders can share the same settled_at date and a single
-- bank deposit may split a day across two transfers, the matcher uses
-- deterministic ordering (settled_at ASC, order_sn ASC) and a greedy
-- prefix-sum algorithm. An exact prefix hit assigns; otherwise the UI
-- returns candidates for manual tick selection.
--
-- Apply:
--   sqlite3 /path/to/inventory.db < 105_payout_batches.sql
--
-- Rollback: 105_payout_batches.rollback.sql

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE TABLE payout_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deposit_date   TEXT    NOT NULL,
    deposit_amount REAL    NOT NULL,
    bank_ref       TEXT,
    note           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    created_by     TEXT,
    is_baseline    INTEGER NOT NULL DEFAULT 0
    -- is_baseline=1: a one-time "ยอดยกมา" row that absorbs all pre-tracking
    -- settled orders so the greedy matcher sees only post-baseline orders.
    -- No bank-tie check applies to baseline rows.
);

-- Additive nullable column — no table rebuild required.
ALTER TABLE marketplace_orders ADD COLUMN payout_batch_id INTEGER;

CREATE INDEX idx_marketplace_orders_payout_batch
    ON marketplace_orders(payout_batch_id);

COMMIT;
