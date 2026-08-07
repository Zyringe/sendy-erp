"""The non-destructive readers of synced_to_stock that must skip non-stock rows."""
import models
from models import ecommerce_overview


def test_split_mappings_excludes_non_stock(tmp_db_conn):
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.execute("DELETE FROM purchase_transactions")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง','ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, qty, unit, net, synced_to_stock)"
        " VALUES (1,'2026-06-15','IV9200-1','IV9200',?,'888ค8888',1,'ใบ',30,0)", (pid,))
    conn.commit()

    groups = models.get_pending_split_mappings()
    codes = [g.get('bsn_code') for g in groups] if groups else []
    assert '888ค8888' not in codes, groups


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
