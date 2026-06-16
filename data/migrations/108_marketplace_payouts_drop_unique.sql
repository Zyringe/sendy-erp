-- 108_marketplace_payouts_drop_unique.sql
-- Drop UNIQUE(platform, deposit_date, amount) on marketplace_payouts.
--
-- Two DISTINCT bank deposits can share (date, amount) — same-day same-amount
-- transfers do happen (prod 2025-04-01: 2× ฿3,596). deposit_date is date-only,
-- so the mig-107 UNIQUE wrongly rejected the second deposit and aborted the
-- whole reconcile. Idempotency is already guaranteed by reconcile_payouts
-- (DELETE-then-rebuild per platform), so the constraint is redundant + harmful.
-- SQLite can't drop a constraint in place → table rebuild.
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

ALTER TABLE marketplace_payouts RENAME TO marketplace_payouts_old;

CREATE TABLE marketplace_payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    deposit_date TEXT NOT NULL,
    amount       REAL NOT NULL,
    n_orders     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'reconciled',
    source_file  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO marketplace_payouts
    (id, platform, deposit_date, amount, n_orders, status, source_file, created_at)
  SELECT id, platform, deposit_date, amount, n_orders, status, source_file, created_at
  FROM marketplace_payouts_old;

DROP TABLE marketplace_payouts_old;

COMMIT;
