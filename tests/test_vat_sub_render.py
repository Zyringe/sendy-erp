"""vat-substitute — real Flask-test-client render checks.

The unit-test suites (test_vat_sub_*.py) exercise the Python business logic
directly and never render Jinja — so a template bug like accessing a dict's
'items' KEY via `.items` (which Jinja resolves to the dict's bound `.items()`
METHOD instead, per its attribute-then-subscript lookup) is invisible to
them. Caught only by an actual GET through the real app during the manual
port-5003 smoke test; these pin it so it can't regress silently again."""
import json
import os
import re
import shutil
import subprocess
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


def _login(client, role='admin'):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = f'test-{role}'
        sess['role'] = role


@pytest.fixture
def route_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def _seed_identity_mapped_product(conn):
    """A product with an xp5 identity mapping — exercises the own-stock
    card + the STKGRP-bridge guess path (the exact page that 500'd)."""
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('สินค้าทดสอบ render', 'ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('RENDER1', ?, 'reviewed', 'manual')", (pid,))
    conn.commit()
    return pid


def test_index_renders(route_client, tmp_db):
    _login(route_client)
    r = route_client.get('/vat-sub')
    assert r.status_code == 200


def test_product_view_renders_with_identity_mapping_and_no_vat_book(route_client, tmp_db):
    """No vat_book.db exists in this tmp env -> own_card/candidates/guesses
    all take their 'book unavailable' branches. Exactly this combination
    (guesses = {'empty': True, 'reason': ...}) is what originally 500'd
    (unrelated bug: 'items' key vs dict.items() method) once the book WAS
    present and guesses.items() no longer took the empty branch."""
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
    assert 'สินค้าทดสอบ render' in r.get_data(as_text=True)


def test_product_view_renders_with_real_guess_items(route_client, tmp_db, tmp_path, monkeypatch):
    """Build a real (lightweight) vat_book.db so guesses['items'] is a
    non-empty list — the exact branch that 500'd before the fix (Jinja
    resolved `guesses.items` to the dict's bound .items() method)."""
    import sqlite3
    import config
    import book_registry

    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()

    book_path = book_registry.book_db_path('vat')
    bc = sqlite3.connect(book_path)
    bc.executescript("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'ตัว',
            cost_price REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE product_code_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT NOT NULL, bsn_name TEXT NOT NULL, product_id INTEGER,
            bsn_unit TEXT NOT NULL DEFAULT '');
        CREATE TABLE stock_levels (
            product_id INTEGER PRIMARY KEY, quantity REAL NOT NULL DEFAULT 0);
        CREATE TABLE stmas_meta (
            stkcod TEXT PRIMARY KEY, stkgrp TEXT NOT NULL, vatcod TEXT NOT NULL);
        CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    own_pid = bc.execute("INSERT INTO products (product_name) VALUES ('ของตัวเอง')").lastrowid
    bc.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES ('RENDER1', 'ของตัวเอง', ?)", (own_pid,))
    bc.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (own_pid,))
    bc.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES ('RENDER1', '57', '1')")
    guess_pid = bc.execute("INSERT INTO products (product_name) VALUES ('ตัวแทนเดา')").lastrowid
    bc.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES ('GUESS1', 'ตัวแทนเดา', ?)", (guess_pid,))
    bc.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 3)", (guess_pid,))
    bc.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES ('GUESS1', '57', '1')")
    bc.execute("INSERT INTO book_meta VALUES ('built_at', '2026-08-05T00:00:00')")
    bc.commit()
    bc.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'GUESS1' in html
    assert own_pid or guess_pid  # keep linters quiet about unused


def test_planning_renders(route_client, tmp_db):
    _login(route_client)
    r = route_client.get('/vat-sub/planning')
    assert r.status_code == 200


def test_group_detail_renders(route_client, tmp_db):
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    gid = conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลุ่มทดสอบ render')").lastrowid
    conn.commit()
    conn.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/group/{gid}')
    assert r.status_code == 200
    assert 'กลุ่มทดสอบ render' in r.get_data(as_text=True)


def test_group_detail_renders_move_member_form_when_another_group_exists(route_client, tmp_db):
    """The move-member route (models.move_member) has a UI trigger — a
    group page with >=1 other group and a member must render a form
    posting to vat_sub.group_move_member (caught missing during the manual
    port-5003 smoke test: the route existed but no template linked to it)."""
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    g1 = conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลุ่ม 1')").lastrowid
    conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลุ่ม 2')")
    conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'MV1', 'manual')", (g1,))
    conn.commit()
    conn.close()

    _login(route_client)
    r = route_client.get(f'/vat-sub/group/{g1}')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert f'action="/vat-sub/group/{g1}/move-member"' in html
    assert 'กลุ่ม 2' in html


def test_staff_cannot_post_promote(route_client, tmp_db):
    _login(route_client, role='staff')
    r = route_client.post('/vat-sub/promote', data={'product_id': '1', 'xp5_code': 'X1'})
    # access_control redirects (not 403) for a POST outside the role whitelist
    assert r.status_code in (302, 403)


def test_badge_js_compares_base_unit_not_selected_unit(route_client, tmp_db):
    """Codex r1 finding 1 (decision 10): the price is divided back to X's
    BASE unit before comparing, so the unit-compatibility check must use
    products.unit_type — the SELECTED deal unit only supplies the ratio.
    Selecting โหล (ratio 12) against a ตัว candidate must NOT flip the badge
    to "เทียบไม่ได้"."""
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()
    _login(route_client)
    html = route_client.get(f'/vat-sub/product/{pid}').get_data(as_text=True)
    # tojson escapes Thai to \uXXXX (Flask ensure_ascii) — assert the WIRING:
    # a base-unit constant exists, feeds computeBadge, and the old
    # selected-unit variable is gone entirely.
    assert 'const X_BASE_UNIT = ' in html
    assert 'computeBadge(price, ratio, X_BASE_UNIT,' in html
    assert 'opt.value' not in html


# ── the badge JS (#485): the one implementation, pinned by executing it ────
#
# Until #485 a Python compute_badge claimed to be its line-for-line twin. It
# had no production caller and had drifted (a zero price, a blank unit and a
# unit with a trailing space all disagreed), so it was deleted and the JS the
# user runs is the copy these tests pin. Its multiplier comes from vat_math,
# rendered into the page.

_JS_COMMENT = re.compile(r'/\*.*?\*/|(?<!:)//[^\n]*', re.S)


def _product_view_html(route_client):
    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)          # unit_type 'ตัว'
    conn.close()
    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _badge_script(html):
    scripts = [s for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
               if 'function computeBadge(' in s]
    assert len(scripts) == 1, f'expected the one badge script, found {len(scripts)}'
    return scripts[0]


# name -> the page state before load: the price box, the unit <select>
# (value, data-ratio as the page renders it) + which option is selected, and
# the badge spans (data-unit, data-cost as rendered). X_BASE_UNIT is the seeded
# product's own unit_type, ตัว. `steps` then type a price / switch the unit.
BADGE_CASES = {
    'ok':                    dict(price='107.00', units=[['ตัว', '1.0']], badges=[['ตัว', '80.0']]),
    'warn at equality':      dict(price='107.00', units=[['ตัว', '1.0']], badges=[['ตัว', '100.0']]),
    'derived unit (โหล)':    dict(price='214.00', units=[['ตัว', '1.0'], ['โหล', '12.0']], pick=1,
                                  badges=[['ตัว', '10.0']]),
    'zero price':            dict(price='0.00', units=[['ตัว', '1.0']], badges=[['ตัว', '80.0']]),
    'missing price':         dict(price='', units=[['ตัว', '1.0']], badges=[['ตัว', '80.0']]),
    'unit mismatch':         dict(price='107.00', units=[['ตัว', '1.0']], badges=[['แผง', '80.0']]),
    'missing price, unit mismatch': dict(price='', units=[['ตัว', '1.0']], badges=[['แผง', '80.0']]),
    'unknown cost':          dict(price='107.00', units=[['ตัว', '1.0']], badges=[['ตัว', '0']]),
    'unit with a trailing space': dict(price='107.00', units=[['ตัว', '1.0']], badges=[['ตัว ', '80.0']]),
    'type a price, then switch unit': dict(
        price='107.00', units=[['ตัว', '1.0'], ['โหล', '12.0']], badges=[['ตัว', '80.0']],
        steps=[{'price': '856'}, {'pick': 1}]),
}

_OK, _WARN, _GREY = 'vs-badge badge bg-success', 'vs-badge badge bg-warning text-dark', 'vs-badge badge bg-secondary'

# Hand arithmetic, and what the page showed before #485 (triage, node v25):
# 107 / 1.07 = 100 · 214 / 1.07 / 12 = 16.67 · 856 / 1.07 = 800 · 800 / 12 = 66.67
BADGE_EXPECTED = {
    'ok':                    [[['✅ คุ้ม (100.00 > 80.00)', _OK]]],
    'warn at equality':      [[['⚠️ ไม่คุ้ม (100.00 ≤ 100.00)', _WARN]]],
    'derived unit (โหล)':    [[['✅ คุ้ม (16.67 > 10.00)', _OK]]],
    'zero price':            [[['เทียบไม่ได้', _GREY]]],
    'missing price':         [[['เทียบไม่ได้', _GREY]]],
    'unit mismatch':         [[['เทียบไม่ได้ (หน่วยต่างกัน)', _GREY]]],
    'missing price, unit mismatch': [[['เทียบไม่ได้', _GREY]]],
    'unknown cost':          [[['❓ ไม่ทราบต้นทุน', _GREY]]],
    'unit with a trailing space': [[['เทียบไม่ได้ (หน่วยต่างกัน)', _GREY]]],
    'type a price, then switch unit': [
        [['✅ คุ้ม (100.00 > 80.00)', _OK]],
        [['✅ คุ้ม (800.00 > 80.00)', _OK]],
        [['⚠️ ไม่คุ้ม (66.67 ≤ 80.00)', _WARN]],
    ],
}

# A stand-in for the few DOM calls the script makes. Each case gets a fresh
# document, runs the script (its IIFE does the on-load recompute), then fires
# the listeners the script registered — so a listener bound to the wrong
# element or event throws instead of passing.
_HARNESS = r'''
const script = %s, cases = %s, out = {};
for (const [name, c] of Object.entries(cases)) {
  const listeners = {};
  const on = key => (ev, fn) => { listeners[key + ':' + ev] = fn; };
  const price = {value: c.price, addEventListener: on('price')};
  const unit = {selectedIndex: c.pick || 0, addEventListener: on('unit'),
                options: c.units.map(([u, r]) => ({value: u,
                  getAttribute: k => (k === 'data-ratio' ? r : null)}))};
  const badges = c.badges.map(([u, cost]) => ({textContent: 'คำนวณ...', className: 'vs-badge',
    getAttribute: k => ({'data-unit': u, 'data-cost': cost})[k] ?? null}));
  const document = {
    getElementById: id => ({'vs-price': price, 'vs-unit': unit})[id] || null,
    querySelectorAll: sel => { if (sel !== '.vs-badge') throw new Error(sel); return badges; },
  };
  const snap = () => badges.map(b => [b.textContent, b.className]);
  new Function('document', script)(document);
  const snaps = [snap()];
  for (const s of c.steps || []) {
    if ('price' in s) { price.value = s.price; listeners['price:input'](); }
    if ('pick' in s) { unit.selectedIndex = s.pick; listeners['unit:change'](); }
    snaps.push(snap());
  }
  out[name] = snaps;
}
console.log(JSON.stringify(out));
'''


def run_badge_script(script, node, cases=BADGE_CASES):
    """Every case's badge (label, class) after load and after each step."""
    proc = subprocess.run([node, '-e', _HARNESS % (json.dumps(script), json.dumps(cases))],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_badge_multiplier_and_label_come_from_vat_math(route_client, tmp_db):
    """Source level, runs everywhere (no node needed): the page renders
    vat_math's multiplier into the script and into the label above the price
    box, and the badge divides by the rendered constant, not a typed one."""
    html = _product_view_html(route_client)
    code = _JS_COMMENT.sub('', _badge_script(html))
    assert 'function recompute(' in code, 'CONTROL: the comment strip ate the script'
    assert 'const VAT_MULTIPLIER = 1.07;' in code
    assert 'pricePerUnit / VAT_MULTIPLIER' in code
    labels = re.findall(r'<div class="col-auto text-muted small">\s*(.*?)\s*</div>', html, re.S)
    assert labels == ['ราคาจ่ายจริง ÷ 1.07 (ไม่รวม VAT) ÷ อัตราแปลงหน่วย แล้วเทียบกับต้นทุนในสมุด VAT']


def test_badge_js_labels_every_case_exactly_as_before(route_client, tmp_db):
    """Executes the rendered badge script. ⚠ Skips without node, so it is a
    bonus rather than the floor — the source-level test above runs everywhere."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('node not available — the source-level assertions still ran')
    got = run_badge_script(_badge_script(_product_view_html(route_client)), node)
    assert len(got) == len(BADGE_EXPECTED) == 10
    assert got == BADGE_EXPECTED


def test_product_view_renders_200_when_book_predates_stmas_meta(route_client, tmp_db, tmp_path, monkeypatch):
    """A vat_book built by the pre-#368 builder has no stmas_meta table —
    exactly prod's state between the merge and the next team upload. Every
    read path LEFT JOINs stmas_meta, so such a book must be treated as
    NOT READY (open_vat_book -> None => graceful 'ยังไม่ถูกสร้าง/ยังไม่พร้อม'
    states), never a 500 on a nav-reachable page."""
    import sqlite3
    import config
    import models.vat_sub as vs
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    pid = _seed_identity_mapped_product(conn)
    conn.close()

    old_book = tmp_path / 'old_book.db'
    c = sqlite3.connect(old_book)
    c.executescript("""
        CREATE TABLE products (id INTEGER PRIMARY KEY, product_name TEXT, unit_type TEXT, cost_price REAL);
        CREATE TABLE product_code_mapping (id INTEGER PRIMARY KEY, bsn_code TEXT, bsn_name TEXT, product_id INTEGER);
        CREATE TABLE stock_levels (product_id INTEGER PRIMARY KEY, quantity REAL);
    """)
    c.close()
    monkeypatch.setattr(vs.book_registry, 'book_db_path', lambda kind: str(old_book))

    _login(route_client)
    r = route_client.get(f'/vat-sub/product/{pid}')
    assert r.status_code == 200
