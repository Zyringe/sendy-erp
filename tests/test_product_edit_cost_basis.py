"""Regression (#570): the edit form's cost field wrote a column WACC owns.

`blueprints/products.py::product_edit` wrote `products.cost_price` only, but
`models/wacc.py` seeds the cost ledger from `products.opening_cost` and WRITES
`cost_price` back as its own live output. So a manual cost edit
  (a) never moved WACC, and
  (b) was silently reverted by the next `recalculate_product_wacc` — which the
      weekly import runs for EVERY product_id that has a line in the file.
`opening_cost` had no edit surface anywhere in the app, so a product with no
purchase bills had its cost frozen at creation with no way to correct it.

Fix: the edit form's cost field is the WACC BASIS (ต้นทุนยกมา). product_edit
writes `opening_cost` alongside `cost_price` and then recalculates, so the typed
number takes effect and survives every later recompute. Money write path → TDD
(project rule).
"""
import sqlite3

import pytest


INITIAL_DATE = '2026-03-03'


def _seed_product(db_path, *, cost=33.0):
    """A product whose cost basis is purely the opening figure: no bills."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    pid = conn.execute(
        "INSERT INTO products(product_name, units_per_carton, units_per_box,"
        "                     unit_type, hard_to_sell, cost_price, opening_cost,"
        "                     base_sell_price, low_stock_threshold, sku_code)"
        " VALUES ('กลอนทดสอบต้นทุน', 1, 1, 'แผง', 0, ?, ?, 80.0, 10, 'SK-COST-BASIS')",
        (cost, cost)
    ).lastrowid
    conn.commit()
    conn.close()
    return pid


def _seed_purchase(db_path, pid, *, opening_units=10, buy_qty=10, buy_net=500.0):
    """Opening stock on INITIAL_DATE plus one later 'BSN ซื้อ' bill.

    Gives WACC something to blend, so a test can tell "the basis moved" apart
    from "the basis replaced everything".
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        "                         reference_no, note, created_at)"
        " VALUES (?, 'IN', ?, 'unit', NULL, '', ?)",
        (pid, opening_units, f'{INITIAL_DATE} 00:00:00'))
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        "                         reference_no, note, created_at)"
        " VALUES (?, 'IN', ?, 'unit', 'PO-570', 'BSN ซื้อ', '2026-04-01 00:00:00')",
        (pid, buy_qty))
    conn.execute(
        "INSERT INTO purchase_transactions(date_iso, doc_no, product_id, bsn_code,"
        "                                  qty, unit, net)"
        " VALUES ('2026-04-01', 'PO-570', ?, NULL, ?, 'แผง', ?)",
        (pid, buy_qty, buy_net))
    conn.commit()
    conn.close()


def _row(db_path, pid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute(
        "SELECT cost_price, opening_cost, base_sell_price FROM products WHERE id=?",
        (pid,)).fetchone())
    conn.close()
    return row


def _ledger(db_path, pid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT event_type, unit_cost, wacc_after FROM product_cost_ledger"
        " WHERE product_id=? ORDER BY event_date, id", (pid,))]
    conn.close()
    return rows


@pytest.fixture
def admin_client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    return c, empty_db


def _post_cost(client, pid, cost, *, sell='99'):
    """POST the edit form exactly as templates/products/form.html renders it.

    `base_sell_price` is a canary: product_edit ends in a redirect on success AND
    on a caught exception AND on a refused request, so a 302 alone proves nothing
    (erp-engineering-discipline: "a 302 from a Sendy route is NOT evidence").
    Every caller asserts the canary landed before reading the cost columns.
    """
    return client.post(f'/products/{pid}/edit', data={
        'product_name': 'ignored-name',
        'unit_type': 'แผง',
        'units_per_carton': '1',
        'units_per_box': '1',
        'cost_price': str(cost),
        'base_sell_price': sell,
        'low_stock_threshold': '10',
    })


# ── the reported symptom: the typed cost must reach WACC ──────────────────────

def test_edit_writes_the_wacc_basis_not_just_its_output(admin_client):
    import models

    c, db = admin_client
    pid = _seed_product(db, cost=33.0)
    # Materialise the ledger first, the way viewing the product page does —
    # this is the state Put's product was in, and it is what made the lazy
    # recalculate in get_current_wacc stop firing.
    assert models.get_current_wacc(pid) == 33.0

    resp = _post_cost(c, pid, '48.50')
    assert resp.status_code == 302

    row = _row(db, pid)
    assert row['base_sell_price'] == 99, 'canary: the route did not reach the write'
    assert row['opening_cost'] == 48.50, 'the WACC basis must follow the typed cost'
    assert row['cost_price'] == 48.50

    led = _ledger(db, pid)
    assert len(led) == 1, f'expected one INITIAL entry, got {led}'
    assert led[0]['event_type'] == 'INITIAL'
    assert led[0]['unit_cost'] == 48.50
    assert models.get_current_wacc(pid) == 48.50


def test_edited_cost_survives_the_recalculation_every_import_runs(admin_client):
    """THE bug: models/imports.py recalculates every product in the file."""
    import models

    c, db = admin_client
    pid = _seed_product(db, cost=33.0)
    assert models.get_current_wacc(pid) == 33.0

    resp = _post_cost(c, pid, '48.50')
    assert resp.status_code == 302
    assert _row(db, pid)['base_sell_price'] == 99, 'canary'

    # Exactly what models/imports.py:439 does for every affected product.
    returned = models.recalculate_product_wacc(pid)

    assert returned == 48.50
    assert _row(db, pid)['cost_price'] == 48.50, 'the import silently reverted the edit'
    assert models.get_current_wacc(pid) == 48.50


# ── the basis BLENDS with real bills, it does not replace them ────────────────

def test_edit_rebases_wacc_without_discarding_purchase_history(admin_client):
    """A product WITH bills: moving the basis re-blends, it does not overwrite.

    Opening 10 @ basis, then a bill of 10 @ ฿50. WACC is the average of the two
    legs, so the answer must be neither the typed number nor the bill price.
    """
    import models

    c, db = admin_client
    pid = _seed_product(db, cost=33.0)
    _seed_purchase(db, pid, opening_units=10, buy_qty=10, buy_net=500.0)

    # control: the blend is live BEFORE the edit, so a later assertion cannot
    # pass merely because the purchase leg never ran.
    assert models.get_current_wacc(pid) == pytest.approx((10 * 33.0 + 10 * 50.0) / 20)

    resp = _post_cost(c, pid, '48.50')
    assert resp.status_code == 302
    assert _row(db, pid)['base_sell_price'] == 99, 'canary'

    expected = (10 * 48.50 + 10 * 50.0) / 20      # 49.25
    assert models.get_current_wacc(pid) == pytest.approx(expected)
    assert _row(db, pid)['cost_price'] == pytest.approx(expected)
    assert _row(db, pid)['opening_cost'] == 48.50

    led = _ledger(db, pid)
    assert [r['event_type'] for r in led] == ['INITIAL', 'PURCHASE'], led
    assert led[0]['unit_cost'] == 48.50          # the new basis
    assert led[1]['unit_cost'] == pytest.approx(50.0)   # the bill, untouched


# ── clearing the cost still clears it (the zero path) ─────────────────────────

def test_clearing_the_cost_clears_both_columns(admin_client):
    """recalculate_product_wacc refuses to write a 0 WACC, so the route must."""
    c, db = admin_client
    pid = _seed_product(db, cost=33.0)

    resp = _post_cost(c, pid, '')
    assert resp.status_code == 302

    row = _row(db, pid)
    assert row['base_sell_price'] == 99, 'canary'
    assert row['cost_price'] == 0
    assert row['opening_cost'] == 0
    assert _ledger(db, pid) == [], 'a costless product has no ledger to seed'


# ── the rendered form must show the BASIS, in the edit branch only ────────────
#
# These exist because the first attempt at this fix relabelled the wrong block.
# templates/products/form.html holds TWO identical cost boxes — the edit branch
# opens at `{% if action == 'edit' %}` and the new-product branch after its
# `{% else %}` — and every DB-level test above stayed green while the page kept
# rendering the old label off the other branch. A page-wide substring check
# would not have caught it either: both branches live in the same FILE, so the
# assertion has to be scoped to the element the box is actually in.

def _cost_box_cell(html):
    """The one column that contains the cost input, as its own fragment."""
    from lxml import html as lh

    doc = lh.fromstring(html)
    inputs = doc.xpath("//input[@name='cost_price']")
    assert len(inputs) == 1, f'expected exactly one cost input, got {len(inputs)}'
    cell = inputs[0].getparent()
    while cell is not None and 'col-' not in (cell.get('class') or ''):
        cell = cell.getparent()
    assert cell is not None, 'cost input is not inside a layout column'
    return inputs[0], cell


def test_edit_form_shows_the_basis_labelled_as_the_basis(admin_client):
    c, db = admin_client
    pid = _seed_product(db, cost=33.0)
    # Diverge the two columns the way Put's product was, so an assertion on the
    # rendered value can actually tell them apart. Equal values would pass
    # whichever column the template reads.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE products SET cost_price=99.0 WHERE id=?", (pid,))
    conn.commit()
    conn.close()

    resp = c.get(f'/products/{pid}/edit')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    box, cell = _cost_box_cell(html)
    text = ' '.join(cell.itertext())

    assert box.get('value') == '33.0', 'the box must render opening_cost, not cost_price'
    assert 'ต้นทุนยกมา' in text, f'label is not the basis: {text.strip()[:80]!r}'


def test_new_product_form_keeps_its_own_cost_label(admin_client):
    """Control: the relabel must land in the edit branch and ONLY there.

    Without this, relabelling the other branch of the same template passes
    every other test in this file.
    """
    c, _db = admin_client

    resp = c.get('/products/new')
    assert resp.status_code == 200
    _box, cell = _cost_box_cell(resp.get_data(as_text=True))
    text = ' '.join(cell.itertext())

    assert 'ต้นทุน' in text, 'control: the new-product form lost its cost box entirely'
    assert 'ต้นทุนยกมา' not in text, 'the relabel landed in the new-product branch'


# ── #615: an untouched basis must not overwrite the live WACC ────────────────

def _rendered_cost_box(client, pid):
    resp = client.get(f'/products/{pid}/edit')
    assert resp.status_code == 200
    box, _cell = _cost_box_cell(resp.get_data(as_text=True))
    return box


def test_untouched_cost_box_preserves_live_wacc_and_basis(admin_client, monkeypatch):
    import models

    c, db = admin_client
    pid = _seed_product(db, cost=33.0)
    _seed_purchase(db, pid, opening_units=10, buy_qty=10, buy_net=500.0)
    assert models.get_current_wacc(pid) == pytest.approx(41.5)
    before = _row(db, pid)
    rendered_cost = _rendered_cost_box(c, pid).get('value')

    recalculations = []

    def record_recalculation(*args, **kwargs):
        recalculations.append((args, kwargs))
        return 0

    monkeypatch.setattr(models, 'recalculate_product_wacc', record_recalculation)
    resp = _post_cost(c, pid, rendered_cost, sell='101.25')
    assert resp.status_code == 302

    after = _row(db, pid)
    assert after['base_sell_price'] == 101.25, 'canary: the route did not reach the write'
    assert len(recalculations) == 0, 'an untouched basis must not recalculate WACC'
    assert after['cost_price'] == before['cost_price']
    assert after['opening_cost'] == before['opening_cost']


def test_untouched_many_decimal_basis_preserves_both_costs(admin_client, monkeypatch):
    import models

    c, db = admin_client
    opening_cost = 15.158333333333333
    pid = _seed_product(db, cost=opening_cost)
    _seed_purchase(db, pid, opening_units=10, buy_qty=10, buy_net=500.0)
    assert models.get_current_wacc(pid) == pytest.approx(
        (10 * opening_cost + 10 * 50.0) / 20
    )
    before = _row(db, pid)
    rendered_cost = _rendered_cost_box(c, pid).get('value')
    assert rendered_cost == str(before['opening_cost'])

    recalculations = []

    def record_recalculation(*args, **kwargs):
        recalculations.append((args, kwargs))
        return 0

    monkeypatch.setattr(models, 'recalculate_product_wacc', record_recalculation)
    resp = _post_cost(c, pid, rendered_cost, sell='102.75')
    assert resp.status_code == 302

    after = _row(db, pid)
    assert after['base_sell_price'] == 102.75, 'canary: the route did not reach the write'
    assert len(recalculations) == 0, 'an untouched basis must not recalculate WACC'
    assert after['cost_price'] == before['cost_price']
    assert after['opening_cost'] == before['opening_cost']


def test_untouched_blank_box_preserves_purchase_driven_wacc(admin_client, monkeypatch):
    """The dominant prod shape: opening_cost = 0 with a live cost_price > 0.

    850 of the 894 exposed active products on the 2026-09-19 15:36Z prod
    snapshot look like this — their cost came from purchase bills only, so the
    basis was never typed. The box renders `{{ 0.0 or '' }}` = BLANK and posts
    back ''. Before the fix that wrote 0 to BOTH columns, zeroing a real WACC.
    The two tests above seed opening == cost != 0 and cannot see this branch.
    """
    import models

    c, db = admin_client
    pid = _seed_product(db, cost=0.0)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE products SET cost_price=41.5 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    before = _row(db, pid)
    # control: the two columns really diverge the way the 850 do
    assert before['opening_cost'] == 0.0 and before['cost_price'] == 41.5

    box = _rendered_cost_box(c, pid)
    assert box.get('value') == '', 'control: a zero basis renders as a blank box'

    recalculations = []

    def record_recalculation(*args, **kwargs):
        recalculations.append((args, kwargs))
        return 0

    monkeypatch.setattr(models, 'recalculate_product_wacc', record_recalculation)
    resp = _post_cost(c, pid, '', sell='104.25')
    assert resp.status_code == 302

    after = _row(db, pid)
    assert after['base_sell_price'] == 104.25, 'canary: the route did not reach the write'
    assert len(recalculations) == 0, 'a blank, untouched basis must not recalculate WACC'
    assert after['cost_price'] == 41.5, 'the blank box zeroed the live WACC'
    assert after['opening_cost'] == 0.0


def test_edit_form_accepts_its_rendered_many_decimal_basis(admin_client):
    c, db = admin_client
    opening_cost = 15.158333333333333
    pid = _seed_product(db, cost=opening_cost)

    box = _rendered_cost_box(c, pid)

    assert box.get('value') == str(opening_cost)
    assert box.get('step') == 'any', (
        'the browser must allow the raw many-decimal basis rendered by the form'
    )


def test_changed_cost_box_still_rebases_and_recalculates_wacc(admin_client, monkeypatch):
    import models

    c, db = admin_client
    pid = _seed_product(db, cost=33.0)
    _seed_purchase(db, pid, opening_units=10, buy_qty=10, buy_net=500.0)
    assert models.get_current_wacc(pid) == pytest.approx(41.5)
    rendered_cost = _rendered_cost_box(c, pid).get('value')
    assert rendered_cost == '33.0'

    real_recalculate = models.recalculate_product_wacc
    recalculations = []

    def record_recalculation(product_id):
        recalculations.append(product_id)
        return real_recalculate(product_id)

    monkeypatch.setattr(models, 'recalculate_product_wacc', record_recalculation)
    resp = _post_cost(c, pid, '48.50', sell='103.50')
    assert resp.status_code == 302

    row = _row(db, pid)
    assert row['base_sell_price'] == 103.50, 'canary: the route did not reach the write'
    assert len(recalculations) == 1
    assert recalculations[0] == pid
    assert row['opening_cost'] == 48.50
    assert row['cost_price'] == pytest.approx(49.25)

    with c.session_transaction() as session:
        flashes = session.get('_flashes', [])
    assert len(flashes) == 1
    assert flashes[0][0] == 'success'
    assert 'ต้นทุน (WACC) ฿49.25' in flashes[0][1]
