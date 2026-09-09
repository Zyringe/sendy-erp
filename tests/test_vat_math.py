"""
VAT-math smoke tests.

Convention used throughout models.py (see e.g. lines 1445, 1490, 1517):

    CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END

vat_type=1 → net is VAT-inclusive (use as-is)
vat_type=2 → net is VAT-exclusive (add 7%)
"""
import sqlite3

import vat_math


def _seed_sale(conn, *, doc_no, doc_base, vat_type, net, customer='C1',
               customer_code='C001', date_iso='2026-04-01'):
    conn.execute("""
        INSERT INTO sales_transactions
            (date_iso, doc_no, doc_base, bsn_code, product_name_raw,
             customer, customer_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES (?, ?, ?, 'X', 'x', ?, ?, 1.0, 'ตัว', ?, ?, '', ?, ?, 0)
    """, (date_iso, doc_no, doc_base, customer, customer_code,
          net, vat_type, net, net))


def _grossed(conn, *, doc_base):
    """Run the app's OWN VAT math against the seeded rows.

    ⚠ This used to re-type the SQL here. It therefore checked its own typing:
    deleting `* 1.07` from a production query left all four tests below green.
    Importing vat_math is what makes them able to fail (card 2, 2026-09-09)."""
    return conn.execute(
        f"SELECT SUM({vat_math.cash_sql()}) AS g "
        "FROM sales_transactions WHERE doc_base = ?",
        (doc_base,)).fetchone()['g']


def test_vat_type_1_does_not_add_vat(empty_db_conn):
    _seed_sale(empty_db_conn, doc_no='IV1-1', doc_base='IV1', vat_type=1, net=1000.0)
    empty_db_conn.commit()
    g = _grossed(empty_db_conn, doc_base='IV1')
    assert g == 1000.0


def test_vat_type_2_adds_seven_percent(empty_db_conn):
    _seed_sale(empty_db_conn, doc_no='IV2-1', doc_base='IV2', vat_type=2, net=1000.0)
    empty_db_conn.commit()
    g = _grossed(empty_db_conn, doc_base='IV2')
    assert abs(g - 1070.0) < 1e-6


def test_vat_mixed_lines_in_same_doc(empty_db_conn):
    """A doc with both vat_type=1 and vat_type=2 lines computes line-by-line."""
    _seed_sale(empty_db_conn, doc_no='IV3-1', doc_base='IV3', vat_type=1, net=500.0)
    _seed_sale(empty_db_conn, doc_no='IV3-2', doc_base='IV3', vat_type=2, net=500.0)
    empty_db_conn.commit()
    g = _grossed(empty_db_conn, doc_base='IV3')
    # 500 + 500*1.07 = 1035
    assert abs(g - 1035.0) < 1e-6


def test_vat_zero_unaffected(empty_db_conn):
    """vat_type=0 (purchase reports use 0) should fall through ELSE branch."""
    _seed_sale(empty_db_conn, doc_no='IV4-1', doc_base='IV4', vat_type=0, net=200.0)
    empty_db_conn.commit()
    g = _grossed(empty_db_conn, doc_base='IV4')
    assert g == 200.0
