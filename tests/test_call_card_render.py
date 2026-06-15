"""Render tests for the call-card pricing/promo upgrade.

Two layers the unit tests can't reach:
  1. The promo macros (macros.html) rendered across all promo_types + multi-tier.
  2. The real /call list + a sample of real customer cards rendering without a 500
     — catches Jinja/macro/template errors that get_card unit tests don't surface.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import call_card as cc


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
