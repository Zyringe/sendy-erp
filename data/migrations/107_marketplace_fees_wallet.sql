-- 107_marketplace_fees_wallet.sql
-- Per-order Shopee fee breakdown + seller-balance wallet ledger + derived
-- bank-deposit (payout) table. Supersedes the manual payout_batches matcher
-- (mig 105) for reconciliation; payout_batches is left in place (deprecated).
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE TABLE marketplace_order_fees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    order_sn        TEXT NOT NULL,
    item_value      REAL,
    fee_commission  REAL DEFAULT 0,
    fee_service     REAL DEFAULT 0,
    fee_transaction REAL DEFAULT 0,
    fee_platform    REAL DEFAULT 0,
    fee_ads_escrow  REAL DEFAULT 0,
    fee_tax         REAL DEFAULT 0,
    shipping_net    REAL DEFAULT 0,
    fee_saver       REAL DEFAULT 0,
    fee_total       REAL,
    net_payout      REAL,
    fee_pct         TEXT,
    fee_raw_json    TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, order_sn)
);

CREATE TABLE marketplace_wallet_txns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    txn_time        TEXT NOT NULL,
    txn_type        TEXT NOT NULL,          -- income | withdrawal | adjustment
    order_sn        TEXT,
    amount          REAL NOT NULL,
    running_balance REAL,
    description     TEXT,
    source_file     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, txn_time, txn_type, order_sn, amount)
);

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

ALTER TABLE marketplace_orders ADD COLUMN payout_id INTEGER;
CREATE INDEX idx_marketplace_orders_payout_id ON marketplace_orders(payout_id);
CREATE INDEX idx_wallet_txns_platform_time ON marketplace_wallet_txns(platform, txn_time, id);

COMMIT;
