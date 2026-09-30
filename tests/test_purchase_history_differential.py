"""Card C, P1: randomized differential, `purchase_history` vs the code it replaces.

The OLD side is `tests/_card_c_oracle.py`, the per-surface SQL frozen at c9ef583.
It must not be the live `models.customers`: P2 rewires that onto the module, and
a comparison against it would be module-vs-itself (P1 review W3). For a fixed list of seeds, build a tie-dense synthetic
shop (several docs on the same date, an SR on the same day as an IV, freebie-only
bills, flagged and unflagged write-offs, a หน้าร้าน line, a NULL-code SR under a
coded name, two units of one product) and assert OLD == NEW for every field the
module returns, with and without a date window.
"""
import datetime as dt
import random

from tests._purchase_history_fixture import add_line, mk_product, writeoff

SEEDS = list(range(200))
TODAY = dt.date(2026, 9, 30)
DATES = ['2026-01-05', '2026-01-05', '2026-02-10', '2026-03-15', '2026-03-15',
         '2026-05-01', '2026-06-20']
WINDOWS = [(None, None), ('2026-02-01', None), (None, '2026-03-15'),
           ('2026-02-10', '2026-05-01')]


def _build_shop(conn, rng, code, pids):
    name = 'ร้าน%s' % code
    docs = []
    for i in range(rng.randint(3, 10)):
        kind = rng.choice(['IV', 'IV', 'IV', 'HS', 'SR'])
        doc = '%s%s%d' % (kind, code, i)
        docs.append(doc)
        date = rng.choice(DATES)
        for n in range(rng.randint(1, 3)):
            net = rng.choice([0, 0, 50, 120, 400, 999.5])
            qty = rng.choice([1, 2, 6, 12])
            customer = 'หน้าร้านS' if rng.random() < 0.08 else name
            add_line(conn, doc_base=doc, date_iso=date, pid=rng.choice(pids), qty=qty,
                     net=net, customer=customer, code=code, suffix=n + 1,
                     unit=rng.choice(['ตัว', 'ตัว', 'โหล']))
    # a code-less credit note under the same bill name: never part of `code`
    add_line(conn, doc_base='SRNC%s' % code, date_iso='2026-04-04', pid=pids[0], qty=1,
             net=77, customer=name, code=None)
    for doc in docs:
        r = rng.random()
        if r < 0.1:
            writeoff(conn, doc, 1, code)
        elif r < 0.25:
            writeoff(conn, doc, 0, code)


def _old(conn, code, date_from, date_to):
    import winback
    from tests import _card_c_oracle as c
    where, params = c.customer_sales_scope('customer_code', code, date_from, date_to)
    summary, top, monthly, docs = c.customer_sales_aggregates(conn, where, params)
    cards = c.product_rows(conn, where, params)
    wb_where, wb_params = c.customer_sales_scope('customer_code', code, None, None)
    return {
        'summary': dict(summary),
        'top': [dict(r) for r in top],
        'monthly': [dict(r) for r in monthly],
        'docs': docs,
        'cards': cards,
        'returned_net_total': c.returns_off_cards(conn, where, params, []),
        'winback': winback.compute_winback(conn, wb_where, wb_params, today=TODAY),
    }


def _close(a, b):
    return a == b or (a is not None and b is not None and abs(a - b) < 1e-9)


def _same_rows(old, new, msg):
    assert len(old) == len(new), msg
    for o, n in zip(old, new):
        assert o.keys() == n.keys(), msg
        for k in o:
            assert _close(o[k], n[k]), '%s %s: %r != %r' % (msg, k, o[k], n[k])


def _same_top(old, new, msg):
    """top_products: same rows and order by money. Two things in the OLD query are
    plan-dependent and not compared: `unit` (a bare column in a GROUP BY that spans
    units, SQLite returns any row's) and the order among equal `total_net`."""
    key = lambda r: (r['product_id'] is None, r['product_id'] or 0, r['name'])
    strip = lambda rows: [{k: v for k, v in r.items() if k != 'unit'} for r in rows]
    assert [r['total_net'] for r in old] == [r['total_net'] for r in new] or all(
        _close(a['total_net'], b['total_net']) for a, b in zip(old, new)), msg
    _same_rows(sorted(strip(old), key=key), sorted(strip(new), key=key), msg)


def test_history_equals_the_code_it_replaces_on_random_shops(empty_db_conn):
    import price_lookup
    import purchase_history
    from tests import _card_c_oracle as c_oracle
    conn = empty_db_conn
    pids = [mk_product(conn, 'สินค้า%d' % i) for i in range(4)]
    for seed in SEEDS:
        _build_shop(conn, random.Random(seed), 'R%d' % seed, pids)
    conn.commit()

    checked = nonempty_products = nonempty_winback = negative_totals = 0
    for seed in SEEDS:
        code = 'R%d' % seed
        for date_from, date_to in WINDOWS:
            msg = 'seed=%d window=%s..%s' % (seed, date_from, date_to)
            old = _old(conn, code, date_from, date_to)
            new = purchase_history.history(conn, code, date_from, date_to, today=TODAY)

            s = old['summary']
            t = new['totals']
            assert t['doc_count'] == s['doc_count'], msg
            assert _close(t['purchase_total'], s['total_net']), msg
            assert _close(t['qty_total'], s['total_qty']), msg
            assert t['first_activity'] == s['first_date'], msg
            assert t['last_activity'] == s['last_date'], msg
            assert t['purchase_count'] == s['purchase_doc_count'], msg
            # first_purchase has no page-side oracle: an independent MIN over the
            # same population (P1 review W2)
            where, params = c_oracle.customer_sales_scope(
                'customer_code', code, date_from, date_to)
            first = conn.execute(
                'SELECT MIN(date_iso) FROM sales_transactions WHERE %s AND %s'
                % (where, price_lookup.purchase_population_filter('')), params).fetchone()[0]
            assert t['first_purchase'] == first, msg
            assert t['last_purchase'] == s['last_purchase_date'], msg

            _same_rows(old['monthly'], new['monthly'], msg + ' monthly')
            _same_top(old['top'], new['top_products'], msg + ' top')
            _same_rows(old['docs'], new['documents'], msg + ' docs')
            _same_rows(old['winback'], new['winback'], msg + ' winback')
            assert _close(new['returned_net_total'], old['returned_net_total']), msg

            # 4 products x 2 units < 20: the card's top-20 union keeps every row
            cards = sorted(old['cards'], key=lambda c: (c['product_id'], c['unit']))
            assert len(cards) == len(new['products']), msg + ' product rows'
            for c, p in zip(cards, new['products']):
                assert (c['product_id'], c['unit'], c['name']) == \
                       (p['product_id'], p['unit'], p['name']), msg
                assert c['times_bought'] == p['times_bought'], msg
                assert _close(c['total_qty'], p['qty']), msg
                assert _close(c['total_net'], p['net']), msg
                assert _close(c['returned_qty'], p['returned_qty']), msg
                assert _close(c['returned_net'], p['returned_net']), msg

            checked += 1
            nonempty_products += bool(new['products'])
            nonempty_winback += bool(new['winback'])
            negative_totals += t['purchase_total'] < 0
    # the corpus must exercise the branches, or equality is vacuous
    assert checked == len(SEEDS) * len(WINDOWS)
    assert nonempty_products > checked // 2
    assert nonempty_winback >= 10
    assert negative_totals >= 3


def test_histories_equals_history_totals_for_every_key(empty_db_conn):
    import purchase_history
    conn = empty_db_conn
    pids = [mk_product(conn, 'สินค้า%d' % i) for i in range(4)]
    for seed in SEEDS[:60]:
        _build_shop(conn, random.Random(seed), 'R%d' % seed, pids)
    conn.commit()
    hs = purchase_history.histories(conn)
    hs_since = purchase_history.histories(conn, total_since='2026-03-01')
    for seed in SEEDS[:60]:
        code = 'R%d' % seed
        t = purchase_history.history(conn, code, today=TODAY)['totals']
        assert hs[code]['doc_count'] == t['doc_count'], code
        assert _close(hs[code]['purchase_total'], t['purchase_total']), code
        assert hs[code]['last_activity'] == t['last_activity'], code
        assert hs[code]['last_purchase'] == t['last_purchase'], code
        w = purchase_history.history(conn, code, date_from='2026-03-01', today=TODAY)
        assert _close(hs_since[code]['purchase_total'], w['totals']['purchase_total']), code


def test_histories_survives_a_key_committed_between_its_two_reads(empty_db_conn):
    """P1 review W4: the aggregate and the bill-name scan are two reads. A first-ever
    row for a new key landing between them used to raise KeyError."""
    import purchase_history
    conn = empty_db_conn
    pid = mk_product(conn, 'x')
    add_line(conn, doc_base='IV1', date_iso='2026-01-01', pid=pid, qty=1, net=10,
             customer='ก', code='K1')
    conn.commit()

    class Seam:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            if 'ORDER BY s.date_iso, s.id' in sql:      # the second read
                add_line(self.inner, doc_base='IV2', date_iso='2026-01-02', pid=pid,
                         qty=1, net=5, customer='ใหม่', code='NEW1')
            return self.inner.execute(sql, *args)

    hs = purchase_history.histories(Seam(conn))
    assert set(hs) == {'K1'} and hs['K1']['bill_name'] == 'ก'
