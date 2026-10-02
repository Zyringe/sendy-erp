"""#673 fix round (/interrogate on the PR 1 diff, items 4, 8, 9).

ONE bill-measurement contract, reused by cash conversion, minimum
qualification, last_paid and R6: the bill's raw spelling row first, then its
word, then the tier-implied โหล (12). Before it, a `หล` bill on a product
carrying `หล = 6` (a half-dozen pack) AND `โหล = 12` was costed at 6 pieces per
unit but counted as 12 toward the minimum (Codex), and a `โหล` bill on a
product whose dozen exists only as a `1 โหล` tier was dropped from price
evidence while the minimum was measured with that same tier.

Also: the qty-unknown flag has its own code, and one gate/one text helper.
"""
import os

import pytest

import price_lookup as pl
from models import promotions as promo_models

MIG_199 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'migrations', '199_promo_min_qty.sql')
TODAY = '2026-10-02'
_pid = [987000]
_doc = [9987000]


@pytest.fixture
def db(tmp_db_conn):
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'min_qty' not in cols:
        tmp_db_conn.executescript(open(MIG_199, encoding='utf-8').read())
        tmp_db_conn.commit()
    assert tmp_db_conn.execute(
        "SELECT word FROM unit_map WHERE spelling = 'หล' AND book = 'BSN5657'").fetchone()[0] == 'โหล'
    return tmp_db_conn


def _product(conn, unit_type='ตัว', base=10.0, *, rows=(), tiers=()):
    _pid[0] += 1
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, brand_id, "
        "is_active) VALUES (?, ?, ?, 4, 6, 1)", (f'contract #{_pid[0]}', unit_type, base)).lastrowid
    for u, r in rows:
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                     (pid, u, r))
    for label, price in tiers:
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (pid, label, price))
    conn.commit()
    return pid


def _promo(conn, pid, *, discount_value=25.0, min_qty=None, min_qty_unit=None,
           date_start='2026-06-01'):
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, date_start, "
        "is_active, min_qty, min_qty_unit) VALUES (?, 'c', 'percent', ?, ?, 1, ?, ?)",
        (pid, discount_value, date_start, min_qty, min_qty_unit))
    conn.commit()


def _bill(conn, pid, *, date_iso, qty, unit, price, customer='TST673-MC'):
    _doc[0] += 1
    net = round(price * qty, 2)
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
        (date_iso, f'IV{_doc[0]}-1', f'IV{_doc[0]}', pid, f'ร้าน {customer}', customer, qty,
         unit, price, net, net))
    conn.commit()


def test_measure_ratio_prefers_the_raw_spelling_row(db):
    pid = _product(db, rows=[('หล', 6.0), ('โหล', 12.0)])
    assert pl.measure_ratio(db, pid, 'ตัว', 'หล') == 6.0
    assert pl.measure_ratio(db, pid, 'ตัว', 'โหล') == 12.0
    assert pl.measure_ratio(db, pid, 'ตัว', 'ตัว') == 1.0


def test_measure_ratio_word_then_tier_implied_dozen(db):
    by_word = _product(db, rows=[('โหล', 12.0)])
    assert pl.measure_ratio(db, by_word, 'ตัว', 'หล') == 12.0          # word fallback
    tier_only = _product(db, unit_type='ดอก', tiers=[('1 โหล', 90.0)])
    assert pl.measure_ratio(db, tier_only, 'ดอก', 'โหล') == 12.0       # tier-implied
    assert pl.measure_ratio(db, tier_only, 'ดอก', 'หล') == 12.0
    nothing = _product(db)
    assert pl.measure_ratio(db, nothing, 'ตัว', 'โหล') is None         # never 1.0
    zero = _product(db, rows=[('ลัง', 0.0)])
    assert pl.measure_ratio(db, zero, 'ตัว', 'ลัง') is None


def test_half_dozen_spelling_counts_6_toward_the_minimum(db):
    """Codex: 2 หล on a หล=6 product is 12 pieces, not 24."""
    pid = _product(db, rows=[('หล', 6.0), ('โหล', 12.0)])
    promo = {'promo_type': 'percent', 'discount_value': 5.0, 'min_qty': 20.0,
             'min_qty_unit': 'ตัว', 'bundle_condition': None, 'bundle_buy': None,
             'gift_desc': None}
    assert pl.gate_for_ask(db, pid, 'ตัว', promo, 2, 'หล') == 'not_met'     # 12 < 20
    assert pl.gate_for_ask(db, pid, 'ตัว', promo, 4, 'หล') == 'met'         # 24 >= 20
    assert pl.gate_for_ask(db, pid, 'ตัว', promo, 2, 'โหล') == 'met'        # 24 >= 20


def test_tier_implied_dozen_bill_is_price_and_promo_evidence(db):
    """Codex: a 5 โหล bill at the promo price on a tier-only-dozen product used
    to be dropped by word_ratio, so R6 never saw the promo used and `lowest`
    never saw the bill."""
    pid = _product(db, unit_type='ดอก', base=10.0, tiers=[('1 โหล', 120.0)])
    _promo(db, pid, min_qty=5, min_qty_unit='โหล')
    _bill(db, pid, date_iso='2026-09-20', qty=5, unit='โหล', price=90.0)    # 7.5/ดอก = promo
    out = pl.resolve_price(db, product_id=pid, qty=60, today=TODAY)
    assert out['list']['promo_gate'] == 'met'
    assert out['context']['promo_last_used'] == '2026-09-20'
    assert out['context']['promo_stale'] is False
    assert out['context']['lowest']['cash_per_unit'] == 7.5
    assert out['window']['n_unratioed'] == 0


def test_apply_price_promo_pass_through_is_gone():
    assert not hasattr(pl, 'apply_price_promo')


def test_one_text_helper_names_every_blocking_status():
    statuses = ('not_met', 'qty_unknown', 'unconvertible', 'missing')
    texts = {s: pl.gate_text(s) for s in statuses}
    assert len(set(texts.values())) == 4 and all(texts.values())
    assert 'ยังไม่ระบุจำนวน' in texts['qty_unknown']
    for ok in promo_models.GATE_APPLIES:
        assert pl.gate_text(ok) is None
