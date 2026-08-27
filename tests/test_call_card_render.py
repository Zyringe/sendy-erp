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
