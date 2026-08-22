"""PR4 (mapping-suggest-clone) — clone-from-existing-SKU on /products/new.

projects/mapping-suggest-clone/plan.md, "PR4 — /products/new clone".

/products/new reuses PR3's two endpoints UNCHANGED (GET /products/spec/<pid>,
POST /products/preview-identity) — their own coverage lives in
tests/test_product_spec_and_preview_identity.py and is not duplicated here.
This file covers what's NEW to PR4:
  - the create form renders the clone-source search control + the new
    sub_category_short_code field
  - /products/spec/<pid>'s payload keys map onto the create form's field ids
    (a rename on either side of the clone contract must go red)
  - /products/new POST persists sub_category_short_code
  - a real clone round-trip THROUGH THE FORM ROUTE: feed a source's
    /products/spec/<pid> payload through product_new()'s own form-parsing
    (int()/'__other__' coercion — different code path from
    create_structured_product() called directly, which is all PR3 exercised)
    and confirm the created row's sku_code matches what
    /products/preview-identity predicted for the identical fields

⚠ tests/conftest.py::tmp_db clones the LIVE dev DB *with its data*. Every
fixture here forces its own state (DELETE-then-INSERT by name).

⚠ The client-side "select value absent from its option list" fallback
(setSelectOrWarn in the rendered <script>) has no reachable LIVE fixture:
category_id/brand_id/color_code are FK-sourced from the exact same tables
the create form's <option>s are built from, and packaging_th is enforced by
products_packaging_th_check_{insert,update} to already be one of
form_options.packaging()'s 11 values (pinned by
test_form_options.py::test_packaging_matches_the_check_trigger) — so no
live product can ever carry a value outside its own form's option list.
This repo has no JS test runner, so the fallback branch is pinned here as a
SOURCE-level regression guard (the specific shapes the function must
contain: clears the value AND records a warning, never a silent no-op),
not exercised at runtime. A real runtime demo needs a browser click-through
against a synthetic mismatch (no live product reaches this branch) — the
function is short enough to review directly instead.
"""
import os
import re
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

# Stable seed rows already validated live (test_mapping_subcategory.py /
# test_product_spec_and_preview_identity.py): category id 6 = ค้อน, HMR.
_CAT_ID = 6
_CAT_SHORT = 'HMR'

_CLONE_SOURCE_NAME = 'ทดสอบ PR4 clone source — ห้ามลบมือ'


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c, tmp_db


@pytest.fixture
def clone_source_product(tmp_db):
    """A throwaway product carrying every clone-relevant spec field, so a
    clone round-trip through it exercises every field product_new() parses."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM products WHERE product_name = ?", (_CLONE_SOURCE_NAME,))
    conn.execute("""
        INSERT INTO products
          (product_name, unit_type, cost_price, base_sell_price,
           category_id, sub_category, sub_category_short_code, series,
           brand_id, model, size, color_code, packaging_th, condition,
           pack_variant, units_per_carton, units_per_box, is_active)
        VALUES (?, 'ตัว', 55.5, 99.0, ?, 'ทดสอบหมวดย่อย PR4', 'ZP4', 'SERIES-P4',
                1, 'MODEL-P4', '77mm', 'AC', 'ถุง', 'ไม่สวย', 3, 24, 12, 1)
    """, (_CLONE_SOURCE_NAME, _CAT_ID))
    conn.commit()
    pid = conn.execute(
        "SELECT id FROM products WHERE product_name=?", (_CLONE_SOURCE_NAME,)
    ).fetchone()[0]
    conn.close()
    return pid


# ── GET /products/new renders the clone controls ────────────────────────────

# ── the rendered <script>, with JS comments removed ─────────────────────────
#
# A bare `'function cloneFromProduct(' in html` passes just as happily when the
# whole function sits inside a /* ... */ block. That is how PR4's first cut
# shipped ~120 lines of dead JS past seven green tests: the block comment above
# `const allProducts` was closed with Jinja's `#}` instead of `*/`, so
# everything down to the next real `*/` rendered as a comment. Assert against
# the comment-STRIPPED source, never the raw page.

_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.S)
_LINE_COMMENT = re.compile(r'(?m)^[ \t]*//.*$')


def _create_form_script(html):
    """Body of the inline <script> that carries the create form's JS."""
    anchor = html.index('function parseRawName(')
    open_tag = html.rindex('<script', 0, anchor)
    body_start = html.index('>', open_tag) + 1
    return html[body_start:html.index('</script>', body_start)]


def _live_js(html):
    src = _create_form_script(html)
    return _LINE_COMMENT.sub('', _BLOCK_COMMENT.sub('', src))


def test_product_new_get_renders_clone_search_control(admin_client):
    client, _db = admin_client
    resp = client.get('/products/new')
    assert resp.status_code == 200, resp.data[:500]
    html = resp.get_data(as_text=True)
    assert 'id="clone-search"' in html
    assert 'id="clone-drop"' in html
    assert 'id="clone-status"' in html
    assert 'id="sku-preview"' in html
    assert 'function cloneFromProduct(' in _live_js(html)


def test_clone_js_is_live_code_not_commented_out(admin_client):
    """Every clone symbol must survive comment-stripping.

    The markup rendering is not the feature — the JS behind it is. When PR4's
    block comment was closed with `#}` instead of `*/`, every id above was
    still in the DOM, `function cloneFromProduct(` was still in the page
    source, and the whole feature was inert."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))

    # CONTROL: the strip helper leaves ordinary code alone. Without this the
    # assertions below could pass by matching an empty-ish string, or fail for
    # a reason that has nothing to do with comments.
    assert 'function parseRawName(' in live

    # An UNTERMINATED /* is invisible to the paired-delimiter regex, so it
    # would leave commented-out code in `live` and make the loop vacuous.
    assert '/*' not in live, "unterminated JS block comment in the create form's <script>"

    for symbol in (
        'const allProducts',
        "getElementById('product_name').addEventListener('input'",
        'function setSelectOrWarn(',
        'function fetchPnIdentityPreview(',
        'function cloneFromProduct(',
        'setupCloneSearchAutocomplete',
    ):
        assert symbol in live, (
            f"{symbol} renders but sits inside a comment — dead code on the page"
        )


def test_rendered_page_carries_no_jinja_comment_terminator(admin_client):
    """A `#}` can never survive rendering — Jinja emits nothing for a real
    comment — so one in the output is an unmatched terminator. Here it was a
    JS block comment closed with the wrong delimiter."""
    client, _db = admin_client
    html = client.get('/products/new').get_data(as_text=True)
    # CONTROL: Jinja comments really are stripped at render time.
    assert 'New product: type raw name' not in html
    assert '#}' not in html


def test_product_new_get_renders_sub_category_short_code_field(admin_client):
    client, _db = admin_client
    resp = client.get('/products/new')
    html = resp.get_data(as_text=True)
    assert 'name="sub_category_short_code"' in html
    assert 'id="sub_category_short_code"' in html


def test_product_new_get_prefills_sub_category_short_code_on_validation_error(admin_client):
    """The _new_form_context() re-render on a POST validation error must
    still carry whatever the user had typed into the new field — same
    contract as the pre-existing sub_category field it sits next to."""
    client, _db = admin_client
    resp = client.post('/products/new', data={
        'product_name': 'pytest PR4 invalid packaging',
        'sub_category_short_code': 'ZWARN',
        'packaging_th': 'ไม่มีจริง',   # rejected by the CHECK trigger
        'unit_type': 'ตัว',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]
    html = resp.get_data(as_text=True)
    assert 'value="ZWARN"' in html


# ── /products/spec/<pid> payload maps onto the form's field ids ─────────────

def test_product_spec_keys_map_onto_product_new_field_ids(admin_client, clone_source_product):
    """The rename-safety contract PR4 depends on: every key
    /products/spec/<pid> returns for a clonable field has a matching
    id="<key>" element in the /products/new create form. A rename on
    either side (the endpoint's SELECT aliases, or the form's field ids)
    must fail this test."""
    client, _db = admin_client
    spec_resp = client.get(f'/products/spec/{clone_source_product}')
    assert spec_resp.status_code == 200, spec_resp.data[:500]
    spec = spec_resp.get_json()

    form_resp = client.get('/products/new')
    html = form_resp.get_data(as_text=True)

    # Keys that ARE directly-editable form fields (id/product_name/color_th
    # are read-only lookups, not prefill targets — color_th is deliberately
    # not settable, name_builder always re-derives it from color_code).
    clonable_keys = [
        'category_id', 'sub_category', 'sub_category_short_code', 'series',
        'brand_id', 'model', 'size', 'color_code', 'packaging_th',
        'condition', 'pack_variant', 'unit_type', 'units_per_carton',
        'units_per_box',
    ]
    assert set(clonable_keys) <= set(spec.keys()), (
        "expected clone-source keys missing from /products/spec response — "
        f"got {sorted(spec.keys())}"
    )
    missing_ids = [k for k in clonable_keys if f'id="{k}"' not in html]
    assert not missing_ids, f"form is missing id=... for: {missing_ids}"


# ── POST /products/new persists sub_category_short_code ─────────────────────

def test_product_new_post_persists_sub_category_short_code(admin_client):
    client, db_path = admin_client
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = 'pytest sub_category_short_code product'")
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': 'pytest sub_category_short_code product',
        'category_id': str(_CAT_ID),
        'sub_category': 'ค้อนทดสอบ',
        'sub_category_short_code': 'ZSUB',
        'unit_type': 'ตัว',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sub_category_short_code, sku_code FROM products "
        "WHERE product_name = 'pytest sub_category_short_code product'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['sub_category_short_code'] == 'ZSUB'
    assert 'ZSUB' in row['sku_code']


# ── Full clone round-trip through product_new()'s OWN form parsing ──────────

def test_clone_round_trip_via_product_new_post_matches_preview_identity(admin_client, clone_source_product):
    """Simulates exactly what the JS does: fetch the source's spec, POST
    those same values through /products/new (product_new()'s form-field
    parsing — int()/'__other__' coercion, NOT create_structured_product()
    called directly, which is all test_product_spec_and_preview_identity.py
    exercises), and confirm the created row's sku_code equals what
    /products/preview-identity predicted for the identical fields — the
    guarantee the live sku_code preview box rests on for THIS route."""
    client, db_path = admin_client
    spec = client.get(f'/products/spec/{clone_source_product}').get_json()

    preview_fields = {
        'category_id': spec['category_id'],
        'sub_category': spec['sub_category'],
        'sub_category_short_code': spec['sub_category_short_code'],
        'series': spec['series'],
        'brand_id': spec['brand_id'],
        'model': spec['model'],
        'size': spec['size'],
        'color_code': spec['color_code'],
        'packaging_th': spec['packaging_th'],
        'condition': spec['condition'],
        'pack_variant': spec['pack_variant'],
    }
    preview = client.post('/products/preview-identity', json=preview_fields).get_json()
    assert preview['sku_code'], "expected a non-empty preview sku_code"

    product_name = 'pytest PR4 clone round-trip product'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': product_name,   # explicit override, like the real form
        'category_id': str(spec['category_id']),
        'sub_category': spec['sub_category'],
        'sub_category_short_code': spec['sub_category_short_code'],
        'series': spec['series'],
        'brand_id': str(spec['brand_id']),
        'model': spec['model'],
        'size': spec['size'],
        'color_code': spec['color_code'],
        'packaging_th': spec['packaging_th'],
        'condition': spec['condition'],
        'pack_variant': spec['pack_variant'],
        'unit_type': spec['unit_type'],
        'units_per_carton': str(spec['units_per_carton']),
        'units_per_box': str(spec['units_per_box']),
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['sku_code'] == preview['sku_code'], (
        f"created sku_code {row['sku_code']!r} != previewed {preview['sku_code']!r}"
    )
    # Decision Q4/Q14: clone never carries cost or family_id through this
    # route either — /products/spec never returns them, so the form has
    # nothing to prefill, and this pins that stays true end to end.
    assert row['cost_price'] == 0.0
    assert row['family_id'] is None
    assert row['sub_category_short_code'] == spec['sub_category_short_code']
    assert row['units_per_carton'] == spec['units_per_carton']
    assert row['units_per_box'] == spec['units_per_box']


# ── select-fallback source pin (see module docstring — no live fixture) ─────

def test_preview_drops_out_of_order_responses(admin_client):
    """Structural pin (no JS runner in this repo, same as the test below).

    /mapping's fetchIdentityPreview() stamps each request and drops any
    response that is not the newest (Codex review of PR3). Without the same
    guard here, two edits either side of the 250ms debounce race, and the
    older answer overwrites product_name with a name built from stale
    columns — the divergence this preview exists to prevent."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))
    start = live.index('function fetchPnIdentityPreview(')
    fn_src = live[start:live.index('\n}', start)]

    stamp = fn_src.index('++_pnPreviewSeq')
    guard = fn_src.index('seq !== _pnPreviewSeq')
    write = fn_src.index("getElementById('product_name').value")
    assert stamp < guard < write, (
        "fetchPnIdentityPreview must stamp its request, then drop a stale "
        "response BEFORE writing product_name"
    )
    assert 'return' in fn_src[guard:write], "the stale-response check must bail out"


def test_set_select_or_warn_js_never_silently_leaves_blank(admin_client):
    """Structural pin only (see module docstring): the fallback branch must
    clear the select's stale value AND record a warning — not just return.
    A version that dropped the `warnings.push` call (silently blank) or
    dropped `sel.value = ''` (leaving a stale selection while claiming it
    was overwritten) both pass every OTHER test in this file, because no
    live product reaches this branch."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))
    start = live.index('function setSelectOrWarn(')
    end = live.index('\n}', start)
    fn_src = live[start:end]

    assert "sel.value = ''" in fn_src, (
        "setSelectOrWarn must clear the select on a miss, not leave a stale value"
    )
    assert 'warnings.push(label)' in fn_src, (
        "setSelectOrWarn must record the miss — a silent return is the bug class "
        "this function exists to avoid (see /naming's 44-row incident)"
    )
    # The clear+warn must be reachable ONLY after the match loop fails, not
    # before it (i.e. not unconditionally clearing+warning every call).
    loop_idx = fn_src.index('for (')
    warn_idx = fn_src.index('warnings.push(label)')
    assert warn_idx > loop_idx, "the warning must sit AFTER the match loop, not before it"
