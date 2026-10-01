"""/call list AR badges are keyed by customer code (ADR 0023).

The badge map used to index every balance by name as well as by code, and the
list looked the NAME up first, so two customers who share a name both showed
one of their balances.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from urllib.parse import quote

import lxml.html

NAME = 'ทดสอบ ร้านชื่อซ้ำแบดจ์'
OWES = {'ZZBADGE1': 1111.0, 'ZZBADGE2': 3333.0}
QUIET = 'ZZBADGE3'      # same name, owes nothing chaseable


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    try:
        snap, batch_id = conn.execute(
            "SELECT snapshot_date_iso, batch_id FROM express_ar_outstanding"
            " WHERE entity='BSN' ORDER BY snapshot_date_iso DESC LIMIT 1").fetchone()
        for code in (*OWES, QUIET):
            conn.execute("DELETE FROM customers WHERE code = ?", (code,))
            conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (code,))
            conn.execute("DELETE FROM express_ar_outstanding WHERE TRIM(customer_code) = ?",
                         (code,))
            conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (code, NAME))
            # /call lists customers with at least one sales row.
            conn.execute("""INSERT INTO sales_transactions
                              (date_iso, doc_no, doc_base, customer, customer_code,
                               qty, unit, unit_price, vat_type, total, net)
                            VALUES ('2026-07-01', ?, ?, ?, ?, 1, 'ตัว', 10, 1, 10, 10)""",
                         (f'HS-{code}-1', f'HS-{code}', NAME, code))
        for code, amount in OWES.items():
            conn.execute("""
                INSERT INTO express_ar_outstanding
                    (batch_id, snapshot_date_iso, customer_code, customer_name, doc_no,
                     doc_date_iso, is_anomalous, bill_amount, paid_amount,
                     outstanding_amount, entity)
                VALUES (?, ?, ?, ?, ?, '2026-07-01', 0, ?, 0, ?, 'BSN')
            """, (batch_id, snap, code, NAME, f'IV-{code}', amount, amount))
        conn.commit()
    finally:
        conn.close()


def _badges(html):
    out = {}
    for tr in lxml.html.fromstring(html).xpath("//tr[td[@class='cc-cust']]"):
        code = tr.xpath("string(.//div[@class='cd'])").strip()
        badge = tr.xpath("string(.//span[contains(@class, 'cc-b ar')])").strip()
        out[code] = (float(badge.replace('⚠️', '').strip().lstrip('฿').replace(',', ''))
                     if badge else None)
    return out


def test_two_customers_sharing_a_name_each_show_their_own_balance(tmp_db):
    _seed(tmp_db)
    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
    html = c.get(f'/call?q={quote(NAME)}').get_data(as_text=True)

    badges = _badges(html)
    assert set(badges) >= {*OWES, QUIET}, badges
    for code, amount in OWES.items():
        assert badges[code] == amount, (code, badges)
    assert badges[QUIET] is None, badges
