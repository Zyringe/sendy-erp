"""#673 — the minimum-quantity gate on a price promo (Put 2026-10-02, option A).

Two layers, tested separately:
  A. models.promotions.promo_gate / promo_price — pure, no DB. The gate decides
     whether a promo's PRICE effect applies; promo_price asks it every time, and
     `qty_pieces` / `min_pieces` are REQUIRED keywords so a call site that never
     thought about quantity is a TypeError, not a silent discount.
  B. price_lookup.promo_min_measure — the one unit chain that turns (qty, unit)
     and (min_qty, min_qty_unit) into one measure: same หน่วย word on both sides →
     compared as written; otherwise both through the resolver's chain (a
     unit_conversions row, else the tier-implied โหล = 12). Never the unknown → 1.0
     fallback, never a ratio of 0.

The product shapes in B are the real backfill shapes from the plan
(projects/promo-min-qty/promo-min-qty-plan.md, "Backfill table"), forced on
throwaway products — never inherited from the clone.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import price_lookup as pl
from models import promotions as promo_models

MIG_199 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'migrations', '199_promo_min_qty.sql')


def _promo(promo_type='percent', discount_value=5.0, *, min_qty=None, min_qty_unit=None,
           bundle_condition=None, bundle_buy=None, bundle_free=None, gift_desc=None):
    return {
        'promo_type': promo_type, 'discount_value': discount_value,
        'min_qty': min_qty, 'min_qty_unit': min_qty_unit,
        'bundle_condition': bundle_condition, 'bundle_buy': bundle_buy,
        'bundle_free': bundle_free, 'gift_desc': gift_desc, 'gift_qty': None,
    }


# ── A · promo_gate ────────────────────────────────────────────────────────────

def gate(promo, qty_pieces, min_pieces):
    return promo_models.promo_gate(promo, qty_pieces=qty_pieces, min_pieces=min_pieces)


def test_gate_none_without_a_minimum():
    assert gate(_promo(), 1, None) == 'none'
    assert gate(_promo(), None, None) == 'none'     # no qty needed when nothing is gated
    assert gate(None, 1, None) == 'none'


def test_gate_met_and_not_met_at_the_boundary():
    p = _promo(min_qty=5, min_qty_unit='โหล')
    assert gate(p, 59, 60) == 'not_met'
    assert gate(p, 60, 60) == 'met'
    assert gate(p, 61, 60) == 'met'


def test_gate_compares_at_4_dp():
    p = _promo(min_qty=5, min_qty_unit='โหล')
    assert gate(p, 59.99999, 60) == 'met'           # float noise is not a shortfall
    assert gate(p, 59.9999, 60) == 'not_met'        # control: a real 1e-4 shortfall is


def test_gate_unconvertible_when_the_minimum_has_no_measure():
    assert gate(_promo(min_qty=1, min_qty_unit='ลัง'), 100, None) == 'unconvertible'


def test_gate_qty_unknown_when_the_ask_has_no_measure():
    assert gate(_promo(min_qty=20, min_qty_unit='อัน'), None, 20) == 'qty_unknown'


def test_gate_missing_when_a_label_has_no_number():
    """Put 2026-10-02: a bare ยกลัง/ยกล่อง with no number is never a valid gated
    promo — fail closed, list price."""
    assert gate(_promo(bundle_condition='ยกลัง'), 1000, None) == 'missing'


def test_gate_ignores_promos_with_no_price_effect():
    bundle = _promo('bundle', None, bundle_buy=10, bundle_free=1, bundle_condition='ยกลัง')
    assert gate(bundle, 1, None) == 'none'


# ── A · promo_price ───────────────────────────────────────────────────────────

def test_promo_price_requires_the_quantity_keywords():
    with pytest.raises(TypeError):
        promo_models.promo_price(100.0, 1.0, _promo())                       # noqa
    with pytest.raises(TypeError):
        promo_models.promo_price(100.0, 1.0, _promo(), qty_pieces=1)         # noqa


@pytest.mark.parametrize('qty_pieces,min_pieces,expected', [
    (19, 20, 100.0),     # not met → list
    (20, 20, 95.0),      # met → the whole line discounted
    (None, 20, 100.0),   # qty unknown → list
    (20, None, 100.0),   # unconvertible → list
])
def test_promo_price_applies_only_when_met(qty_pieces, min_pieces, expected):
    p = _promo(min_qty=20, min_qty_unit='อัน')
    assert promo_models.promo_price(100.0, 1.0, p, qty_pieces=qty_pieces,
                                    min_pieces=min_pieces) == expected


def test_promo_price_label_without_number_is_list():
    p = _promo(bundle_condition='ยกลัง')
    assert promo_models.promo_price(100.0, 1.0, p, qty_pieces=10_000, min_pieces=None) == 100.0


def test_promo_price_ungated_is_unchanged_whatever_the_qty():
    p = _promo()
    for q in (None, 0, 1, 1000):
        assert promo_models.promo_price(100.0, 1.0, p, qty_pieces=q, min_pieces=None) == 95.0


def test_gated_fixed_promo_met_and_not_met():
    p = _promo('fixed', 40.0, min_qty=2, min_qty_unit='โหล')
    assert promo_models.promo_price(600.0, 12.0, p, qty_pieces=24, min_pieces=24) == 480.0
    assert promo_models.promo_price(600.0, 12.0, p, qty_pieces=12, min_pieces=24) == 600.0


# ── B · promo_min_measure (DB) ───────────────────────────────────────────────

@pytest.fixture
def db(tmp_db_conn):
    cols = {r['name'] for r in tmp_db_conn.execute("PRAGMA table_info(promotions)")}
    if 'min_qty' not in cols:
        tmp_db_conn.executescript(open(MIG_199, encoding='utf-8').read())
        tmp_db_conn.commit()
    return tmp_db_conn


_pid = [973000]


def _product(conn, unit_type, *, rows=(), tiers=()):
    _pid[0] += 1
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, ?, 10, 5, 1)", (f'gate #{_pid[0]}', unit_type)).lastrowid
    for unit, ratio in rows:
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                     (pid, unit, ratio))
    for label, price in tiers:
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (pid, label, price))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM unit_conversions WHERE product_id=?",
                        (pid,)).fetchone()[0] == len(rows)
    return pid


def measure(conn, pid, unit_type, promo, qty, unit):
    return pl.promo_min_measure(conn, pid, unit_type, promo, qty, unit)


def status(conn, pid, unit_type, promo, qty, unit):
    q, m = measure(conn, pid, unit_type, promo, qty, unit)
    return promo_models.promo_gate(promo, qty_pieces=q, min_pieces=m)


def test_pid307_shape_tier_implied_dozen(db):
    """ดอก product, NO โหล row, only a '1 โหล' tier: the tier-implied 12 converts
    '5 โหล' to 60 ดอก (plan's ⚠ on pid 307)."""
    pid = _product(db, 'ดอก', tiers=[('1 โหล', 300.0)])
    p = _promo(discount_value=25.0, min_qty=5, min_qty_unit='โหล')
    assert measure(db, pid, 'ดอก', p, 59, 'ดอก') == (59.0, 60.0)
    assert status(db, pid, 'ดอก', p, 59, 'ดอก') == 'not_met'
    assert status(db, pid, 'ดอก', p, 60, 'ดอก') == 'met'
    assert status(db, pid, 'ดอก', p, 5, 'โหล') == 'met'          # same word: as written


def test_pid307_shape_without_the_tier_is_unconvertible(db):
    """CONTROL for the test above: the same product minus its tier has no path
    from โหล to ดอก, so the gate must fail closed rather than read 5 โหล as 5."""
    pid = _product(db, 'ดอก')
    p = _promo(discount_value=25.0, min_qty=5, min_qty_unit='โหล')
    assert measure(db, pid, 'ดอก', p, 60, 'ดอก') == (60.0, None)
    assert status(db, pid, 'ดอก', p, 6000, 'ดอก') == 'unconvertible'


def test_macoh_shape_min_in_the_base_unit(db):
    pid = _product(db, 'อัน', rows=[('ตัว', 1.0)])
    p = _promo(min_qty=20, min_qty_unit='อัน')
    assert status(db, pid, 'อัน', p, 19, 'อัน') == 'not_met'
    assert status(db, pid, 'อัน', p, 20, 'อัน') == 'met'
    assert status(db, pid, 'อัน', p, 20, 'ตัว') == 'met'          # via the ตัว=1 row


def test_x66_200ml_shape_row_dozen(db):
    pid = _product(db, 'ตัว', rows=[('โหล', 12.0)], tiers=[('1 โหล', 670.0)])
    p = _promo(discount_value=3.0, min_qty=3, min_qty_unit='โหล')
    assert measure(db, pid, 'ตัว', p, 35, 'ตัว') == (35.0, 36.0)
    assert status(db, pid, 'ตัว', p, 35, 'ตัว') == 'not_met'
    assert status(db, pid, 'ตัว', p, 36, 'ตัว') == 'met'
    assert status(db, pid, 'ตัว', p, 3, 'โหล') == 'met'
    assert status(db, pid, 'ตัว', p, 2, 'โหล') == 'not_met'


def test_600ml_shape_base_unit_is_the_dozen(db):
    pid = _product(db, 'โหล', rows=[('โหล', 1.0)])
    p = _promo(discount_value=3.0, min_qty=2, min_qty_unit='โหล')
    assert status(db, pid, 'โหล', p, 1, 'โหล') == 'not_met'
    assert status(db, pid, 'โหล', p, 2, 'โหล') == 'met'


def test_unknown_ask_unit_never_reads_as_ratio_1(db):
    """_resolve_unit(strict=False) answers 1.0 for a unit it knows nothing about;
    the gate must not: 'กล่อง' with no row is NOT one ตัว."""
    pid = _product(db, 'ตัว', rows=[('โหล', 12.0)])
    p = _promo(min_qty=3, min_qty_unit='โหล')
    q, m = measure(db, pid, 'ตัว', p, 100, 'กล่อง')
    assert (q, m) == (None, 36.0)
    assert status(db, pid, 'ตัว', p, 100, 'กล่อง') == 'qty_unknown'


def test_a_ratio_of_zero_is_no_ratio(db):
    pid = _product(db, 'ตัว', rows=[('ลัง', 0.0)])
    p = _promo(min_qty=1, min_qty_unit='ลัง')
    assert status(db, pid, 'ตัว', p, 10_000, 'ตัว') == 'unconvertible'


def test_no_quantity_is_qty_unknown(db):
    pid = _product(db, 'อัน')
    p = _promo(min_qty=20, min_qty_unit='อัน')
    assert status(db, pid, 'อัน', p, None, 'อัน') == 'qty_unknown'


def test_spelling_variants_meet_as_one_word(db):
    """ADR 0018: a minimum stored in the word and an ask in a variant spelling
    ('หล') are the same หน่วย — compared as written, no ratio needed."""
    pid = _product(db, 'ตัว')
    p = _promo(min_qty=3, min_qty_unit='โหล')
    assert status(db, pid, 'ตัว', p, 3, 'หล') == 'met'


# ── effective_price: no quantity → a gated promo is not the price ────────────

def test_effective_price_ignores_a_gated_promo(db):
    pid = _product(db, 'อัน')
    db.execute("UPDATE products SET base_sell_price = 100 WHERE id = ?", (pid,))
    db.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
               "is_active, min_qty, min_qty_unit) VALUES (?, 'g', 'percent', 5, 1, 20, 'อัน')",
               (pid,))
    db.commit()
    assert promo_models.effective_price({'id': pid, 'base_sell_price': 100.0}, conn=db) == 100.0
    # control: the same promo without its minimum is the discounted price
    db.execute("UPDATE promotions SET min_qty = NULL, min_qty_unit = NULL WHERE product_id = ?",
               (pid,))
    db.commit()
    assert promo_models.effective_price({'id': pid, 'base_sell_price': 100.0}, conn=db) == 95.0
