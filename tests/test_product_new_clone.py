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
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess

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


# ═════════════════════════════════════════════════════════════════════════
# projects/products-new-clone-provenance/plan.md — created_via='manual_clone_<pid>'
#
# /products/new's clone (above) copies every spec field from a source
# product but always stamps plain 'manual' — indistinguishable from a
# hand-typed product. This section adds a second provenance token driven
# by a new hidden `clone_source_pid` field the clone JS arms/clears, per
# the plan's "Verification contract".
# ═════════════════════════════════════════════════════════════════════════

_HIDDEN_CLONE_PID_TAG_RE = re.compile(r'<input[^>]*\bid="clone_source_pid"[^>]*>')
_VALUE_ATTR_RE = re.compile(r'\bvalue="([^"]*)"')
_CLONE_CLEAR_TAG_RE = re.compile(r'<button[^>]*\bid="clone-clear"[^>]*>')
_CLONE_STATUS_TEXT_RE = re.compile(r'<span[^>]*\bid="clone-status-text"[^>]*>(.*?)</span>', re.S)


def _extract_clone_source_pid_input(html):
    """The hidden clone_source_pid input, asserting there is EXACTLY ONE —
    so a red here reads as 'wrong/missing value', never a None-dereference
    from a selector that found nothing (Codex R2, verification contract #2)."""
    matches = _HIDDEN_CLONE_PID_TAG_RE.findall(html)
    assert len(matches) == 1, (
        f"expected exactly one clone_source_pid hidden input, found "
        f"{len(matches)}: {matches}"
    )
    m = _VALUE_ATTR_RE.search(matches[0])
    return m.group(1) if m else ''


def _extract_clone_status_text(html):
    m = _CLONE_STATUS_TEXT_RE.search(html)
    assert m is not None, "expected a #clone-status-text span (D10a stable children)"
    return m.group(1)


def _extract_clone_clear_tag(html):
    matches = _CLONE_CLEAR_TAG_RE.findall(html)
    assert len(matches) == 1, (
        f"expected exactly one clone-clear button (D10a stable children), "
        f"found {len(matches)}: {matches}"
    )
    return matches[0]


def _extract_js_function(live, name):
    """Brace-matched extraction of `function <name>(...) { ... }` from the
    live (comment-stripped) JS — robust to nested `{ }` blocks (inline
    callbacks etc.) that would break a naive search for the next bare
    line-start '}' (the trick the pre-existing tests above use, which only
    happens to work for fetchPnIdentityPreview/setSelectOrWarn because
    neither has an earlier zero-indent '}')."""
    marker = f'function {name}('
    start = live.index(marker)
    brace_start = live.index('{', start)
    depth = 0
    i = brace_start
    while i < len(live):
        if live[i] == '{':
            depth += 1
        elif live[i] == '}':
            depth -= 1
            if depth == 0:
                return live[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces while extracting function {name}")


def _extract_callback_body(src, anchor):
    """Brace-matched body (contents only, no braces) of the anonymous
    function whose definition starts at `anchor` inside `src`, e.g.
    '.then(function (spec)' or '.catch(function ('."""
    start = src.index(anchor)
    brace_start = src.index('{', start)
    depth = 0
    i = brace_start
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[brace_start + 1:i]
        i += 1
    raise AssertionError(f"unbalanced braces extracting callback at {anchor!r}")


def _extract_retain_declaration(live):
    """`var RETAIN = {};` verbatim from the live source (not hardcoded) —
    used to feed the executable contract test the REAL declaration rather
    than a Python-side guess at its shape."""
    start = live.index('var RETAIN')
    end = live.index(';', start) + 1
    return live[start:end]


# ── Executable JS contract test for setCloneSource (Codex review: source-
# substring pins on this function are provably vacuous under mutation —
# `hidden.value =` still matches `hidden.value ==`, and 'hidden'/'​.value'
# being merely PRESENT in the RETAIN branch doesn't pin WHICH branch reads
# armedPid). Run the REAL rendered function under node with a tiny hand-
# written DOM stub and assert OBSERVED STATE instead. ─────────────────────

def _node_executable():
    found = shutil.which('node')
    if found:
        return found
    # This repo has no prior node precedent / no pinned PATH entry for it —
    # fall back to the known Homebrew location noted in the dispatch brief.
    fallback = '/opt/homebrew/bin/node'
    return fallback if os.path.exists(fallback) else None


# Fakes only the three DOM surfaces setCloneSource touches: a hidden input's
# .value, a span's .textContent, and a button's classList.toggle. No jsdom —
# real getElementById/classList semantics are a few lines each.
_DOM_STUB = r"""
function makeClassList(initial) {
  var classes = {};
  (initial || []).forEach(function (c) { classes[c] = true; });
  return {
    contains: function (c) { return !!classes[c]; },
    toggle: function (c, force) {
      if (force === undefined) {
        if (classes[c]) { delete classes[c]; return false; }
        classes[c] = true;
        return true;
      }
      if (force) { classes[c] = true; } else { delete classes[c]; }
      return !!force;
    }
  };
}

// A real <input>.value setter always coerces to string (setting it to the
// number 23 reads back as "23") — reproduce that so a stub-only pass can't
// hide `hidden.value = pid` losing the coercion a real input gives for free.
function makeHiddenInput() {
  var raw = '';
  return {
    get value() { return raw; },
    set value(v) { raw = String(v); }
  };
}

var elements = {
  'clone_source_pid': makeHiddenInput(),
  'clone-status-text': { textContent: '' },
  'clone-clear': { classList: makeClassList(['d-none']) }
};

var document = {
  getElementById: function (id) {
    return Object.prototype.hasOwnProperty.call(elements, id) ? elements[id] : null;
  }
};
"""

# Drives setCloneSource through the four contract cases and prints one JSON
# snapshot per case. Deliberately does NOT touch cloneFromProduct/fetch —
# that needs a real network stub this repo has no precedent for; the
# _cloneSeq ordering guard stays a scoped source pin (see the two tests
# below this one).
_CONTRACT_DRIVER = r"""
var results = [];
function snapshot(label) {
  results.push({
    label: label,
    hiddenValue: elements['clone_source_pid'].value,
    statusText: elements['clone-status-text'].textContent,
    clearHidden: elements['clone-clear'].classList.contains('d-none')
  });
}

setCloneSource(23, 'คัดลอกจาก #23 แล้ว — ตรวจสอบก่อนบันทึก');
snapshot('armed');

setCloneSource(null, '');
snapshot('cleared');

setCloneSource(23, 'คัดลอกจาก #23 แล้ว — ตรวจสอบก่อนบันทึก');
setCloneSource(RETAIN, 'โหลดข้อมูลสินค้าไม่สำเร็จ');
snapshot('retain_with_armed');

setCloneSource(null, '');
setCloneSource(RETAIN, 'โหลดข้อมูลสินค้าไม่สำเร็จ');
snapshot('retain_with_nothing_armed');

process.stdout.write(JSON.stringify(results));
"""


def test_set_clone_source_executable_contract(admin_client, tmp_path):
    """Executable contract test (Codex review finding): runs the REAL
    rendered setCloneSource under node with a tiny DOM stub and drives it
    through all four call shapes, asserting OBSERVED STATE — not source
    substrings. This is what actually catches the two mutations the
    source-pin tests below (and the now-deleted
    test_retain_message_names_the_still_armed_source_on_failure) could not:

      1. `hidden.value =` -> `hidden.value ==` (a no-op comparison, thrown
         away) — the old pin `'hidden.value =' in helper_src` still matches
         `==` because it's a substring of it. Here, the 'armed' snapshot's
         hiddenValue would stay '' instead of becoming '23'.
      2. `var armedPid = hidden ? '' : hidden.value;` (the two ternary
         branches swapped) — every source pin that only checked 'hidden'
         and '.value' were *present* in the RETAIN section still passed,
         because both identifiers are still there, just on the wrong side.
         Here, armedPid becomes '' whenever a real element exists, so
         'retain_with_armed' would show the plain fallback message instead
         of naming #23.

    Node-absent handling: if `node` is not on PATH (and not at the
    Homebrew fallback), SKIP only this test — never fall back to a weaker
    check silently. The remaining source-substring tests in this file
    (test_clone_from_product_routes_state_through_set_clone_source,
    test_set_clone_source_call_shapes_are_unambiguous, the two _cloneSeq
    tests) still run unconditionally and catch outright DELETION of these
    lines even with node unavailable — they just cannot catch a same-
    length swap like this one.
    """
    node = _node_executable()
    if not node:
        pytest.skip(
            "node not found on PATH or at /opt/homebrew/bin/node — "
            "executable JS contract test skipped; the source-substring "
            "pins for setCloneSource still run and catch outright deletion"
        )

    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))
    retain_decl = _extract_retain_declaration(live)
    fn_src = _extract_js_function(live, 'setCloneSource')

    script = '\n'.join([_DOM_STUB, retain_decl, fn_src, _CONTRACT_DRIVER])
    script_path = tmp_path / 'set_clone_source_contract.js'
    script_path.write_text(script, encoding='utf-8')

    proc = subprocess.run(
        [node, str(script_path)], capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, (
        f"node execution of the extracted setCloneSource failed "
        f"(stderr):\n{proc.stderr}\n\n--- extracted script ---\n{script}"
    )
    results = {row['label']: row for row in json.loads(proc.stdout)}

    armed = results['armed']
    assert armed['hiddenValue'] == '23', (
        f"setCloneSource(23, ...) must write the hidden pid, got {armed!r} "
        "(catches `hidden.value =` -> `hidden.value ==`)"
    )
    assert armed['statusText'] == 'คัดลอกจาก #23 แล้ว — ตรวจสอบก่อนบันทึก'
    assert armed['clearHidden'] is False, "clear button must be shown once a source is armed"

    cleared = results['cleared']
    assert cleared['hiddenValue'] == ''
    assert cleared['statusText'] == ''
    assert cleared['clearHidden'] is True, "clear button must hide once disarmed"

    retain_armed = results['retain_with_armed']
    assert retain_armed['hiddenValue'] == '23', "RETAIN must keep whatever pid was armed"
    assert 'ยังคัดลอกจาก #23' in retain_armed['statusText'], (
        f"RETAIN with an armed source must name it in the message, got "
        f"{retain_armed!r} (catches "
        "`var armedPid = hidden ? '' : hidden.value;` — branches swapped, "
        "which would read the plain fallback instead of naming #23)"
    )
    assert retain_armed['clearHidden'] is False, "RETAIN must not touch clear-button visibility"

    retain_empty = results['retain_with_nothing_armed']
    assert retain_empty['hiddenValue'] == ''
    assert retain_empty['statusText'] == 'โหลดข้อมูลสินค้าไม่สำเร็จ', (
        "RETAIN with nothing armed must fall back to the plain error message"
    )


@pytest.fixture
def nonexistent_pid(tmp_db):
    """A pid guaranteed to name no product — derived from the live clone's
    own MAX(id) rather than a hardcoded magic number (never assume DB state
    blindly, verification-discipline.md)."""
    conn = sqlite3.connect(tmp_db)
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM products").fetchone()[0]
    conn.close()
    return max_id + 1_000_000


# ── Red #1: a valid clone_source_pid stamps manual_clone_<pid> ─────────────

def test_product_new_post_with_clone_source_pid_stamps_manual_clone(admin_client, clone_source_product):
    client, db_path = admin_client
    product_name = 'pytest PR5 clone stamps manual_clone'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': product_name,
        'unit_type': 'ตัว',
        'clone_source_pid': str(clone_source_product),
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_via FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == f'manual_clone_{clone_source_product}'


# ── Red #2: carry-through on BOTH failure re-renders, resubmit with the
#    EXTRACTED value (never the fixture's own pid — Codex R1 #7) ──────────

@pytest.mark.parametrize('bad_field', ['packaging_th', 'cost_price'])
def test_clone_source_pid_carries_through_validation_error_and_resubmit(
    admin_client, clone_source_product, bad_field
):
    """End-to-end provenance with a carry-through guard (verification
    contract #2). packaging_th fails at the DB layer (:387, DatabaseError
    via the CHECK trigger); cost_price='not-a-number' fails float() (:380,
    ValueError) — two DIFFERENT render call sites, both must carry the
    hidden field (Codex R3 #2: v3 never reached the :380 branch, so a
    missing kwarg there would have passed the whole suite silently)."""
    client, db_path = admin_client
    data = {
        'product_name': 'pytest PR5 carry-through',
        'unit_type': 'ตัว',
        'clone_source_pid': str(clone_source_product),
    }
    if bad_field == 'packaging_th':
        data['packaging_th'] = 'ไม่มีจริง'   # rejected by the CHECK trigger -> :387
    else:
        data['cost_price'] = 'not-a-number'   # float() raises -> :380

    resp = client.post('/products/new', data=data, follow_redirects=False)
    assert resp.status_code == 200, resp.data[:500]   # failure re-renders, never redirects
    html = resp.get_data(as_text=True)

    extracted_pid = _extract_clone_source_pid_input(html)
    assert extracted_pid == str(clone_source_product), (
        f"hidden clone_source_pid must carry the source pid through the "
        f"{bad_field} validation error; expected {clone_source_product!r}, "
        f"got {extracted_pid!r}"
    )

    # Resubmit with the EXTRACTED value, not the fixture's pid directly —
    # re-sending the fixture pid would pass this half even if carry-through
    # were removed entirely (Codex R1 #7).
    product_name = f'pytest PR5 carry-through resubmit {bad_field}'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp2 = client.post('/products/new', data={
        'product_name': product_name,
        'unit_type': 'ตัว',
        'clone_source_pid': extracted_pid,
    }, follow_redirects=False)
    assert resp2.status_code == 302, resp2.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_via FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == f'manual_clone_{clone_source_product}'


# ── Red #5: D10's server-rendered status must never claim provenance the
#    save will not produce — run across BOTH failure renders ──────────────

@pytest.mark.parametrize('bad_field', ['packaging_th', 'cost_price'])
def test_clone_status_server_renders_armed_state_on_failure_with_valid_pid(
    admin_client, clone_source_product, bad_field
):
    """A valid clone_source_pid surviving a validation error must be
    VISIBLE, not just present in the hidden field (Codex R1 #3): status
    text carries the source pid, the clear button is shown."""
    client, _db = admin_client
    data = {
        'product_name': 'pytest PR5 armed status',
        'unit_type': 'ตัว',
        'clone_source_pid': str(clone_source_product),
    }
    if bad_field == 'packaging_th':
        data['packaging_th'] = 'ไม่มีจริง'
    else:
        data['cost_price'] = 'not-a-number'

    resp = client.post('/products/new', data=data, follow_redirects=False)
    assert resp.status_code == 200, resp.data[:500]
    html = resp.get_data(as_text=True)

    assert _extract_clone_source_pid_input(html) == str(clone_source_product)

    status_text = _extract_clone_status_text(html)
    assert f'คัดลอกจาก #{clone_source_product}' in status_text

    clear_tag = _extract_clone_clear_tag(html)
    assert 'd-none' not in clear_tag, "clear button must be VISIBLE when a source is armed"


@pytest.mark.parametrize('bad_field', ['packaging_th', 'cost_price'])
def test_clone_status_server_renders_disarmed_state_on_failure_with_bad_pid(
    admin_client, nonexistent_pid, bad_field
):
    """D11: a pid that fails to resolve must leave NO provenance armed on
    the re-render — the page must never claim a clone the save will not
    produce. Hidden value empty, status carries no 'คัดลอกจาก', clear
    button present (D10a stable children) but hidden."""
    client, _db = admin_client
    data = {
        'product_name': 'pytest PR5 disarmed status',
        'unit_type': 'ตัว',
        'clone_source_pid': str(nonexistent_pid),
    }
    if bad_field == 'packaging_th':
        data['packaging_th'] = 'ไม่มีจริง'
    else:
        data['cost_price'] = 'not-a-number'

    resp = client.post('/products/new', data=data, follow_redirects=False)
    assert resp.status_code == 200, resp.data[:500]
    html = resp.get_data(as_text=True)

    hidden_val = _extract_clone_source_pid_input(html)
    assert hidden_val == '', f"expected empty hidden value for an unresolved pid, got {hidden_val!r}"

    status_text = _extract_clone_status_text(html)
    assert 'คัดลอกจาก' not in status_text

    clear_tag = _extract_clone_clear_tag(html)
    assert 'd-none' in clear_tag, "clear button must be present but HIDDEN when nothing is armed"


# ── Red #4: the hidden field + the live JS both arm and clear it ───────────

def test_product_new_get_renders_clone_source_pid_hidden_input(admin_client):
    """Review finding 2: checking only `id=` is satisfied even with
    `name="clone_source_pid"` removed — every route test in this file POSTs
    the field directly by name, so a missing `name` would only ever show up
    in a real browser submit (which never reaches these tests). Assert the
    attributes that actually make it a POSTed form field."""
    client, _db = admin_client
    html = client.get('/products/new').get_data(as_text=True)
    tags = _HIDDEN_CLONE_PID_TAG_RE.findall(html)
    assert len(tags) == 1, f"expected exactly one clone_source_pid input, found {len(tags)}"
    assert 'type="hidden"' in tags[0], f"must be type=hidden: {tags[0]!r}"
    assert 'name="clone_source_pid"' in tags[0], f"must POST as clone_source_pid: {tags[0]!r}"


def test_clone_from_product_routes_state_through_set_clone_source(admin_client):
    """D10a: clone-status is owned by JS — cloneFromProduct's
    `status.textContent = ...` writes (form.html:513,537,542 on
    origin/main) DELETE every child, so a server-rendered clear BUTTON
    placed inside clone-status would be wiped by the next clone. Every
    state change must route through ONE helper, setCloneSource(), so the
    hidden value / text / button stay in sync — scoped to cloneFromProduct
    only (parseRawName legitimately still writes its OWN #parse-status
    through a same-named `status` variable — do not touch that, per the
    plan's explicit note)."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))
    fn_src = _extract_js_function(live, 'cloneFromProduct')
    assert 'textContent =' not in fn_src, (
        "cloneFromProduct must not write .textContent directly — route "
        "through setCloneSource() (D10a)"
    )
    assert 'function setCloneSource(' in live, "setCloneSource helper must exist"

    # Review finding 2 originally added three "does the body CONTAIN these
    # substrings" assertions here, because the two above are satisfied even
    # with setCloneSource's ENTIRE BODY deleted. That's now proven the
    # stronger way — by actually CALLING the helper and observing the
    # hidden value / status text / clear-button visibility change — in
    # test_set_clone_source_executable_contract below. An empty
    # setCloneSource fails that test outright (every snapshot would show
    # the untouched initial state), so the source-substring version here
    # would be redundant with it and was removed.


def test_set_clone_source_call_shapes_are_unambiguous(admin_client):
    """Pins the THREE call shapes verification contract #4 requires, fixed
    so there is exactly one reading (Codex R5 — folding R3 then R4 left a
    v5 draft requiring `setCloneSource(null` in the failure paths on one
    line and forbidding it two lines later):
      - success (cloneFromProduct's .then): setCloneSource(pid, ...)
      - BOTH failure paths (spec.error branch + .catch):
        setCloneSource(RETAIN, ...) — a named sentinel, never null
      - the clone-clear listener: setCloneSource(null, ...) — the ONLY
        null call in the file
    A test demanding merely 'some setCloneSource(' call is satisfied by
    the success call alone (Codex R3 #3) — so pin the COUNTS, not just
    presence."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))

    fn_src = _extract_js_function(live, 'cloneFromProduct')
    assert 'setCloneSource(pid' in fn_src, (
        "cloneFromProduct's success path must call setCloneSource(pid, ...)"
    )

    assert live.count('setCloneSource(null') == 1, (
        "setCloneSource(null must appear EXACTLY ONCE — the clone-clear "
        "listener disarming. A version demanding merely 'some "
        "setCloneSource(' call would pass on the success call alone."
    )
    assert live.count('setCloneSource(RETAIN') == 2, (
        "both failure paths (spec.error branch and .catch) must call "
        "setCloneSource(RETAIN, ...) — never null, which would disarm a "
        "still-valid clone after a later unrelated failure (D10b/Codex R4)"
    )
    assert "getElementById('clone-clear')" in live, (
        "a ล้าง listener wired to #clone-clear must exist to call "
        "setCloneSource(null, ...) — the only path that disarms (D10c)"
    )

    # Review finding 2: 'setCloneSource(RETAIN' count == 2 still passes if
    # `var RETAIN = {};` itself is deleted — the two call sites still read
    # as literal text, but at runtime RETAIN is then undefined and every
    # clone-failure path throws a ReferenceError before setCloneSource ever
    # runs. Pin the declaration, not just its use.
    assert 'var RETAIN = {}' in live, "RETAIN sentinel must be declared (undeclared use throws ReferenceError at runtime)"


def test_clone_seq_guard_checked_before_state_write_in_then_and_catch(admin_client):
    """D7: cloneFromProduct fires an async fetch with no request guard —
    select A, then select B (B resolves before A) arms A over B, and
    clicking ล้าง mid-flight is undone when a stale fetch lands. The fix
    is a monotonic _cloneSeq; EVERY success/failure callback must capture
    it and bail out early if stale, BEFORE writing any state. Pinned as a
    SOURCE-ORDER check (no JS runner in this repo — same idiom as
    test_preview_drops_out_of_order_responses above), and the .then/.catch
    callbacks are checked SEPARATELY (Codex R4: one combined assertion
    lets an implementer guard .then and leave .catch open)."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))
    fn_src = _extract_js_function(live, 'cloneFromProduct')

    then_body = _extract_callback_body(fn_src, '.then(function (spec)')
    catch_body = _extract_callback_body(fn_src, '.catch(function (')

    for label, body in (('.then', then_body), ('.catch', catch_body)):
        guard_idx = body.index('seq !== _cloneSeq')
        write_idx = body.index('setCloneSource(')
        assert guard_idx < write_idx, (
            f"{label} callback: the _cloneSeq guard must sit BEFORE the "
            f"first setCloneSource(...) state write"
        )
        assert 'return' in body[guard_idx:write_idx], (
            f"{label} callback: the _cloneSeq check must actually `return` "
            f"— found the comparison but no bail-out before the state write"
        )


def test_clone_seq_increments_on_clone_start_and_on_clear(admin_client):
    """D7 + review finding (mutation 3): _cloneSeq must be incremented in
    TWO DIFFERENT PLACES — once inside cloneFromProduct (clone-start) and
    once inside the ล้าง click handler (clear) — same idiom as
    _pnPreviewSeq (form.html:457-505), which this file already exercises
    above.

    A file-wide `live.count('++_cloneSeq') == 2` is satisfied even when
    BOTH increments sit inside cloneFromProduct and the clear handler has
    none: clicking ล้าง then no longer invalidates an in-flight clone
    fetch, so a stale response landing after the clear silently re-arms
    the source the operator just cleared. Scope each count to its own
    function body so that move cannot hide."""
    client, _db = admin_client
    live = _live_js(client.get('/products/new').get_data(as_text=True))

    clone_fn_src = _extract_js_function(live, 'cloneFromProduct')
    assert clone_fn_src.count('++_cloneSeq') == 1, (
        "cloneFromProduct must increment _cloneSeq exactly once (clone-start) — D7"
    )

    clear_handler_src = _extract_callback_body(
        live, "clearBtn.addEventListener('click', function ()"
    )
    assert clear_handler_src.count('++_cloneSeq') == 1, (
        "the ล้าง click handler must increment _cloneSeq exactly once "
        "(clear) — D7. A version that moves this increment next to the "
        "clone-start one leaves the file-wide count at 2 while silently "
        "losing clear's invalidation of an in-flight fetch."
    )

    # Review finding 2: the counts above still pass if `var _cloneSeq = 0;`
    # itself is deleted — every `++_cloneSeq` occurrence is still literal
    # text, but at runtime the first clone throws a ReferenceError before
    # `fetch` is even called (reading an undeclared identifier to increment
    # it is not the same as assigning one). Pin the declaration too.
    assert 'var _cloneSeq = 0' in live, "_cloneSeq must be declared (undeclared use throws ReferenceError at runtime)"


# test_retain_message_names_the_still_armed_source_on_failure (Review
# finding 1) used to live here as a source-only pin on setCloneSource's
# RETAIN branch. It only checked that 'hidden' and '.value' were PRESENT
# between the RETAIN guard and the still-armed message — which stays true
# even with the branches of `var armedPid = hidden ? '' : hidden.value;`
# SWAPPED (both identifiers are still there, just on the wrong side).
# test_set_clone_source_executable_contract above supersedes it by
# actually calling setCloneSource(RETAIN, ...) with a source armed and
# asserting the rendered message names it — which that swap DOES break.
# Deleted rather than kept alongside the stronger executable test.


# ── Regression / already-green (#6-#9): the fallback already exists on
#    origin/main (:384 hardcodes 'manual' and nothing reads the field) —
#    these pin it does NOT regress once the resolver lands, they are not
#    expected to be red now ─────────────────────────────────────────────

def test_product_new_post_with_nonexistent_clone_source_pid_falls_back_to_manual(
    admin_client, nonexistent_pid
):
    """#6 (fallback half only — see the JUDGMENT CALL note on the sibling
    test below): a pid that resolves to no product still creates the row
    as plain 'manual'. This half is genuinely already green on
    origin/main — :384 hardcodes 'manual' regardless of what
    clone_source_pid carries."""
    client, db_path = admin_client
    product_name = 'pytest PR5 nonexistent pid fallback'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': product_name,
        'unit_type': 'ตัว',
        'clone_source_pid': str(nonexistent_pid),
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_via FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == 'manual'


def test_product_new_post_with_nonexistent_clone_source_pid_logs_warning(
    admin_client, nonexistent_pid, caplog
):
    """#6 (warning half) / D4: "log a warning when a non-blank value fails
    to resolve." JUDGMENT CALL: the plan's verification contract lists this
    warning as part of item #6 and buckets #6-9 together as "already
    green," but the warning itself is NEW behavior — nothing on
    origin/main calls current_app.logger.warning for clone_source_pid, so
    this half is genuinely RED today, unlike the fallback half above.
    Split into its own test so the "already green" claim in the sibling
    test above is actually true as written, and this one is honestly
    reported as red-first."""
    client, db_path = admin_client
    product_name = 'pytest PR5 nonexistent pid warning'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        resp = client.post('/products/new', data={
            'product_name': product_name,
            'unit_type': 'ตัว',
            'clone_source_pid': str(nonexistent_pid),
        }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    assert any(
        str(nonexistent_pid) in r.getMessage() and r.levelno >= logging.WARNING
        for r in caplog.records
    ), "a non-blank clone_source_pid that fails to resolve must log a warning (D4)"


@pytest.mark.parametrize('bad_value', ['abc', ''])
def test_product_new_post_with_non_numeric_clone_source_pid_falls_back_no_500(admin_client, bad_value):
    """#7: a non-numeric pid ('abc' or '') falls back to plain 'manual' —
    and critically must NOT 500."""
    client, db_path = admin_client
    product_name = f'pytest PR5 non-numeric fallback {bad_value or "blank"}'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': product_name,
        'unit_type': 'ตัว',
        'clone_source_pid': bad_value,
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_via FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == 'manual'


def test_product_new_post_with_oversized_clone_source_pid_falls_back_no_500(admin_client):
    """#8: '9' * 100 parses fine in Python's int() and then raises
    OverflowError when bound to a SQLite query — NOT a sqlite3.DatabaseError,
    so the route's existing `except` would not catch it without an explicit
    range check (D4, verified empirically: int('9'*100) raises OverflowError,
    and isinstance(e, sqlite3.DatabaseError) is False). This is a guard
    against a SPECIFIC implementation mistake: there is no query to overflow
    on origin/main today, but the moment the resolver is added WITHOUT the
    range check, this 500s — which is what break-it-once at review time
    proves."""
    client, db_path = admin_client
    product_name = 'pytest PR5 oversized pid fallback'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': product_name,
        'unit_type': 'ตัว',
        'clone_source_pid': '9' * 100,
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_via FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == 'manual'


def test_product_new_post_without_clone_source_pid_key_defaults_to_manual(admin_client):
    """#9: no clone_source_pid key at all (pre-existing POSTs from before
    this feature, or a form submitted with JS disabled) → plain 'manual',
    same as today."""
    client, db_path = admin_client
    product_name = 'pytest PR5 no clone_source_pid key'
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM products WHERE product_name = ?", (product_name,))
    conn.commit()
    conn.close()

    resp = client.post('/products/new', data={
        'product_name': product_name,
        'unit_type': 'ตัว',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT created_via FROM products WHERE product_name = ?", (product_name,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == 'manual'
