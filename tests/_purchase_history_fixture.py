"""Row builders shared by the purchase_history tests (card C, P1).

Plain INSERTs into a schema-only DB (`empty_db_conn`), real SQLite, no mocks.
"""


def mk_product(conn, name, unit_type='ตัว'):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?,?,?,?,1)", (name, unit_type, 100.0, 60.0))
    return cur.lastrowid


def add_line(conn, *, doc_base, date_iso, pid, qty, net, customer, code,
             unit='ตัว', suffix=1, ref_invoice=None, name_raw=None):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, product_name_raw, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, ref_invoice) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, '%s-%d' % (doc_base, suffix), doc_base, pid, name_raw, customer, code,
         qty, unit, (net / qty) if qty else 0, 1, net, net, ref_invoice))


def writeoff(conn, doc_no, excludes_revenue, code=None):
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date, "
        "excludes_revenue) VALUES (?,?,?,?,?,?)",
        (doc_no, code, 0, 'expense', '2026-09-01', excludes_revenue))
