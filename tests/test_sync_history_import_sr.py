"""history_import compensator direction bug in models._sync_bsn_to_stock.

`batch_id == 'history_import'` is a legacy sentinel string used by an old
bulk-historical-load path (predates migration 009 / import_log's FK). Rows in
such a batch get a compensating leg inserted so historical data nets to ZERO
against current stock. The old compensator always inserted a blind
IN +base_qty leg, silently assuming every row in the batch was a normal sale
(row_txn_type OUT). An SR (sales-return) row's PRIMARY leg already posts
IN +base_qty (see test_sync_sales_returns.py) — so the compensator's blind
+IN doubled it to +2×base_qty instead of netting zero.

Fix: the compensator must reverse the row's ACTUAL leg (row_txn_type + change),
not assume it is always a sale.

Note: `batch_id` is declared `INTEGER REFERENCES import_log(id)`, but the
legacy 'history_import' string predates that FK (see migration 009's comment:
sales_transactions had 453 such orphan rows). Foreign-key enforcement (ON by
default in these fixtures) would reject a literal string insert today, so the
seed helper below disables it for that INSERT to reproduce the legacy shape.
"""


def _seed_product(conn, name, unit_type='ตัว', cost_price=0.0):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, ?)",
        (name, unit_type, cost_price),
    )
    pid = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
        (pid,),
    )
    return pid


def _seed_sale(conn, product_id, doc_no, qty, unit='ตัว', *, net, batch_id=None, customer='ร้านทดสอบ'):
    conn.execute(
        """
        INSERT INTO sales_transactions
            (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
             customer, customer_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES (?, '2026-04-24', ?, ?, ?, 'BSN001', 'test', ?, 'c-code',
                ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (batch_id, doc_no, doc_no.split('-')[0], product_id, customer, qty, unit, net, net),
    )


def test_history_import_normal_sale_nets_zero(empty_db_conn):
    """Guard existing behavior: a normal sale row in a history_import batch
    still gets its IN compensator and nets to 0 stock effect."""
    import models

    # PRAGMA foreign_keys can only be toggled outside a transaction — do it
    # before any INSERT on this connection.
    empty_db_conn.execute("PRAGMA foreign_keys = OFF")
    pid = _seed_product(empty_db_conn, "History Normal Sale")
    _seed_sale(empty_db_conn, pid, "IV9700001-1", qty=40, net=400.0, batch_id='history_import')
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    stock = empty_db_conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()['quantity']
    assert stock == 0, f"history_import normal sale must net to 0 stock, got {stock}"


def test_history_import_sr_nets_zero(empty_db_conn):
    """The bug: an SR row inside a history_import batch posts IN +base_qty as
    its primary leg. The compensator must reverse THAT leg (OUT -base_qty),
    not blindly add another IN — otherwise stock nets to +2×base_qty instead
    of 0."""
    import models

    empty_db_conn.execute("PRAGMA foreign_keys = OFF")
    pid = _seed_product(empty_db_conn, "History SR")
    _seed_sale(empty_db_conn, pid, "SR9700002-1", qty=25, net=250.0, batch_id='history_import')
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    stock = empty_db_conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()['quantity']
    assert stock == 0, f"history_import SR must net to 0 stock, got {stock}"


def test_non_history_sr_still_nets_positive(empty_db_conn):
    """Regression guard: an SR row OUTSIDE a history_import batch must still
    raise stock by +qty (no compensator fires for live/current batches)."""
    import models

    pid = _seed_product(empty_db_conn, "Live SR")
    _seed_sale(empty_db_conn, pid, "SR9700003-1", qty=15, net=150.0, batch_id=None)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    stock = empty_db_conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()['quantity']
    assert stock == 15, f"live SR should raise stock by +15, got {stock}"
