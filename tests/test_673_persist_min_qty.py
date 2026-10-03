"""#673 — writing a promo's minimum quantity.

create_promotion / replace_promotion persist min_qty + min_qty_unit (the unit
as its หน่วย word, like bundle_unit). The /products/<id>/promotions/new route
parses both and refuses, before anything is written:
  - a quantity without a unit, or a number that is not > 0 (a unit with no
    quantity is "no minimum" at the route since PR 2: the form's unit
    <select> always posts one);
  - a minimum on a promo that does not change the price (bundle / gift, or a
    row that also carries buy-N-get-M / a gift);
  - a ยกลัง/ยกล่อง label on a price promo with no number (Put 2026-10-02: never
    a valid state; the writers refuse it, the route no longer reads a label);
  - a unit with no ratio for THIS product — the message lists the units that
    do resolve (write-time refusal; the resolver's flag is only the backstop).

The form itself is pinned in test_673_promo_form_display.py.
Every product is fresh (mig 177 allows one current price promo per product).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

_pid = [979000]


def _conn(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    return c


def _product(tmp_db, unit_type='อัน', rows=(), tiers=()):
    _pid[0] += 1
    c = _conn(tmp_db)
    pid = c.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, ?, 100, 60, 1)", (f'persist673 #{_pid[0]}', unit_type)).lastrowid
    for u, r in rows:
        c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                  (pid, u, r))
    for label, price in tiers:
        c.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                  (pid, label, price))
    c.commit()
    c.close()
    return pid


def _promos(tmp_db, pid):
    c = _conn(tmp_db)
    rows = c.execute("SELECT promo_type, discount_value, min_qty, min_qty_unit, bundle_condition "
                     "FROM promotions WHERE product_id = ?", (pid,)).fetchall()
    c.close()
    return [tuple(r) for r in rows]


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


# ── models ───────────────────────────────────────────────────────────────────

def test_create_promotion_stores_the_minimum_as_a_word(tmp_db):
    import models
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    models.create_promotion({'product_id': pid, 'promo_name': 'c', 'promo_type': 'percent',
                             'discount_value': 5, 'min_qty': 2, 'min_qty_unit': 'หล'})
    assert _promos(tmp_db, pid) == [('percent', 5.0, 2.0, 'โหล', None)]


def test_replace_promotion_stores_the_minimum_as_a_word(tmp_db):
    import models
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    ok, msg, _id = models.replace_promotion(
        pid, {'promo_name': 'r', 'promo_type': 'percent', 'discount_value': 5,
              'min_qty': 2, 'min_qty_unit': 'หล'}, today='2026-10-02')
    assert ok, msg
    assert _promos(tmp_db, pid) == [('percent', 5.0, 2.0, 'โหล', None)]


def test_replace_promotion_without_a_minimum_is_unchanged(tmp_db):
    import models
    pid = _product(tmp_db)
    ok, msg, _id = models.replace_promotion(
        pid, {'promo_name': 'r', 'promo_type': 'percent', 'discount_value': 5},
        today='2026-10-02')
    assert ok, msg
    assert _promos(tmp_db, pid) == [('percent', 5.0, None, None, None)]


# ── route ────────────────────────────────────────────────────────────────────

def _post(client, pid, **form):
    data = {'promo_name': 'route673', 'promo_type': 'percent', 'discount_value': '5', **form}
    return client.post(f'/products/{pid}/promotions/new', data=data)


def test_route_saves_a_minimum(admin_client, tmp_db):
    pid = _product(tmp_db)
    resp = _post(admin_client, pid, min_qty='20', min_qty_unit='อัน')
    assert resp.status_code == 302
    assert _promos(tmp_db, pid) == [('percent', 5.0, 20.0, 'อัน', None)]


def test_route_saves_a_minimum_in_a_tier_implied_dozen(admin_client, tmp_db):
    """pid 307's shape: no โหล row, only a '1 โหล' tier — the dozen resolves."""
    pid = _product(tmp_db, unit_type='ดอก', tiers=[('1 โหล', 120.0)])
    resp = _post(admin_client, pid, min_qty='5', min_qty_unit='โหล', discount_value='25')
    assert resp.status_code == 302
    assert _promos(tmp_db, pid) == [('percent', 25.0, 5.0, 'โหล', None)]


REFUSED = {
    'qty without unit': (dict(min_qty='20'), 'ขั้นต่ำ'),
    'zero': (dict(min_qty='0', min_qty_unit='อัน'), 'มากกว่า 0'),
    'negative': (dict(min_qty='-3', min_qty_unit='อัน'), 'มากกว่า 0'),
    'not a number': (dict(min_qty='ยี่สิบ', min_qty_unit='อัน'), 'ข้อมูลไม่ถูกต้อง'),
    'on a bundle': (dict(promo_type='bundle', discount_value='', bundle_buy='10',
                         bundle_free='1', min_qty='20', min_qty_unit='อัน'), 'โปรลดราคา'),
    'on mixed with bundle': (dict(promo_type='mixed', bundle_buy='10', bundle_free='1',
                                  min_qty='20', min_qty_unit='อัน'), 'โปรลดราคา'),
}


@pytest.mark.parametrize('label', sorted(REFUSED))
def test_route_refuses_and_writes_nothing(admin_client, tmp_db, label):
    pid = _product(tmp_db)
    form, needle = REFUSED[label]
    resp = _post(admin_client, pid, **form)
    assert resp.status_code == 200                 # re-rendered form, not a redirect
    assert needle in resp.get_data(as_text=True)
    assert _promos(tmp_db, pid) == []


def test_route_refuses_a_unit_with_no_ratio_and_lists_the_ones_that_resolve(admin_client, tmp_db):
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    resp = _post(admin_client, pid, min_qty='1', min_qty_unit='ลัง')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ลัง' in html and 'อัน' in html and 'โหล' in html
    assert _promos(tmp_db, pid) == []


# ── fix round (/interrogate on the PR 1 diff, item 5): the writers refuse too ─
# The route was the only gate; create_promotion / replace_promotion are called
# by scripts as well, and accepted 1 ลัง on an อัน product with no ลัง ratio.

def test_create_promotion_refuses_a_unit_with_no_ratio(tmp_db):
    import models
    pid = _product(tmp_db)
    with pytest.raises(ValueError, match='หน่วยที่ใช้ได้'):
        models.create_promotion({'product_id': pid, 'promo_name': 'c', 'promo_type': 'percent',
                                 'discount_value': 5, 'min_qty': 1, 'min_qty_unit': 'ลัง'})
    assert _promos(tmp_db, pid) == []


@pytest.mark.parametrize('extra,needle', [
    (dict(min_qty=1, min_qty_unit='ลัง'), 'หน่วยที่ใช้ได้'),
    (dict(bundle_condition='ยกลัง'), 'ยกลัง'),
    (dict(min_qty=0, min_qty_unit='อัน'), 'มากกว่า 0'),
    (dict(min_qty=20), 'ขั้นต่ำ'),
], ids=['no ratio', 'label without number', 'zero', 'qty without unit'])
def test_replace_promotion_refuses_and_writes_nothing(tmp_db, extra, needle):
    import models
    pid = _product(tmp_db)
    ok, msg, new_id = models.replace_promotion(
        pid, {'promo_name': 'r', 'promo_type': 'percent', 'discount_value': 5, **extra},
        today='2026-10-02')
    assert (ok, new_id) == (False, None)
    assert needle in msg
    assert _promos(tmp_db, pid) == []


# ── re-review on d0db6aa, finding 8: validate what is stored ─────────────────

def test_minimum_is_validated_as_the_word_that_will_be_stored(tmp_db):
    """'หล' is stored as its word 'โหล'. Here 'หล' has its own usable row (6)
    but 'โหล' has only a ratio-0 row and no tier, so the STORED minimum could
    never be measured — it must be refused at write, not pass on the raw
    spelling."""
    import models
    pid = _product(tmp_db, rows=[('หล', 6.0), ('โหล', 0.0)])
    ok, msg, new_id = models.replace_promotion(
        pid, {'promo_name': 'r', 'promo_type': 'percent', 'discount_value': 5,
              'min_qty': 2, 'min_qty_unit': 'หล'}, today='2026-10-02')
    assert (ok, new_id) == (False, None), msg
    assert 'โหล' in msg
    assert _promos(tmp_db, pid) == []
