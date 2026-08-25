"""PR3 (mapping-suggest-clone) — GET /products/spec/<pid> and
POST /products/preview-identity.

projects/mapping-suggest-clone/plan.md, "PR3 — clone", items 1-2.

Covers:
  - /products/spec/<pid> returns the clone-source spec, WITHOUT cost_price /
    base_sell_price / family_id (Q4 clone never copies cost; Q14 clone never
    inherits the source's photo family).
  - /products/preview-identity produces the SAME sku_code
    create_structured_product would actually write for the same fields — the
    claim the create_now duplicate guard rests on.
  - An all-blank preview never leaks build_sku_code's INT-<id> fallback.
  - staff gets a real 200+JSON from preview-identity, not the 302→HTML a
    role-gated naming.* endpoint would give (the regression this new,
    separate endpoint exists to avoid).

⚠ tests/conftest.py::tmp_db clones the LIVE dev DB *with its data*. The spec
test creates its OWN throwaway product (deleted first by name) rather than
relying on any specific existing row's current column values.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

# Stable seed rows already validated live (test_mapping_subcategory.py):
# category id 6 = ค้อน, short_code HMR.
_CAT_ID = 6
_CAT_SHORT = 'HMR'

_SPEC_TEST_NAME = 'ทดสอบ clone spec — ห้ามลบมือ'


def _admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _staff_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-staff'
        sess['role'] = 'staff'
    return c


@pytest.fixture
def admin_client(tmp_db):
    return _admin_client(tmp_db), tmp_db


@pytest.fixture
def staff_client(tmp_db):
    return _staff_client(tmp_db), tmp_db


# ── /products/spec/<pid> ─────────────────────────────────────────────────────

@pytest.fixture
def spec_source_product(tmp_db):
    """A throwaway product with every clone-relevant field set to a NON-zero,
    NON-null value — so if cost/base_sell_price/family_id ever leaked into
    the response, the control values below would actually be non-trivial
    (not just an absent key matching an absent value by coincidence)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM products WHERE product_name = ?", (_SPEC_TEST_NAME,))
    conn.execute("""
        INSERT INTO products
          (product_name, unit_type, cost_price, base_sell_price,
           category_id, sub_category, sub_category_short_code, series,
           brand_id, model, size, color_code, packaging_th, condition,
           pack_variant, units_per_carton, units_per_box, is_active)
        VALUES (?, 'ตัว', 123.45, 199.0, ?, 'ทดสอบหมวดย่อย', 'ZTST', 'SERIES-X',
                1, 'MODEL-X', '99mm', 'AC', 'ถุง', 'ไม่สวย', 2, 12, 6, 1)
    """, (_SPEC_TEST_NAME, _CAT_ID))
    conn.commit()
    pid = conn.execute(
        "SELECT id FROM products WHERE product_name=?", (_SPEC_TEST_NAME,)
    ).fetchone()[0]
    # family_id is set to a REAL family here so both halves of the contract
    # have something concrete to check: the money fields must still be absent,
    # and family_id must now be PRESENT and correct (Q14 reversed 2026-08-25 —
    # a clone joins its template's family).
    fam = conn.execute("SELECT id FROM product_families LIMIT 1").fetchone()
    if fam:
        conn.execute("UPDATE products SET family_id=? WHERE id=?", (fam[0], pid))
        conn.commit()
    conn.close()
    return pid


def test_product_spec_returns_expected_keys(admin_client, spec_source_product):
    client, _db = admin_client
    resp = client.get(f'/products/spec/{spec_source_product}')
    assert resp.status_code == 200, resp.data[:500]
    body = resp.get_json()

    # CONTROL — fields that SHOULD be present and correct (not vacuous: if
    # the whole SELECT silently returned nothing, these would fail too).
    assert body['id'] == spec_source_product
    assert body['product_name'] == _SPEC_TEST_NAME
    assert body['category_id'] == _CAT_ID
    assert body['sub_category'] == 'ทดสอบหมวดย่อย'
    assert body['sub_category_short_code'] == 'ZTST'
    assert body['series'] == 'SERIES-X'
    assert body['brand_id'] == 1
    assert body['model'] == 'MODEL-X'
    assert body['size'] == '99mm'
    assert body['color_code'] == 'AC'
    assert body['color_th'] == 'สีรมดำ'
    assert body['packaging_th'] == 'ถุง'
    assert body['condition'] == 'ไม่สวย'
    assert body['pack_variant'] == '2'
    assert body['unit_type'] == 'ตัว'
    assert body['units_per_carton'] == 12
    assert body['units_per_box'] == 6


def test_product_spec_excludes_money(admin_client, spec_source_product):
    """The negative half — must not leak cost_price / base_sell_price. GET is
    fail-open for every logged-in role and staff is explicitly a no-cost role,
    so money must never cross this boundary.

    Asserted explicitly (not merely absent from a dict that also happens to
    lack other things): the fixture row carries 123.45 / 199.0, so an absent
    key is a real exclusion rather than an absent value matching by
    coincidence. The `family_id` CONTROL below is what makes this test unable
    to pass on a broken/empty response — if the whole SELECT returned nothing,
    the money keys would be absent too and this would look clean."""
    client, _db = admin_client
    resp = client.get(f'/products/spec/{spec_source_product}')
    body = resp.get_json()
    assert body.get('family_id'), (
        'CONTROL: the fixture set a real family — without a populated response '
        'the money assertions below could not fail'
    )
    assert 'cost_price' not in body
    assert 'base_sell_price' not in body


def test_product_spec_ships_family_so_a_clone_can_join_it(admin_client, spec_source_product):
    """Reverses decision Q14 (2026-08-25). Q14 withheld family_id on the theory
    that colour is part of the family key, so a clone joining its template's
    family would render the white variant with the black sibling's photo.
    Measured on prod that day: 82 families already hold more than one colour
    (328 products), only 2 groups are split by colour alone, and Q14's own
    example `SD-230-AC` does not exist — the real family is `SD-230`, holding
    AC + CR + SN in one size_table.

    The endpoint must therefore report the template's family (and its name, for
    the "จะเข้า family ..." line the two clone UIs show). It is REPORTING, not
    posting: the server derives the new product's family from
    `clone_source_pid` at create time, so this value can go stale without
    causing a wrong write."""
    client, db_path = admin_client
    resp = client.get(f'/products/spec/{spec_source_product}')
    body = resp.get_json()

    conn = sqlite3.connect(db_path)
    expected = conn.execute(
        'SELECT p.family_id, pf.display_name FROM products p '
        'LEFT JOIN product_families pf ON pf.id = p.family_id WHERE p.id = ?',
        (spec_source_product,)).fetchone()
    conn.close()
    assert expected[0], 'fixture did not attach a family — the rest is vacuous'
    assert body['family_id'] == expected[0]
    assert body['family_name'] == expected[1]


def test_product_spec_unknown_id_404(admin_client):
    client, _db = admin_client
    resp = client.get('/products/spec/99999999')
    assert resp.status_code == 404


# ── /products/preview-identity ───────────────────────────────────────────────

def _rich_fields():
    return {
        'category_id': _CAT_ID,
        'sub_category': 'ทดสอบหมวดย่อย',
        'sub_category_short_code': 'ZTST',
        'series': 'SERIES-X',
        'brand_id': 1,          # Golden Lion, short_code GL
        'model': 'MODEL-X',
        'size': '99mm',
        'color_code': 'AC',     # สีรมดำ
        'packaging_th': 'ถุง',
        'condition': 'ไม่สวย',
        'pack_variant': None,
    }


def test_preview_identity_matches_create_structured_product_sku_code(admin_client):
    """The claim the create_now duplicate guard rests on: the preview and a
    REAL create_structured_product insert with the identical fields must
    produce the identical sku_code."""
    client, db_path = admin_client
    resp = client.post('/products/preview-identity', json=_rich_fields())
    assert resp.status_code == 200, resp.data[:500]
    preview = resp.get_json()
    assert preview['sku_code'], "expected a non-empty preview sku_code"

    from models.products import create_structured_product
    pid = create_structured_product(dict(_rich_fields(), product_name=None), 'manual')

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sku_code, product_name FROM products WHERE id=?", (pid,)
    ).fetchone()
    conn.close()

    assert row[0] == preview['sku_code'], (
        f"preview sku_code {preview['sku_code']!r} != actual {row[0]!r}"
    )
    assert row[1] == preview['name'], (
        f"preview name {preview['name']!r} != actual {row[1]!r}"
    )
    assert preview['sku_code'].startswith(_CAT_SHORT)
    assert 'ZTST' in preview['sku_code']


def test_preview_identity_empty_fields_no_int_leak(admin_client):
    client, _db = admin_client
    resp = client.post('/products/preview-identity', json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['sku_code'] == ''
    assert not body['sku_code'].startswith('INT-')
    assert body['name'] == ''


def test_preview_identity_staff_role_200_json_not_302(staff_client):
    """The regression this endpoint exists to avoid: naming.product_preview_name
    is _MANAGER_POST_OK only, so a staff POST there 302-redirects to HTML and
    r.json() throws. staff IS a real /mapping user (bsn.mapping_save is
    staff-postable) — this must return 200 + real JSON."""
    client, _db = staff_client
    resp = client.post('/products/preview-identity', json=_rich_fields())
    assert resp.status_code == 200, resp.data[:500]
    assert resp.is_json
    body = resp.get_json()
    assert body['sku_code'].startswith(_CAT_SHORT)
