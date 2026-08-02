"""The posting-order invariant that WACC's legacy positional pairing rests on.

WHY THIS EXISTS
    Before mig 148, recalculate_product_wacc paired the Nth 'BSN ซื้อ' IN of a
    document with the Nth purchase_transactions row of that document BY
    POSITION. That is only correct while the ledger is posted in source-id
    order, which _sync_bsn_to_stock guarantees with a single `ORDER BY id` on
    its source scan (bsn_sync.py, "ORDER BY id is load-bearing on a REPLAY").

    mig 148 gave new rows an explicit source-line identity, so they no longer
    depend on this. But every legacy row with NULL/NULL provenance still takes
    the positional path, and a ledger rebuild re-posts through the same
    function — so the invariant is still load-bearing and was protected by
    nothing but a code comment.

    Delete the `ORDER BY id` and no existing test fails. This one does.

SCOPE
    This pins POSTING ORDER, not cost. It is deliberately independent of the
    mig-148 identity columns so it keeps guarding the legacy path even if the
    identity design changes.
"""


def _seed_product(conn, name, unit_type='ตัว'):
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES (?, ?)",
        (name, unit_type)).lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
                 (pid,))
    return pid


def _seed_purchase(conn, product_id, doc_no, qty, net, *, bsn_code, line_seq,
                   unit='ตัว', date_iso='2026-04-24'):
    return conn.execute(
        """
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, line_seq, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES (?, ?, ?, ?, ?, 'raw', 'S', 'S1', ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (date_iso, doc_no, product_id, bsn_code, line_seq, qty, unit, net, net),
    ).lastrowid


def test_sync_posts_purchase_ins_in_source_id_order(empty_db_conn):
    """The ledger rows a sync creates must appear in purchase_transactions.id
    order — that equality IS the positional pairing.

    Two documents interleaved, so a bare table scan or any per-document
    grouping would produce a different sequence than a global id sort.
    """
    import models

    pid = _seed_product(empty_db_conn, 'Order Invariant')
    expected = []
    expected.append(_seed_purchase(empty_db_conn, pid, 'RRA', 10, 100.0,
                                   bsn_code='A', line_seq=1))
    expected.append(_seed_purchase(empty_db_conn, pid, 'RRB', 20, 200.0,
                                   bsn_code='B', line_seq=1))
    expected.append(_seed_purchase(empty_db_conn, pid, 'RRA', 30, 300.0,
                                   bsn_code='A', line_seq=2))
    expected.append(_seed_purchase(empty_db_conn, pid, 'RRB', 40, 400.0,
                                   bsn_code='B', line_seq=2))
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    posted = empty_db_conn.execute(
        "SELECT quantity_change FROM transactions"
        " WHERE product_id=? AND note='BSN ซื้อ' ORDER BY id", (pid,)
    ).fetchall()
    source = empty_db_conn.execute(
        "SELECT qty FROM purchase_transactions WHERE product_id=? ORDER BY id",
        (pid,)
    ).fetchall()

    assert [r['quantity_change'] for r in posted] == [r['qty'] for r in source], (
        "ledger must be posted in purchase_transactions.id order — the legacy "
        "positional pairing in recalculate_product_wacc depends on it"
    )


def test_legacy_positional_pairing_still_costs_correctly(empty_db_conn):
    """End-to-end consequence: with provenance stripped (a pre-mig-148 row),
    the positional path must still attach each net to its own quantity.

    The freebie shape is used because it is maximally sensitive — reversing the
    two lines swaps ฿0.00 onto the paid quantity and the whole document's net
    onto a single free unit.
    """
    import models

    pid = _seed_product(empty_db_conn, 'Legacy Pairing')
    _seed_purchase(empty_db_conn, pid, 'RRZ', 23, 1278.22, bsn_code='PAID', line_seq=1)
    _seed_purchase(empty_db_conn, pid, 'RRZ', 1, 0.0, bsn_code='FREE', line_seq=2)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    # Strip provenance so the rows take the LEGACY positional path.
    empty_db_conn.execute(
        "UPDATE transactions SET source_bsn_code=NULL, source_line_seq=NULL"
        " WHERE product_id=?", (pid,))
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    assert abs(wacc - 55.5748) < 1e-3, (
        f"legacy positional pairing produced {wacc}; 1278.22 means the paid "
        f"net was attached to the single free unit"
    )
