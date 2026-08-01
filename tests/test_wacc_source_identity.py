"""Source-line identity on the stock ledger (mig 148).

WHY THIS EXISTS
    recalculate_product_wacc used to pair the Nth 'BSN ซื้อ' IN of a document
    with the Nth purchase_transactions row of that document BY POSITION
    (wacc.py's pt_by_docno + pt_cursor), computing `unit_cost = net / qty` with
    `net` from the purchase row at the cursor and `qty` from the TRANSACTION.
    When several INs of one document share a created_at, their relative order
    is decided only by transactions.id — a surrogate key that re-imports and
    ledger rebuilds reissue. Reorder them and a quantity gets multiplied
    against another line's net.

    Real shape, pid 988 / RR6700096: lines (qty 23, net ฿1,278.22) and
    (qty 1, net ฿0.00, a แถม freebie). Correct → ฿55.57/piece. Reversed →
    ฿1,278.22/piece, the entire document charged to one unit.

    mig 148 stores which line each IN came from, so the lookup is direct.

PR1 covers the WRITE path only. The read path (wacc.py resolving by identity,
validate-before-mutate, batch pre-flight) is PR2.
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


def _seed_purchase(conn, product_id, doc_no, qty, net, *, bsn_code='BSN001',
                   line_seq=1, unit='ตัว'):
    conn.execute(
        """
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, line_seq, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES ('2026-04-24', ?, ?, ?, ?, 'test', 'sup', 'sup-code',
                ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (doc_no, product_id, bsn_code, line_seq, qty, unit, net, net),
    )


def _seed_sale(conn, product_id, doc_no, qty, net, *, bsn_code='BSN001', unit='ตัว'):
    conn.execute(
        """
        INSERT INTO sales_transactions
            (date_iso, doc_no, product_id, bsn_code, product_name_raw,
             customer, customer_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES ('2026-04-24', ?, ?, ?, 'test', 'cust', 'cust-code',
                ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (doc_no, product_id, bsn_code, qty, unit, net, net),
    )


def test_purchase_sync_records_source_line(empty_db_conn):
    """A 'BSN ซื้อ' IN must carry the identity of the line that created it."""
    import models

    pid = _seed_product(empty_db_conn, "Source Identity Basic")
    _seed_purchase(empty_db_conn, pid, "RR9900001", qty=10, net=100.0,
                   bsn_code='ABC123', line_seq=1)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    row = empty_db_conn.execute(
        "SELECT note, source_bsn_code, source_line_seq FROM transactions"
        " WHERE reference_no='RR9900001'"
    ).fetchone()
    assert row['note'] == 'BSN ซื้อ'
    assert row['source_bsn_code'] == 'ABC123'
    assert row['source_line_seq'] == 1


def test_multi_line_document_records_distinct_lines(empty_db_conn):
    """The pid 988 shape: one document, two lines, one of them a free แถม.

    Both INs share a created_at, so before mig 148 only transactions.id decided
    which net attached to which quantity. Each IN must now carry its OWN line.
    """
    import models

    pid = _seed_product(empty_db_conn, "Freebie Doc")
    _seed_purchase(empty_db_conn, pid, "RR9900002", qty=23, net=1278.22,
                   bsn_code='PAID', line_seq=1)
    _seed_purchase(empty_db_conn, pid, "RR9900002", qty=1, net=0.0,
                   bsn_code='FREE', line_seq=2)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    rows = empty_db_conn.execute(
        "SELECT quantity_change, source_bsn_code, source_line_seq FROM transactions"
        " WHERE reference_no='RR9900002' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    got = {(r['quantity_change'], r['source_bsn_code'], r['source_line_seq'])
           for r in rows}
    assert got == {(23, 'PAID', 1), (1, 'FREE', 2)}, got


def test_same_line_seq_different_bsn_code_stays_distinct(empty_db_conn):
    """The pid 549 / RR6800012 shape — the case a naive line_seq fix gets wrong.

    One document can carry two DIFFERENT bsn_codes that both map to the same
    product, each starting at line_seq = 1, with the same qty but different
    nets (real data: ฿755.25 vs ฿484.50 for qty 2.0). Neither quantity nor
    line_seq alone separates them; only (bsn_code, line_seq) does.
    """
    import models

    pid = _seed_product(empty_db_conn, "Two Codes One Doc")
    _seed_purchase(empty_db_conn, pid, "RR9900003", qty=2, net=755.25,
                   bsn_code='543A8000', line_seq=1)
    _seed_purchase(empty_db_conn, pid, "RR9900003", qty=2, net=484.50,
                   bsn_code='543A9000', line_seq=1)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    rows = empty_db_conn.execute(
        "SELECT source_bsn_code, source_line_seq FROM transactions"
        " WHERE reference_no='RR9900003' ORDER BY id"
    ).fetchall()
    pairs = {(r['source_bsn_code'], r['source_line_seq']) for r in rows}
    assert pairs == {('543A8000', 1), ('543A9000', 1)}, pairs
    assert len(rows) == 2, "both lines must post their own IN"


def test_sales_sync_leaves_source_line_null(empty_db_conn):
    """Sales rows have no line_seq at all — reading one would raise.

    sales_transactions deliberately has no line_seq (mig 091: sales doc_no
    already carries the printed '-N' suffix). The write path must not touch it
    on the sales branch, and the ledger row must stay NULL/NULL so the read
    path treats it as legacy rather than half-linked.
    """
    import models

    pid = _seed_product(empty_db_conn, "Sales No Provenance")
    _seed_purchase(empty_db_conn, pid, "RR9900004", qty=50, net=500.0)
    _seed_sale(empty_db_conn, pid, "IV9900004-1", qty=5, net=75.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    sale = empty_db_conn.execute(
        "SELECT note, source_bsn_code, source_line_seq FROM transactions"
        " WHERE reference_no='IV9900004-1'"
    ).fetchone()
    assert sale['note'] == 'BSN ขาย'
    assert sale['source_bsn_code'] is None
    assert sale['source_line_seq'] is None


def test_purchase_return_leaves_source_line_null(empty_db_conn):
    """GR returns post OUT as 'BSN ซื้อ-คืน' and never enter the WACC purchase
    branch, so they carry no source line."""
    import models

    pid = _seed_product(empty_db_conn, "GR No Provenance")
    _seed_purchase(empty_db_conn, pid, "HP9900005", qty=100, net=1000.0)
    _seed_purchase(empty_db_conn, pid, "GR9900005", qty=30, net=300.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    gr = empty_db_conn.execute(
        "SELECT txn_type, note, source_bsn_code, source_line_seq FROM transactions"
        " WHERE reference_no='GR9900005'"
    ).fetchone()
    assert gr['txn_type'] == 'OUT'
    assert gr['note'] == 'BSN ซื้อ-คืน'
    assert gr['source_bsn_code'] is None
    assert gr['source_line_seq'] is None


def test_no_partial_provenance_written(empty_db_conn):
    """Exactly one of the two columns set is an invalid state the read path
    must never see. Whatever the write path does, it writes both or neither."""
    import models

    pid = _seed_product(empty_db_conn, "Partial Guard")
    _seed_purchase(empty_db_conn, pid, "RR9900006", qty=7, net=70.0)
    _seed_sale(empty_db_conn, pid, "IV9900006-1", qty=2, net=30.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    bad = empty_db_conn.execute(
        "SELECT COUNT(*) c FROM transactions"
        " WHERE (source_bsn_code IS NULL) <> (source_line_seq IS NULL)"
    ).fetchone()['c']
    assert bad == 0, f"{bad} rows have exactly one provenance column set"
