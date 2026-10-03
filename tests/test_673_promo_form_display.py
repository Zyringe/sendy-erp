"""#673 PR 2: the promo minimum in the UI.

- `price_lookup.min_qty_units` is THE list of units a minimum may be written
  in for a product, as (word, pieces per unit) pairs (the ones `measure_ratio`
  resolves, base unit first); `min_qty_unit_problem` refuses everything
  outside it.
- The promo form offers exactly that list in its unit <select>, each option
  labelled with its ratio, behind an empty placeholder that stays selected
  unless the posted unit is in the list (a stale unit falls back to blank,
  never to the first real unit). It has no `bundle_condition` select.
- One rule in every render (product detail's active row and history, the
  call card's promo_detail macro): the minimum when there is one, else the
  legacy ยกลัง/ยกล่อง label when there is one, whatever the promo type (a
  bundle / gift / mixed-with-freebie row cannot carry a minimum; a labelled
  price row with none is priced at list, promo_min_missing).

Every product is fresh (mig 177 allows one current price promo per product).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from html import unescape
import re
import sqlite3

import pytest

_pid = [981000]


def _conn(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    return c


def _product(tmp_db, unit_type='อัน', rows=(), tiers=()):
    _pid[0] += 1
    c = _conn(tmp_db)
    pid = c.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, ?, 100, 60, 1)", (f'display673 #{_pid[0]}', unit_type)).lastrowid
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
                     "FROM promotions WHERE product_id = ? ORDER BY id", (pid,)).fetchall()
    c.close()
    return [tuple(r) for r in rows]


def _units(tmp_db, pid):
    import price_lookup
    c = _conn(tmp_db)
    try:
        unit_type = c.execute("SELECT unit_type FROM products WHERE id = ?", (pid,)).fetchone()[0]
        return price_lookup.min_qty_units(c, pid, unit_type)
    finally:
        c.close()


def _words(tmp_db, pid):
    return [w for w, _ratio in _units(tmp_db, pid)]


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 99
        sess['username'] = 'x'
        sess['role'] = 'admin'
    return c


def _post(client, pid, **form):
    data = {'promo_name': 'form673', 'promo_type': 'percent', 'discount_value': '5', **form}
    return client.post(f'/products/{pid}/promotions/new', data=data)


def _select_options(html, name):
    """[(value, label, selected)] of the <select name=...>."""
    m = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f'no <select name="{name}"> in the page'
    return [(v, ' '.join(label.split()), bool(sel.strip()))
            for v, sel, label in re.findall(r'<option value="([^"]*)"([^>]*)>(.*?)</option>',
                                            m.group(1), re.S)]


def _selected(html, name):
    return [v for v, _label, sel in _select_options(html, name) if sel]


def _active_row(html):
    """The info card's โปรโมชัน row of /products/<id>."""
    m = re.search(r'<td class="text-subtle">โปรโมชัน</td>(.*?)</tr>', html, re.S)
    assert m, 'no active-promo row on the page'
    return m.group(1)


def _history_row(html, promo_name):
    """One row of the promo history table, found by its name cell."""
    m = re.search(rf'<td class="fw-500">{re.escape(promo_name)}</td>(.*?)</tr>', html, re.S)
    assert m, f'no history row for {promo_name!r}'
    return m.group(1)


def _script(html):
    """The page's inline scripts with JS comments stripped: a phrase that only
    survives in a comment must not pass."""
    body = '\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S))
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    body = re.sub(r'(?m)^\s*//.*$', '', body)
    assert 'function updatePreview' in body, 'control: the stripped script kept the code'
    return body


# ── the helper ───────────────────────────────────────────────────────────────

def test_min_qty_units_is_base_first_and_only_measurable_units(tmp_db):
    """A ลัง tier with no ratio answers a price but cannot measure a minimum,
    so it is not offered; the dozen row is."""
    pid = _product(tmp_db, rows=[('โหล', 12.0)], tiers=[('1 ลัง', 900.0)])
    assert _units(tmp_db, pid) == [('อัน', 1.0), ('โหล', 12.0)]


def test_min_qty_units_includes_a_tier_implied_dozen(tmp_db):
    """pid 307's shape: no โหล row, a '1 โหล' tier: the dozen measures as 12."""
    pid = _product(tmp_db, unit_type='ดอก', tiers=[('1 โหล', 120.0)])
    assert _units(tmp_db, pid) == [('ดอก', 1.0), ('โหล', 12.0)]


def test_min_qty_units_equals_what_min_qty_unit_problem_accepts(tmp_db):
    import bsn_units
    import price_lookup
    pid = _product(tmp_db, rows=[('หล', 6.0), ('กล่อง', 0.0), ('แผง', 10.0)],
                   tiers=[('1 ลัง', 900.0), ('1 โหล', 50.0)])
    c = _conn(tmp_db)
    try:
        candidates = {'อัน', 'ตัว', 'หล', 'โหล', 'กล่อง', 'แผง', 'ลัง', 'ชิ้น', 'ม้วน'}
        # the writer validates the normalised word it will store (min_qty_problem)
        words = {bsn_units.normalize_unit(u, conn=c) for u in candidates}
        accepted = {w for w in words
                    if price_lookup.min_qty_unit_problem(c, pid, 'อัน', w) is None}
        pairs = price_lookup.min_qty_units(c, pid, 'อัน')
        ratios = {w: price_lookup.measure_ratio(c, pid, 'อัน', w) for w, _r in pairs}
        refusal = price_lookup.min_qty_unit_problem(c, pid, 'อัน', 'ม้วน')
    finally:
        c.close()
    listed = [w for w, _r in pairs]
    assert dict(pairs) == ratios                   # each ratio is measure_ratio's own
    assert listed[0] == 'อัน' and pairs[0][1] == 1.0
    assert len(listed) == len(set(listed))
    assert set(listed) == accepted
    assert {'อัน', 'โหล', 'แผง'} <= accepted and 'ลัง' not in accepted and 'กล่อง' not in accepted
    # the refusal names exactly that list
    assert refusal.endswith('หน่วยที่ใช้ได้: ' + ', '.join(listed))


# ── the form (GET) ───────────────────────────────────────────────────────────

def test_form_offers_exactly_the_measurable_units(admin_client, tmp_db):
    pid = _product(tmp_db, rows=[('โหล', 12.0)], tiers=[('1 ลัง', 900.0)])
    resp = admin_client.get(f'/products/{pid}/promotions/new')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert re.search(r'<input[^>]*name="min_qty"', html)
    options = _select_options(html, 'min_qty_unit')
    assert [v for v, _l, _s in options] == [''] + _words(tmp_db, pid) == ['', 'อัน', 'โหล']
    assert [label for _v, label, _s in options] == ['— หน่วย —', 'อัน', 'โหล (= 12 อัน)']
    assert _selected(html, 'min_qty_unit') == ['']     # placeholder until a unit is picked
    group = re.search(r'id="minQtyGroup"(.*?)</div>\s*</div>', html, re.S)
    assert group, 'no minimum group on the form'
    assert 'ขั้นต่ำ (ไม่บังคับ)' in group.group(1)
    assert 'ไม่ถึงขั้นต่ำ ลูกค้าจ่ายราคาปกติ' in group.group(1)


def test_form_has_no_bundle_condition_select(admin_client, tmp_db):
    pid = _product(tmp_db)
    html = admin_client.get(f'/products/{pid}/promotions/new').get_data(as_text=True)
    assert 'name="promo_type"' in html            # control: the form rendered
    assert 'bundle_condition' not in html
    assert 'ต้องซื้อยกลัง' not in html and 'ต้องซื้อยกล่อง' not in html


def test_form_repopulates_the_minimum_after_a_refusal(admin_client, tmp_db):
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    resp = _post(admin_client, pid, discount_value='150', min_qty='20', min_qty_unit='โหล')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert re.search(r'<input[^>]*name="min_qty"[^>]*value="20"', html)
    assert _selected(html, 'min_qty_unit') == ['โหล']
    assert _promos(tmp_db, pid) == []


def test_form_re_render_drops_a_unit_not_in_the_list(admin_client, tmp_db):
    """Sol: a refused '2 ลัง' re-rendered as '2 อัน' (the first real option),
    one click from saving a minimum the operator never typed. A posted unit
    outside the list falls back to the blank placeholder."""
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    resp = _post(admin_client, pid, min_qty='2', min_qty_unit='ลัง')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert re.search(r'<input[^>]*name="min_qty"[^>]*value="2"', html)   # control: re-rendered
    assert _selected(html, 'min_qty_unit') == ['']
    assert _promos(tmp_db, pid) == []


def test_form_preview_script_states_the_minimum(admin_client, tmp_db):
    """The JS preview is driven in a browser by verify-sendy; this pins that
    the shipped script carries the condition text and listens on both inputs."""
    pid = _product(tmp_db)
    js = _script(admin_client.get(f'/products/{pid}/promotions/new').get_data(as_text=True))
    assert 'ต่ำกว่านั้นราคาปกติ' in js
    assert "getElementById('minQty').addEventListener" in js
    assert "getElementById('minQtyUnit').addEventListener" in js


def test_form_preview_omits_the_list_clause_without_a_list_price(admin_client, tmp_db):
    """An unpriced product (basePrice <= 0) must not read 'ต่ำกว่านั้นราคาปกติ
    ฿0.00': the clause is guarded on basePrice > 0. The script's behaviour is
    driven in a browser by verify-sendy; this pins the guard's place."""
    pid = _product(tmp_db)
    js = _script(admin_client.get(f'/products/{pid}/promotions/new').get_data(as_text=True))
    stmts = [line for line in js.splitlines() if 'ต่ำกว่านั้นราคาปกติ' in line]
    assert len(stmts) == 1
    assert 'basePrice > 0' in stmts[0]


# ── the form (POST) ──────────────────────────────────────────────────────────

def test_post_percent_with_a_minimum_saves_it(admin_client, tmp_db):
    pid = _product(tmp_db)
    resp = _post(admin_client, pid, min_qty='20', min_qty_unit='อัน')
    assert resp.status_code == 302
    assert _promos(tmp_db, pid) == [('percent', 5.0, 20.0, 'อัน', None)]


def test_post_a_unit_that_does_not_resolve_is_refused(admin_client, tmp_db):
    import price_lookup
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    resp = _post(admin_client, pid, min_qty='1', min_qty_unit='ลัง')
    assert resp.status_code == 200
    c = _conn(tmp_db)
    try:
        expected = price_lookup.min_qty_unit_problem(c, pid, 'อัน', 'ลัง')
    finally:
        c.close()
    # the flash is HTML-escaped (the message quotes the unit)
    assert expected and expected in unescape(resp.get_data(as_text=True))
    assert _promos(tmp_db, pid) == []


def test_post_blank_quantity_and_placeholder_unit_means_no_minimum(admin_client, tmp_db):
    """What the form posts when the group is left alone: '' and ''."""
    pid = _product(tmp_db, rows=[('โหล', 12.0)])
    resp = _post(admin_client, pid, min_qty='', min_qty_unit='')
    assert resp.status_code == 302
    assert _promos(tmp_db, pid) == [('percent', 5.0, None, None, None)]


def test_post_ignores_a_bundle_condition(admin_client, tmp_db):
    """The form no longer posts one; a stray one (an old open tab) is not stored."""
    pid = _product(tmp_db)
    resp = _post(admin_client, pid, bundle_condition='ยกลัง', min_qty='20', min_qty_unit='อัน')
    assert resp.status_code == 302
    assert _promos(tmp_db, pid) == [('percent', 5.0, 20.0, 'อัน', None)]


# ── product detail ───────────────────────────────────────────────────────────

def test_detail_shows_the_minimum_on_the_active_row_and_in_history(admin_client, tmp_db):
    pid = _product(tmp_db)
    assert _post(admin_client, pid, min_qty='20', min_qty_unit='อัน').status_code == 302
    resp = admin_client.get(f'/products/{pid}')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    active = _active_row(html)
    assert 'form673' in active and 'ลด 5.0% · ซื้อ ≥ 20 อัน' in active
    assert '(ซื้อ ≥ 20 อัน)' in _history_row(html, 'form673')


def test_detail_history_keeps_a_legacy_label_with_no_minimum(admin_client, tmp_db):
    """7 closed prod rows carry ยกลัง/ยกล่อง with no number: history must still
    say what they required. The active row never renders the bare label."""
    pid = _product(tmp_db)
    c = _conn(tmp_db)
    c.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
              "bundle_condition, date_start, date_end) "
              "VALUES (?, 'legacy-ยกลัง', 'percent', 5, 'ยกลัง', '2026-01-01', '2026-01-31')",
              (pid,))
    c.commit()
    c.close()
    assert _post(admin_client, pid, min_qty='20', min_qty_unit='อัน').status_code == 302
    html = admin_client.get(f'/products/{pid}').get_data(as_text=True)
    assert '(ต้องซื้อยกลัง)' in _history_row(html, 'legacy-ยกลัง')
    gated = _history_row(html, 'form673')
    assert '(ซื้อ ≥ 20 อัน)' in gated and 'ต้องซื้อ' not in gated
    active = _active_row(html)
    assert 'form673' in active and 'ต้องซื้อ' not in active


def test_detail_active_row_keeps_a_label_with_no_minimum(admin_client, tmp_db):
    """A current labelled row with no number (fail-closed: priced at list)
    still says what it requires."""
    pid = _product(tmp_db)
    c = _conn(tmp_db)
    c.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
              "bundle_condition, date_start) "
              "VALUES (?, 'bare-ยกลัง', 'percent', 5, 'ยกลัง', '2026-01-01')", (pid,))
    c.commit()
    c.close()
    active = _active_row(admin_client.get(f'/products/{pid}').get_data(as_text=True))
    assert 'bare-ยกลัง' in active and 'ลด 5.0% · ต้องซื้อยกลัง' in active


def test_detail_shows_the_minimum_instead_of_a_label(admin_client, tmp_db):
    """A row carrying both: the number is the rule, the label is not shown."""
    pid = _product(tmp_db)
    c = _conn(tmp_db)
    c.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
              "bundle_condition, min_qty, min_qty_unit, date_start) "
              "VALUES (?, 'both-ยกลัง', 'percent', 5, 'ยกลัง', 20, 'อัน', '2026-01-01')", (pid,))
    c.commit()
    c.close()
    html = admin_client.get(f'/products/{pid}').get_data(as_text=True)
    active = _active_row(html)
    assert 'both-ยกลัง' in active and 'ลด 5.0% · ซื้อ ≥ 20 อัน' in active
    assert 'ต้องซื้อ' not in active
    history = _history_row(html, 'both-ยกลัง')
    assert '(ซื้อ ≥ 20 อัน)' in history and 'ต้องซื้อ' not in history


# ── promo_detail macro (call card modal) ─────────────────────────────────────

def _promo_detail(promo):
    from app import app as flask_app
    with flask_app.test_request_context():
        mac = flask_app.jinja_env.get_template('macros.html').module
        return str(mac.promo_detail(promo))


def _promo(**kw):
    base = {'promo_name': 'card673', 'promo_type': 'percent', 'discount_value': 5.0,
            'min_qty': None, 'min_qty_unit': None, 'bundle_condition': None,
            'bundle_buy': None, 'bundle_free': None, 'bundle_unit': None,
            'bundle_tiers_json': None, 'gift_desc': None, 'gift_qty': None,
            'date_start': None, 'date_end': None}
    base.update(kw)
    return base


def test_promo_detail_macro_prints_the_minimum(tmp_db):
    html = _promo_detail(_promo(min_qty=2.5, min_qty_unit='โหล'))
    assert 'card673' in html and 'ส่วนลด 5%' in html
    assert 'ซื้อ ≥ 2.5 โหล' in html and 'ต้องซื้อขั้นต่ำ' not in html


def test_promo_detail_macro_without_a_minimum_prints_the_label(tmp_db):
    html = _promo_detail(_promo(promo_type='bundle', discount_value=None, bundle_buy=10,
                                bundle_free=1, bundle_condition='ยกลัง'))
    assert 'card673' in html and 'ต้องซื้อยกลัง' in html
    assert 'ขั้นต่ำ' not in html


def test_promo_detail_macro_prefers_the_minimum_over_a_label(tmp_db):
    html = _promo_detail(_promo(min_qty=20.0, min_qty_unit='อัน', bundle_condition='ยกลัง'))
    assert 'ซื้อ ≥ 20 อัน' in html
    assert 'ต้องซื้อยกลัง' not in html


# ── one condition text on every site (S2: macros.promo_condition) ───────────

_CUST = 'TEST673S'
_CUST_NAME = 'ลูกค้าทดสอบ 673 เงื่อนไข'


def _render_macro(name, promo):
    from app import app as flask_app
    with flask_app.test_request_context():
        tpl = flask_app.jinja_env.from_string(
            "{% from 'macros.html' import " + name + " %}{{ " + name + "(p) }}")
        return tpl.render(p=promo)


def _site_promos(tmp_db):
    """One gated price promo (met by the customer's last bill) and one
    label-only bundle, each on its own fresh product."""
    gated_pid = _product(tmp_db, unit_type='ตัว')
    bundle_pid = _product(tmp_db, unit_type='ตัว')
    c = _conn(tmp_db)
    c.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
              "min_qty, min_qty_unit, date_start) "
              "VALUES (?, 'site-gated', 'percent', 10, 20, 'ตัว', '2024-01-01')", (gated_pid,))
    c.execute("INSERT INTO promotions (product_id, promo_name, promo_type, bundle_buy, bundle_free, "
              "bundle_unit, bundle_condition, date_start) "
              "VALUES (?, 'site-bundle', 'bundle', 10, 1, 'ตัว', 'ยกลัง', '2024-01-01')",
              (bundle_pid,))
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?) "
              "ON CONFLICT(code) DO UPDATE SET name = excluded.name", (_CUST, _CUST_NAME))
    c.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
              "customer_code, qty, unit, unit_price, vat_type, total, net) "
              "VALUES ('2026-01-01', 'IV67399-1', 'IV67399', ?, ?, ?, 20, 'ตัว', 90, 0, 1800, 1800)",
              (gated_pid, _CUST_NAME, _CUST))
    c.commit()
    promos = {r['promo_name']: dict(r) for r in c.execute(
        "SELECT * FROM promotions WHERE product_id IN (?, ?)", (gated_pid, bundle_pid))}
    c.close()
    return gated_pid, bundle_pid, promos['site-gated'], promos['site-bundle']


def test_promo_condition_is_the_one_rule():
    gated = _promo(min_qty=20.0, min_qty_unit='ตัว')
    assert _render_macro('promo_condition', gated) == 'ซื้อ ≥ 20 ตัว'
    assert _render_macro('promo_condition', dict(gated, bundle_condition='ยกลัง')) == 'ซื้อ ≥ 20 ตัว'
    assert _render_macro('promo_condition', _promo(bundle_condition='ยกลัง')) == 'ต้องซื้อยกลัง'
    assert _render_macro('promo_condition', _promo()) == ''


def test_every_site_renders_the_same_condition_text(admin_client, tmp_db):
    """promo_summary, promo_detail, product detail (active row + history) and
    the customer page's % formula cell all print promo_condition's text and
    only their own wrapper. The customer page shows price promos only, so the
    label-only bundle has no site there; its cross-sell formula cell (line
    ~851) cannot render a minimum (cross-sell asks with no quantity, so a
    gated promo never applies) and is held by the template sweep below."""
    gated_pid, bundle_pid, gated, bundle = _site_promos(tmp_db)
    cases = [(gated_pid, gated, 'ซื้อ ≥ 20 ตัว'), (bundle_pid, bundle, 'ต้องซื้อยกลัง')]
    for pid, promo, text in cases:
        assert _render_macro('promo_condition', promo) == text
        assert _render_macro('promo_summary', promo).strip().endswith(' · ' + text)
        assert f'<div class="text-muted small">{text}</div>' in _render_macro('promo_detail', promo)
        page = admin_client.get(f'/products/{pid}').get_data(as_text=True)
        assert f' · {text}</small>' in _active_row(page)
        assert f'({text})' in _history_row(page, promo['promo_name'])
        assert 'ต้องซื้อขั้นต่ำ' not in page

    from urllib.parse import quote
    html = admin_client.get(f'/customer/code/{quote(_CUST)}').get_data(as_text=True)
    rows = [r for r in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)
            if re.search(rf'/products/{gated_pid}[?"]', r)]
    cells = [c for r in rows
             for c in re.findall(r'<td data-label="ราคาวันนี้"[^>]*>(.*?)</td>', r, re.S)]
    formula = [c for c in cells if '−10%' in c]
    assert len(formula) == 1, 'CONTROL: the % formula rendered for the gated product'
    assert '<div class="text-subtle small">ซื้อ ≥ 20 ตัว</div>' in formula[0]


def test_no_template_spells_the_condition_by_hand():
    """The condition is written once, in macros.promo_condition. Exempt:
    promotions/form.html, whose 'ซื้อ ≥' is the input's own label and its live
    preview of a promo not yet saved, not a render of a stored one."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / 'inventory_app' / 'templates'
    files = sorted(root.rglob('*.html'))
    assert len(files) > 50, 'control: the sweep sees the template tree'
    hits = []
    macro_body = None
    for f in files:
        rel = f.relative_to(root).as_posix()
        if rel == 'promotions/form.html':
            continue
        text = re.sub(r'\{#.*?#\}', '', f.read_text(encoding='utf-8'), flags=re.S)
        if rel == 'macros.html':
            m = re.search(r'\{% macro promo_condition\(promo\) -?%\}(.*?)\{%-? endmacro', text, re.S)
            assert m, 'macros.html defines promo_condition'
            macro_body = m.group(1)
            text = text.replace(m.group(0), '')
            assert 'promo_condition(' in text, 'control: the comment strip kept the code'
        for needle in ('ซื้อ ≥', 'ต้องซื้อ'):
            if needle in text:
                hits.append((rel, needle))
    assert hits == []
    assert 'ซื้อ ≥' in macro_body and 'ต้องซื้อ' in macro_body
    calls = {rel: (root / rel).read_text(encoding='utf-8').count('promo_condition(')
             for rel in ('customer_summary.html', 'products/detail.html')}
    assert calls == {'customer_summary.html': 2, 'products/detail.html': 2}
