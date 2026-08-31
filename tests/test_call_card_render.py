"""Render tests for the call-card pricing/promo upgrade.

Three layers the unit tests can't reach:
  1. The promo macros (macros.html) rendered across all promo_types + multi-tier.
  2. The real /call list + a sample of real customer cards rendering without a 500
     — catches Jinja/macro/template errors that get_card unit tests don't surface.
  3. (2e, PR C) The rendered ราคาล่าสุดที่ลูกค้าได้ cell for a seeded pair equals
     resolve_price's own customer.last.cash_per_unit — the actual number Put
     reads on the page, not just what _assemble_products returns in Python.
"""
import datetime as dt
import os
import re
os.environ.setdefault('SKIP_DB_INIT', '1')

import call_card as cc
import price_lookup as pl


def _app():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app


def _client(flask_app):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def _render(flask_app, src, **ctx):
    with flask_app.app_context():
        return flask_app.jinja_env.from_string(src).render(**ctx)


# ── promo macros across all types ────────────────────────────────────────────

def test_promo_summary_percent_and_fixed():
    app = _app()
    base = "{% import 'macros.html' as m %}{{ m.promo_summary(p) }}"
    assert 'ลด 20%' in _render(app, base, p={'promo_type': 'percent', 'discount_value': 20.0})
    assert '฿80' in _render(app, base, p={'promo_type': 'fixed', 'discount_value': 80.0})


def test_promo_summary_bundle_and_multitier_pill():
    app = _app()
    base = "{% import 'macros.html' as m %}{{ m.promo_summary(p) }}"
    single = _render(app, base, p={'promo_type': 'bundle', 'bundle_buy': 10, 'bundle_free': 1,
                                   'bundle_unit': None, 'bundle_tiers_json': None})
    assert 'ซื้อ 10 แถม 1' in single
    assert 'ระดับ' not in single  # single tier → no multi-tier pill
    multi = _render(app, base, p={'promo_type': 'bundle', 'bundle_buy': 10, 'bundle_free': 1,
                                  'bundle_unit': None,
                                  'bundle_tiers_json': '[{"buy":10,"free":1},{"buy":20,"free":3}]'})
    assert '2 ระดับ' in multi  # multi-tier → pill appears


def test_promo_detail_lists_tiers_and_condition():
    app = _app()
    src = "{% import 'macros.html' as m %}{{ m.promo_detail(p) }}"
    out = _render(app, src, p={
        'promo_name': 'โปรลัง', 'promo_type': 'bundle',
        'bundle_buy': 10, 'bundle_free': 1, 'bundle_unit': 'ตัว',
        'bundle_condition': 'ยกลัง',
        'bundle_tiers_json': '[{"buy":10,"free":1},{"buy":20,"free":3}]',
        'date_start': '2026-06-01', 'date_end': None,
    })
    assert 'โปรลัง' in out
    assert 'ต้องซื้อยกลัง' in out
    assert 'ซื้อ 20 แถม 3' in out          # second tier listed in full detail
    assert _render(app, src, p=None).strip()  # None promo → "ไม่มีโปรโมชัน" (non-empty)


def test_disc_label_formats():
    app = _app()
    src = "{% import 'macros.html' as m %}[{{ m.disc_label(d) | trim }}]"
    assert '20%' in _render(app, src, d='20%')
    assert '15+5%' in _render(app, src, d='15+5%')
    assert '฿28.00' in _render(app, src, d='28.00')
    assert _render(app, src, d='').strip() == '[]'     # empty → nothing
    assert _render(app, src, d=None).strip() == '[]'   # None → nothing


# ── full page render (integration, against a clone of the live DB) ───────────

def test_call_list_and_sample_cards_render(tmp_db_conn):
    """/call list + a sample of real customer cards render (no 500) with the new
    price-cell + modal markup. Uses a tmp clone of the live DB (skips if absent)."""
    app = _app()
    client = _client(app)
    list_resp = client.get('/call')
    assert list_resp.status_code == 200
    # the restored ⭐ special-price badge renders on the list (live data flags ~18%)
    assert 'ราคาพิเศษ' in list_resp.get_data(as_text=True)

    rows = cc.get_call_list(tmp_db_conn)
    assert rows, "live clone has no customers"

    saw_cell = False
    saw_orders = False
    saw_position = False
    for row in rows[:15]:
        code = row['customer_code']
        if not code:
            continue
        resp = client.get('/call/' + code)
        assert resp.status_code in (200, 404), f"/call/{code} -> {resp.status_code}"
        if resp.status_code == 200:
            html = resp.get_data(as_text=True)
            assert 'detailModal' in html, f"modal markup missing on /call/{code}"
            if 'cc-px-list' in html:
                saw_cell = True
            if 'tpl-orders-1' in html and 'tpl-peer-1' in html:
                saw_orders = True
            if 'ของเพื่อน' in html or 'ในกลุ่ม' in html:
                saw_position = True
    assert saw_cell, "no sampled card rendered the new price-cell markup"
    assert saw_orders, "no sampled card rendered the peer + orders modal templates"
    assert saw_position, "no sampled card rendered the peer-position (ส่วนต่าง) text"


# ── 2e (PR C): the rendered price cell matches resolve_price ─────────────────

def test_customer_latest_cell_matches_resolver(tmp_db_conn):
    """C3 parity: the ราคาล่าสุดที่ลูกค้าได้ cell's number must equal
    resolve_price(...)['customer']['last']['cash_per_unit'] for a seeded
    pair — parse the number out of the rendered HTML, never compare
    formatted strings."""
    conn = tmp_db_conn
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES ('พารีตี้ทดสอบ 2e', 'ตัว', 100.0, 60.0, 1)"
    )
    pid = cur.lastrowid
    code = 'TST-2E-PARITY'
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
        (code, 'ลูกค้าทดสอบ parity'),
    )
    today = dt.date.today().isoformat()
    bill_date = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, "
        "customer, customer_code, qty, unit, unit_price, vat_type, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (bill_date, 'IVPARITY-1', 'IVPARITY', pid, 'ลูกค้าทดสอบ parity', code,
         1, 'ตัว', 100.0, 0, 100.0),
    )
    conn.commit()

    expected = pl.resolve_price(conn, product_id=pid, customer_code=code, today=today)
    assert expected['customer']['last']['in_window'] is True  # sanity: not the stale-fallback case
    expected_val = expected['customer']['last']['cash_per_unit']

    app = _app()
    client = _client(app)
    resp = client.get(f'/call/{code}')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # The bare product name also appears earlier on the page (the "แบรนด์เด่น"
    # summary stat) -- anchor on the ซื้อประจำ table row specifically
    # (cc-namelink), not the first occurrence of the name anywhere.
    name_match = re.search(r'cc-namelink">พารีตี้ทดสอบ 2e<', html)
    assert name_match, "product row not found in the ซื้อประจำ table"
    row_html = html[name_match.start():name_match.start() + 4000]
    m = re.search(r'cc-px-net">สุทธิ ฿([\d,]+\.\d{2})', row_html)
    assert m, "customer_latest cell not found in rendered row"
    rendered_val = float(m.group(1).replace(',', ''))
    assert rendered_val == expected_val


def test_stale_pre_epoch_price_renders_with_its_warning(tmp_db_conn):
    """Put's decision B: a pre-epoch bill is SHOWN on the real page, carrying a
    'ก่อนเปลี่ยนราคา / ไม่ใช่ราคาปัจจุบัน' marker so it cannot be read as the
    current price. Showing the number WITHOUT the marker would be worse than
    blanking it, so this asserts the rendered element, not just the value.

    Asserts on the ELEMENT (`cc-px-stale`) and parses the number out — never a
    bare Thai substring, and never a formatted-string comparison.
    """
    conn = tmp_db_conn
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES ('สเตลทดสอบ 2e', 'ตัว', 100.0, 60.0, 1)").lastrowid
    code = 'TST-2E-STALE'
    conn.execute("INSERT INTO customers (code, name) VALUES (?, ?) "
                 "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                 (code, 'ลูกค้าทดสอบ stale'))
    today = dt.date.today()
    epoch = (today - dt.timedelta(days=30)).isoformat()
    bill = (today - dt.timedelta(days=200)).isoformat()      # well before the epoch
    conn.execute(
        "INSERT INTO product_price_history (product_id, field_name, old_value, new_value, changed_at) "
        "VALUES (?, 'base_sell_price', 80, 100, ?)", (pid, epoch + ' 09:00:00'))
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit, unit_price, vat_type, net) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (bill, 'IVSTALE-1', 'IVSTALE', pid, 'ลูกค้าทดสอบ stale', code, 1, 'ตัว', 100.0, 0, 73.0))
    conn.commit()

    # sanity: the resolver agrees this pair is the stale case, not the in-window one
    r = pl.resolve_price(conn, product_id=pid, customer_code=code, today=today.isoformat())
    assert r['customer']['last']['in_window'] is False

    html = _client(_app()).get(f'/call/{code}').get_data(as_text=True)
    # split()[0] is the page prefix, never a row, and the product name also
    # appears there — require a real cell so the count assertion means something.
    rows = [seg for seg in html.split('<tr')[1:]
            if 'สเตลทดสอบ 2e' in seg and '</td>' in seg]
    assert len(rows) == 1, f"expected exactly 1 row for the seeded product, got {len(rows)}"
    row = rows[0]

    assert 'cc-px-stale' in row, row          # the marker element is present
    assert 'ไม่ใช่ราคาปัจจุบัน' in row
    assert bill in row                        # and it names WHEN
    nums = [float(m.replace(',', '')) for m in re.findall(r'฿([\d,]+\.\d{2})', row)]
    assert 73.0 in nums, nums                 # the number itself is still shown


# ── เงียบ filter + phone column (feat/call-quiet-filter) ─────────────────────
# Rendered at the TEMPLATE layer, with no DB: a route test would call
# get_connection() and create inventory_app/instance/inventory.db inside the
# worktree, which poisons every later run (conftest resolves LIVE_DB to it).
# Route wiring is covered by the booted-server check in the PR description.

def _list_ctx(**over):
    ctx = {
        'rows': [{
            'customer_code': 'C001', 'name': 'ร้านทดสอบ', 'province': 'เชียงใหม่',
            'region': 'เหนือ', 'last_buy': '2026-02-09', 'spend': 105524.0,
            'call_status': 'never', 'call_days': None, 'last_called': None,
            'phone': '053-115732,089-4317234',
            'badges': {'ar': 0, 'quiet': True, 'special': False},
        }],
        'regions': ['เหนือ'], 'salespersons': [{'code': '00', 'name': 'บริษัท /00'}],
        'args': {}, 'spend_window': '1y',
        'elapsed_th': cc.elapsed_th, 'status_label': cc.STATUS_LABEL,
    }
    ctx.update(over)
    return ctx


def _render_list(app, **over):
    # test_request_context, not app_context: the template calls url_for() and
    # csrf_token(), both of which need a request.
    app.config['WTF_CSRF_ENABLED'] = False
    with app.test_request_context('/call'):
        return app.jinja_env.get_template('call/list.html').render(**_list_ctx(**over))


def test_list_page_offers_a_quiet_filter_control():
    html = _render_list(_app())
    # assert on the CONTROL, not the word — 'เงียบ' already appears as a badge label
    assert 'name="quiet"' in html


def test_list_page_shows_the_phone_number():
    html = _render_list(_app())
    assert '053-115732,089-4317234' in html


def test_quiet_selection_survives_touching_another_filter():
    """The filter form submits on change and drops any param it does not carry —
    that is why spend_window has a hidden input. quiet needs the same."""
    html = _render_list(_app(), args={'quiet': '1'})
    form = html.split('id="cc-filter-form"', 1)[1].split('</form>', 1)[0]
    assert 'quiet' in form, "quiet is not carried inside the filter form"
    assert ('value="1"' in form and 'name="quiet"' in form), \
        "quiet is in the form but its current value is not preserved"


def test_window_selector_shows_the_window_actually_used_not_the_one_asked_for():
    """quiet widens a 6m window to 1y (effective_spend_window). If the selector
    still reads '6 เดือน' the page shows 1-year money under a 6-month label."""
    html = _render_list(_app(), args={'quiet': '1', 'spend_window': '6m'}, spend_window='1y')
    opts = dict(re.findall(r'<option value="(6m|1y|2y|all)"([^>]*)>', html))
    assert 'selected' in opts['1y'], "the window actually used (1y) is not the selected option"
    assert 'selected' not in opts['6m'], "the page still claims 6 เดือน while showing 1y numbers"


def test_phone_cell_cannot_widen_the_table_on_a_narrow_screen():
    """.cc-table-wrap is overflow:hidden with table{width:100%}, so a long
    unbroken phone string (seen on prod: '02-4178295,01-643-4024 02-4178287,
    089-2032484') would squeeze every other column instead of scrolling."""
    html = _render_list(_app())
    assert 'cc-phone' in html
    style = html.split('<style', 1)[1].split('</style>', 1)[0]
    assert '.cc-phone' in style, "the phone cell has no width constraint"
    rule = style.split('.cc-phone', 1)[1].split('}', 1)[0]
    # word-break, NOT max-width: a max-width on a <td> is advisory under
    # table-layout:auto (measured 2026-08-31 — a 150px cap rendered 263px).
    assert 'word-break' in rule
