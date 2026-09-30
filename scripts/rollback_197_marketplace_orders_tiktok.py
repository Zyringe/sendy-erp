"""Roll back migration 197: marketplace_orders back to CHECK(platform IN ('shopee','lazada')).

WHY A SCRIPT, NOT A .rollback.sql. Under the sqlite3 CLI a failing statement
does not stop the script: a tiktok row makes the INSERT ... SELECT fail its
CHECK, the CLI carries on, DROPs marketplace_orders and COMMITs, and every
order is gone (reproduced during /interrogate, 2026-09-30). So this refuses
before BEGIN if any row the old CHECK would reject exists, and asserts the
row, line and sequence counts before COMMIT. Deleting tiktok rows to make the
rollback pass is a decision for whoever runs it, not for this script.

The rebuild mirrors the forward migration: foreign_keys OFF before BEGIN
(marketplace_order_items cascades on delete), explicit column list with id,
sqlite_sequence.seq restored, the 4 indexes recreated. The
applied_migrations row for 197 goes in the same transaction.

    python3 scripts/rollback_197_marketplace_orders_tiktok.py --db /path/to/copy.db
"""
import argparse
import sqlite3
import sys

MIGRATION = '197_marketplace_orders_tiktok.sql'
COLUMNS = ('id, platform, order_sn, status, buyer_name, buyer_phone, ship_address, '
           'order_date, paid_date, item_total, marketplace_fee, payout, currency, '
           'source_file, raw_json, first_synced_at, last_synced_at, '
           'actual_payout, settled_at, settlement_source, payout_batch_id, payout_id')

CREATE_OLD = """CREATE TABLE marketplace_orders_old (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    order_sn         TEXT    NOT NULL,        -- Shopee order_sn / Lazada order number
    status           TEXT,                    -- marketplace order status
    buyer_name       TEXT,
    buyer_phone      TEXT,
    ship_address     TEXT,
    order_date       TEXT,                    -- ISO; order create time
    paid_date        TEXT,                    -- ISO; settlement/payment time (nullable until settled)
    item_total       REAL,                    -- sum of line subtotals, pre-fee
    marketplace_fee  REAL,                    -- หักค่าบริการ (nullable until settled)
    payout           REAL,                    -- ยอดรวมหลังหักค่าคอม (nullable until settled)
    currency         TEXT    NOT NULL DEFAULT 'THB',
    source_file      TEXT,                    -- export filename this row was imported from
    raw_json         TEXT,                    -- full export row(s) for forensics
    first_synced_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_synced_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    actual_payout    REAL,
    settled_at       TEXT,
    settlement_source TEXT,
    payout_batch_id  INTEGER,
    payout_id        INTEGER,
    UNIQUE(platform, order_sn)
)"""

# Same text as the forward migration and the live schema, so sqlite_master matches.
INDEXES = (
    "CREATE INDEX idx_marketplace_orders_date\n    ON marketplace_orders(platform, order_date DESC)",
    "CREATE INDEX idx_marketplace_orders_status\n    ON marketplace_orders(status)",
    "CREATE INDEX idx_marketplace_orders_payout_batch\n    ON marketplace_orders(payout_batch_id)",
    "CREATE INDEX idx_marketplace_orders_payout_id ON marketplace_orders(payout_id)",
)


def _counts(conn):
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='marketplace_orders'").fetchone()
    return (conn.execute("SELECT COUNT(*) FROM marketplace_orders").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM marketplace_order_items").fetchone()[0],
            seq[0] if seq else None)


def rollback(conn):
    """Rebuild marketplace_orders with the two-platform CHECK. Raises SystemExit
    before touching anything if a row would not fit, or if `conn` is mid-transaction."""
    if conn.in_transaction:
        sys.exit('refused: the connection already has an open transaction')
    blocked = conn.execute("SELECT platform, COUNT(*) FROM marketplace_orders "
                           "WHERE platform NOT IN ('shopee','lazada') GROUP BY platform").fetchall()
    if blocked:
        sys.exit('refused: rows the old CHECK rejects: '
                 + ', '.join(f'{p}={n}' for p, n in blocked)
                 + '. Decide what happens to them first; this script deletes nothing.')

    before = _counts(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS marketplace_orders_old")
        conn.execute(CREATE_OLD)
        conn.execute(f"INSERT INTO marketplace_orders_old ({COLUMNS}) "
                      f"SELECT {COLUMNS} FROM marketplace_orders")
        conn.execute("DROP TABLE marketplace_orders")
        conn.execute("ALTER TABLE marketplace_orders_old RENAME TO marketplace_orders")
        # The rebuild's INSERT leaves seq at MAX(id), or 0 when there were no rows.
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'marketplace_orders'")
        if before[2] is not None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('marketplace_orders', ?)",
                         (before[2],))
        for sql in INDEXES:
            conn.execute(sql)
        conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIGRATION,))
        after = _counts(conn)
        if after != before:
            raise RuntimeError(f'counts changed (orders, items, seq): {before} -> {after}')
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return after


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', required=True)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        orders, items, seq = rollback(conn)
    finally:
        conn.close()
    # Independent re-read on a fresh connection.
    check = sqlite3.connect(args.db)
    try:
        ddl = check.execute("SELECT sql FROM sqlite_master WHERE name='marketplace_orders'").fetchone()[0]
        assert "'tiktok'" not in ddl, 'the CHECK still names tiktok'
    finally:
        check.close()
    print(f'rolled back 197: {orders} orders, {items} lines, seq {seq}')


if __name__ == '__main__':
    main()
