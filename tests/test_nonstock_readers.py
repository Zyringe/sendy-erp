"""The non-destructive readers of synced_to_stock that must skip non-stock rows."""
import models
from models import ecommerce_overview
from models import reconcile


def test_split_mappings_excludes_non_stock(tmp_db_conn):
    """unit_type='แผง' (pack) + row unit='ตัว' (piece) — NOT the original
    'ตัว'/'ใบ' pairing. get_pending_split_mappings() filters every grouped
    row through cross_unit_hazard() first (mapping.py ~:105) and drops it
    when hazard is None; neither 'ใบ' nor 'ตัว' (the original fixture) is in
    _PIECE_UNITS/_PACK_UNITS (bsn_sync.py:31-32) for that pairing, so that
    row never reached the bsn_code/non_stock predicate at all — the
    assertion passed with ZERO guard code in place. แผง/ตัว clears the
    pack_piece hazard check so the row actually reaches the predicate this
    test means to cover. Do not "tidy" this back to ตัว/ใบ.

    No NULL-bsn_code row here (unlike the ecommerce_overview test below):
    this query's own WHERE has a pre-existing, unrelated `bsn_code IS NOT
    NULL` (mapping.py ~:80/89, predates this fix) — a NULL-bsn_code row can
    never reach this function's output regardless of non_stock_clause, so a
    "NULL row survives" assertion would be false by construction, not a
    guard test."""
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.execute("DELETE FROM purchase_transactions")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง','แผง')"
    ).lastrowid
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, qty, unit, net, synced_to_stock)"
        " VALUES (1,'2026-06-15','IV9200-1','IV9200',?,'888ค8888',1,'ตัว',30,0)", (pid,))

    # Ordinary control, same unit pairing (also clears the hazard gate) so a
    # predicate that wrongly excluded ordinary codes too would be caught.
    ctl_pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('สินค้าปกติ','แผง')"
    ).lastrowid
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, qty, unit, net, synced_to_stock)"
        " VALUES (1,'2026-06-15','IV9201-1','IV9201',?,'C1',1,'ตัว',30,0)", (ctl_pid,))
    conn.commit()

    groups = models.get_pending_split_mappings()
    codes = [g.get('bsn_code') for g in groups] if groups else []
    assert '888ค8888' not in codes, groups
    assert 'C1' in codes, "an ordinary bsn_code must still reach the split-mapping list"


def test_marketplace_sold_ignores_a_discount_line(tmp_db_conn):
    """PROPHYLACTIC guard (design §3.4 #5): measured a no-op on prod today
    because no listing maps to pid 1623. So this test MUST create that listing
    itself — without it the assertion passes whether or not the guard exists."""
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type)"
        " VALUES ('ส่วนลดพิเศษ (Express ZZZ)','ตัว')").lastrowid
    # The mis-mapping this guard exists to survive:
    conn.execute(
        "INSERT INTO platform_skus (platform, product_name, internal_product_id,"
        " qty_per_sale, stock, is_ignored) VALUES ('shopee', 'x', ?, 1, 0, 0)", (pid,))
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, customer, qty, unit, net, synced_to_stock)"
        " VALUES (1,'2026-06-15','IV9300-1','IV9300',?,'ZZZ','หน้าร้านS',500,"
        "'แผ่น',-2500,0)", (pid,))
    conn.commit()

    # CONTROL + carried-forward NULL case: an ordinary mapped product with a
    # NULL bsn_code sale must STILL count as sold. With a bare `NOT IN`, NULL
    # rows evaluate to NULL (falsy) and real marketplace sales vanish from the
    # deduction — understating how much stock moved. That is the money-side
    # failure of NULL semantics, invisible without this row.
    ctl_pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('สินค้าปกติ','ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO platform_skus (platform, product_name, internal_product_id,"
        " qty_per_sale, stock, is_ignored) VALUES ('shopee', 'x', ?, 1, 0, 0)", (ctl_pid,))
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, customer, qty, unit, net, synced_to_stock)"
        " VALUES (1,'2026-06-15','IV9301-1','IV9301',?,NULL,'หน้าร้านS',7,"
        "'ตัว',700,0)", (ctl_pid,))
    conn.commit()

    sold = ecommerce_overview._sold_since_by_pid(
        conn, 'shopee', '2026-01-01', [pid, ctl_pid])

    assert sold.get(pid, 0) == 0, (
        "a 500-qty discount line must never read as 500 units sold")
    assert sold.get(ctl_pid, 0) == 7, (
        "a NULL-bsn_code sale on a mapped product must still count as sold")


def test_reconcile_accepts_a_doc_containing_a_non_stock_line(tmp_db_conn):
    """reconcile.py:530 requires an unsynced row to have NO ledger row, which
    is precisely a non-stock row's state. No code change — this pins it."""
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.execute("DELETE FROM transactions WHERE reference_no LIKE 'IV9400%'")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('สินค้าทดสอบ','ตัว')"
    ).lastrowid

    # Ordinary line: synced, with its matching ledger row (what
    # _sync_bsn_to_stock would have written for a normal 'BSN ขาย' OUT line).
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, customer, qty, unit, unit_price, net,"
        " synced_to_stock) VALUES (1,'2026-06-15','IV9400-1','IV9400',?,"
        "'C1','ลูกค้าทดสอบ',2,'ตัว',100,200,1)", (pid,))
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change,"
        " unit_mode, reference_no, note, created_at) VALUES (?, 'OUT', -2.0,"
        " 'unit', 'IV9400-1', 'BSN ขาย', '2026-06-15 00:00:00')", (pid,))

    # Non-stock line: unsynced, NO ledger row — exactly the state the task-6
    # guard leaves 888ค8888/ZZZ lines in.
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, customer, qty, unit, unit_price, net,"
        " synced_to_stock) VALUES (1,'2026-06-15','IV9400-2','IV9400',?,"
        "'888ค8888','ลูกค้าทดสอบ',1,'ตัว',30,30,0)", (pid,))
    conn.commit()

    payload_rows = reconcile._payload_for_doc(conn, 'IV9400')
    err, matched_ids = reconcile._ledger_check(conn, payload_rows)

    assert err is None, err
