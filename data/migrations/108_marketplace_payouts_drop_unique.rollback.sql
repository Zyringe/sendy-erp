-- 108_marketplace_payouts_drop_unique.rollback.sql
-- Re-add UNIQUE(platform, deposit_date, amount). NOTE: this will FAIL if the
-- current data contains same-(date,amount) duplicates (which is exactly why the
-- constraint was dropped) — rollback is a forensic path only.
-- INSERT...SELECT reads from the CURRENT table so rows added after the forward
-- migration survive the rollback.
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
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, deposit_date, amount)
);

INSERT INTO marketplace_payouts
    (id, platform, deposit_date, amount, n_orders, status, source_file, created_at)
  SELECT id, platform, deposit_date, amount, n_orders, status, source_file, created_at
  FROM marketplace_payouts_old;

DROP TABLE marketplace_payouts_old;

COMMIT;
