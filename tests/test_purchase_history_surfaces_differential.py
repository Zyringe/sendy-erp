"""Card C, P2: the rewired surfaces vs the SQL they replaced.

The OLD side is `tests/_card_c_oracle.py` (c9ef583, frozen). The synthetic shops
are P1's tie-dense builder, so the same edge cases (same-day SR, freebie-only
bills, flagged and unflagged write-offs, two units, a NULL-code credit note under
a coded shop's bill name) now run through get_customers, get_call_list and the
sales-trip reader.

One difference is BY DESIGN and pinned in its own test at the bottom: a coded
customer with lines billed under a `หน้าร้าน...` name. /call's old spend dropped
those lines row by row while the customer page kept them, so the two disagreed.
The other tests remove that case so they can assert the rest is identical.
"""
import random

import pytest

from tests import _card_c_oracle as oracle
from tests._purchase_history_fixture import add_line, mk_product
from tests.test_purchase_history_differential import _build_shop

SEEDS = list(range(60))


def _shops(conn):
    pids = [mk_product(conn, 'สินค้า%d' % i) for i in range(4)]
    for seed in SEEDS:
        # marketplace=0: /call's old spend dropped a หน้าร้าน-named line under a coded
        # shop (see the last test). Everything else must be identical.
        _build_shop(conn, random.Random(seed), 'R%d' % seed, pids, marketplace=0)
        conn.execute("INSERT INTO customers (code, name, address) VALUES (?,?,?)",
                     ('R%d' % seed, 'หจก.ร้านR%d' % seed, 'ขอนแก่น'))
    conn.commit()


def _close(a, b):
    return a == b or (a is not None and b is not None and abs(a - b) < 1e-9)


@pytest.mark.parametrize('search', [None, 'ร้านR1', 'R2', 'หจก.'])
def test_get_customers_equals_the_frozen_list_sql(empty_db_conn, search):
    import models
    conn = empty_db_conn
    _shops(conn)
    old = oracle.customers_list_rows(conn, search)
    new, total = models.get_customers(search=search, page=1, per_page=10 ** 6)
    assert total == len(old) == len(new) > 0

    old_by = {r['customer_code']: r for r in old}
    new_by = {r['customer_code']: r for r in new}
    assert set(old_by) == set(new_by)
    null_names = {r[0] for r in conn.execute(
        "SELECT DISTINCT customer FROM sales_transactions WHERE customer_code IS NULL")}
    for code, o in old_by.items():
        n = new_by[code]
        for f in ('doc_count', 'total_net', 'last_date', 'last_purchase_date',
                  'salesperson', 'missing_master'):
            assert _close(o[f], n[f]), (search, code, f, o[f], n[f])
        if code is None:
            # SQLite picks the bare bill name of ONE of the tied newest lines
            assert n['customer'] in null_names
        else:
            assert o['customer'] == n['customer'], (search, code)


def test_the_null_code_group_is_still_one_row_summed_over_its_bill_names(empty_db_conn):
    """P1 review N6: histories() splits the NULL-code lines per bill name; this
    list keeps them as ONE row until P4, and the sum must survive the split."""
    import models
    conn = empty_db_conn
    _shops(conn)
    rows, _ = models.get_customers(page=1, per_page=10 ** 6)
    phantom = [r for r in rows if r['customer_code'] is None]
    assert len(phantom) == 1
    (want,) = [r for r in oracle.customers_list_rows(conn) if r['customer_code'] is None]
    assert phantom[0]['doc_count'] == want['doc_count'] == len(SEEDS)
    assert _close(phantom[0]['total_net'], want['total_net'])
    assert want['total_net'] != 0                  # control: the sum is not vacuous


def test_billless_master_rows_are_untouched(empty_db_conn):
    import models
    conn = empty_db_conn
    _shops(conn)
    conn.execute("INSERT INTO customers (code, name, address) VALUES ('NOBILL','ไม่เคยซื้อ','ขอนแก่น')")
    conn.commit()
    rows, _ = models.get_customers(include_billless=True, page=1, per_page=10 ** 6)
    r = [x for x in rows if x['customer_code'] == 'NOBILL'][0]
    assert (r['customer'], r['doc_count'], r['total_net'], r['last_date'],
            r['last_purchase_date']) == ('ไม่เคยซื้อ', 0, 0, None, None)


@pytest.mark.parametrize('window', ['all', '6m', '1y', '2y'])
def test_call_list_spend_and_last_buy_equal_the_frozen_sql(empty_db_conn, window):
    import call_card
    conn = empty_db_conn
    _shops(conn)
    cutoff = call_card._spend_cutoff(window)
    spend, last_buy = oracle.call_spend(conn, cutoff), oracle.call_last_buy(conn)
    rows = call_card.get_call_list(conn, spend_window=window)
    assert len(rows) >= len(SEEDS)
    for r in rows:
        code = r['customer_code']
        assert _close(r['spend'], spend.get(code, 0.0)), (window, code)
        assert r['last_buy'] == last_buy.get(code), (window, code)
    assert any(r['spend'] for r in rows) and any(r['last_buy'] for r in rows)


def test_trip_last_sale_equals_the_frozen_subquery(empty_db_conn):
    import purchase_history
    conn = empty_db_conn
    _shops(conn)
    hist = purchase_history.histories(conn)
    old = oracle.trip_last_sale(conn)
    assert len(old) == len(SEEDS)
    for code, last_sale in old.items():
        assert hist.get(code, {}).get('last_purchase') == last_sale, code
    assert any(old.values())


def test_a_marketplace_named_line_under_a_coded_customer_now_counts_in_call_spend(empty_db_conn):
    """The one BY-DESIGN difference (see the module docstring). /call's spend used
    to drop a line billed as `หน้าร้าน...` under a shop's own code; the customer
    page never did. Now /call spend == the page's ยอดซื้อรวม for the same window."""
    import call_card
    import purchase_history
    conn = empty_db_conn
    p = mk_product(conn, 'x')
    add_line(conn, doc_base='IV1', date_iso='2026-08-01', pid=p, qty=1, net=100,
             customer='ร้านA', code='A1')
    add_line(conn, doc_base='IV2', date_iso='2026-08-02', pid=p, qty=1, net=40,
             customer='หน้าร้านS', code='A1')
    conn.execute("INSERT INTO customers (code, name, address) VALUES ('A1','ร้านA','ขอนแก่น')")
    conn.commit()
    page_total = purchase_history.history(conn, 'A1')['totals']['purchase_total']
    (row,) = [r for r in call_card.get_call_list(conn, spend_window='all')
              if r['customer_code'] == 'A1']
    assert page_total == 140 and row['spend'] == 140
    assert oracle.call_spend(conn)['A1'] == 100        # the old /call figure
