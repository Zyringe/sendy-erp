"""Card C, P1: `purchase_history` -- one module for a customer's purchase history.

Every expected value below is hand-computed from the fixture in `_shop`, not read
back from the module. The module is wired into nothing yet (P2 does that), so
these pin the definitions in sendy_erp/CONTEXT.md "Customer page" directly.
"""
import datetime as dt

import pytest

from tests._purchase_history_fixture import add_line, mk_product, writeoff

CODE = 'H1'
NAME = 'ร้านทดสอบ'
TODAY = dt.date(2026, 9, 30)


def _shop(conn):
    """One shop, every document kind. Returns (p1, p2, p3).

    IV1 01-10  P1 10/1000  P2 5/500
    IV8 02-15  P1 2/200            (written off, NOT flagged: still a purchase)
    IV3 02-01  P3 6/0              (freebie-only bill)
    IV2 03-10  P1 4/400 + P1 2/0 freebie line
    HS1 04-01  P2 1/100            (cash sale counts like an invoice)
    SR1 05-01  P1 3/300            (credit note)
    IV9 06-01  P1 100/5000         (flagged giveaway: excluded everywhere)
    IV7 06-15  P1 1/50, billed to a หน้าร้าน account under this code
    """
    p1, p2, p3 = (mk_product(conn, 'สินค้า1'), mk_product(conn, 'สินค้า2'),
                  mk_product(conn, 'สินค้า3'))

    def line(doc, date, pid, qty, net, customer=NAME, suffix=1):
        add_line(conn, doc_base=doc, date_iso=date, pid=pid, qty=qty, net=net,
                 customer=customer, code=CODE, suffix=suffix)

    line('IV1', '2026-01-10', p1, 10, 1000)
    line('IV1', '2026-01-10', p2, 5, 500, suffix=2)
    line('IV8', '2026-02-15', p1, 2, 200)
    line('IV3', '2026-02-01', p3, 6, 0)
    line('IV2', '2026-03-10', p1, 4, 400)
    line('IV2', '2026-03-10', p1, 2, 0, suffix=2)
    line('HS1', '2026-04-01', p2, 1, 100)
    line('SR1', '2026-05-01', p1, 3, 300)
    line('IV9', '2026-06-01', p1, 100, 5000)
    line('IV7', '2026-06-15', p1, 1, 50, customer='หน้าร้านS')
    writeoff(conn, 'IV8', 0, CODE)
    writeoff(conn, 'IV9', 1, CODE)
    conn.commit()
    return p1, p2, p3


@pytest.fixture
def shop(empty_db_conn):
    return empty_db_conn, _shop(empty_db_conn)


def _hist(conn, key=CODE, **kw):
    import purchase_history
    return purchase_history.history(conn, key, today=TODAY, **kw)


def test_totals_match_context_definitions(shop):
    conn, _ = shop
    t = _hist(conn)['totals']
    # ยอดซื้อรวม: 1500 + 200 + 400 + 100 - 300 + 50 (IV9 flagged out, SR negated)
    assert t['purchase_total'] == 1950.0
    # จำนวนชิ้น: 15 + 2 + 6(IV3) + 6(IV2) + 1 - 3 + 1
    assert t['qty_total'] == 28
    # จำนวนเอกสาร, raw: IV1 IV8 IV3 IV2 HS1 SR1 IV7 (a credit note IS a document)
    assert t['doc_count'] == 7
    assert (t['first_activity'], t['last_activity']) == ('2026-01-10', '2026-06-15')
    # ครั้งที่ซื้อ: IV1 IV8 IV2 HS1. Not IV3 (freebie), SR1, IV9, IV7 (หน้าร้าน)
    assert t['purchase_count'] == 4
    assert (t['first_purchase'], t['last_purchase']) == ('2026-01-10', '2026-04-01')


def test_purchase_count_and_last_purchase_come_from_one_population(shop):
    conn, _ = shop
    t = _hist(conn)['totals']
    assert t['last_purchase'] == '2026-04-01'
    # the newest document the count counts IS the last purchase: cut the window
    # just before HS1 and both move together
    t2 = _hist(conn, date_to='2026-03-31')['totals']
    assert (t2['purchase_count'], t2['last_purchase']) == (3, '2026-03-10')


def test_window_applies_to_everything_but_winback(shop):
    conn, _ = shop
    full = _hist(conn)
    assert len(full['winback']) >= 1          # control: P1 is flagged all-time
    gone = _hist(conn, date_from='2027-01-01')
    assert gone['totals']['doc_count'] == 0
    assert gone['totals']['purchase_total'] == 0
    assert gone['monthly'] == [] and gone['documents'] == []
    assert gone['products'] == [] and gone['top_products'] == []
    assert gone['returned_net_total'] == 0
    assert gone['winback'] == full['winback']


def test_products_keep_times_bought_on_purchase_half_only(shop):
    conn, (p1, p2, p3) = shop
    rows = {(r['product_id'], r['unit']): r for r in _hist(conn)['products']}
    # P3 was only ever a freebie-only bill: no row (HAVING times_bought > 0)
    assert set(rows) == {(p1, 'ตัว'), (p2, 'ตัว')}
    r1, r2 = rows[(p1, 'ตัว')], rows[(p2, 'ตัว')]
    assert r1['times_bought'] == 3            # IV1 IV8 IV2, not SR1
    assert (r1['returned_qty'], r1['returned_net']) == (3, 300)
    assert (r1['qty'], r1['net']) == (13, 1300)   # 10+2+4 -3 ; 1000+200+400 -300
    assert r2['times_bought'] == 2            # IV1 HS1
    assert (r2['returned_qty'], r2['returned_net']) == (0, 0)
    assert (r2['qty'], r2['net']) == (6, 600)
    assert _hist(conn)['returned_net_total'] == 300


def test_last_purchase_per_product_skips_credit_note_and_freebie(shop):
    conn, (p1, p2, _) = shop
    rows = {r['product_id']: r for r in _hist(conn)['products']}
    # SR1 (05-01) and IV7 (06-15, หน้าร้าน) are later than IV2 but are not purchases;
    # IV2's freebie line is the same document as its paid line
    assert rows[p1]['last_purchase'] == '2026-03-10'
    assert rows[p2]['last_purchase'] == '2026-04-01'


def test_top_products_are_money_ordered_and_net_of_returns(shop):
    conn, (p1, p2, p3) = shop
    top = _hist(conn)['top_products']
    assert [t['product_id'] for t in top] == [p1, p2, p3]
    assert (top[0]['total_qty'], top[0]['total_net']) == (16, 1350)   # IV9 is out


def test_monthly_and_documents(shop):
    conn, _ = shop
    h = _hist(conn)
    assert [(m['month'], m['doc_count'], m['total_net']) for m in h['monthly']] == [
        ('2026-01', 1, 1500), ('2026-02', 2, 200), ('2026-03', 1, 400),
        ('2026-04', 1, 100), ('2026-05', 1, -300), ('2026-06', 1, 50)]
    docs = {d['doc_base']: d for d in h['documents']}
    assert len(docs) == 7 and 'IV9' not in docs
    assert docs['SR1']['is_credit_note'] and docs['SR1']['total'] < 0


def test_histories_total_since_only_bounds_purchase_total(shop):
    conn, _ = shop
    import purchase_history
    all_time = purchase_history.histories(conn)[CODE]
    since = purchase_history.histories(conn, total_since='2026-04-01')[CODE]
    assert all_time['purchase_total'] == 1950.0
    assert since['purchase_total'] == -150.0        # HS1 100 - SR1 300 + IV7 50
    for f in ('doc_count', 'last_activity', 'last_purchase', 'bill_name'):
        assert since[f] == all_time[f]
    assert all_time['doc_count'] == 7
    assert all_time['last_activity'] == '2026-06-15'
    assert all_time['last_purchase'] == '2026-04-01'
    assert all_time['bill_name'] == 'หน้าร้านS'    # the newest row's name (IV7)


def test_key_is_trimmed_and_orphan_is_its_bill_name(empty_db_conn):
    conn = empty_db_conn
    import purchase_history
    p = mk_product(conn, 'x')
    add_line(conn, doc_base='IV1', date_iso='2026-01-01', pid=p, qty=1, net=10,
             customer='ก', code=' K1 ')
    add_line(conn, doc_base='SR1', date_iso='2026-01-02', pid=p, qty=1, net=4,
             customer='ORPHAN', code=None)
    add_line(conn, doc_base='SR2', date_iso='2026-01-03', pid=p, qty=1, net=1,
             customer='ORPHAN', code='')
    add_line(conn, doc_base='IV2', date_iso='2026-01-04', pid=p, qty=1, net=7,
             customer='ก', code='K1')
    conn.commit()
    assert _hist(conn, 'K1')['totals']['purchase_total'] == 17.0    # padded + clean
    orphan = _hist(conn, 'ORPHAN')['totals']
    assert orphan['purchase_total'] == -5.0 and orphan['doc_count'] == 2
    hs = purchase_history.histories(conn)
    assert set(hs) == {'K1', 'ORPHAN'}
    assert hs['K1']['doc_count'] == 2


def test_history_of_an_unknown_key_is_empty_not_an_error(shop):
    conn, _ = shop
    h = _hist(conn, 'NOPE')
    assert h['totals']['doc_count'] == 0 and h['totals']['last_purchase'] is None
    assert h['products'] == [] and h['winback'] == []


def test_module_never_reads_price_evidence_or_ar_writeoffs():
    import inspect
    import purchase_history
    from tests._census import code_only
    code = code_only(inspect.getsource(purchase_history))
    assert 'purchase_population_filter' in code           # control: the strip kept code
    assert 'price_evidence_filter' not in code
    assert 'ar_writeoffs' not in code
