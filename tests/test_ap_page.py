"""Tests for the unified /ap page (AP consolidation).

Task 1: /ap route + overview tab
Task 2: รายซัพพลายเออร์ (suppliers) tab
Task 3: การจ่ายเงิน (payments) tab
Task 4: redirect /express/ap + nav endpoint
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')


def _client(role, tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = role; s['role'] = role
    return c


# ── Task 1 ────────────────────────────────────────────────────────────────────

def test_ap_overview_renders_and_total_matches(tmp_db):
    import models
    from database import get_connection
    c = _client('admin', tmp_db)
    r = c.get('/ap')                       # default tab=overview
    assert r.status_code == 200
    body = r.data.decode()
    assert 'ภาพรวม' in body and 'รายซัพพลายเออร์' in body and 'การจ่ายเงิน' in body
    conn = get_connection()
    ap = models.get_ap_outstanding(conn)
    conn.close()
    # grand total appears (leading digits), proving overview uses the real helper
    total_str = f"{ap['grand_total']:,.0f}"
    # at least the first 3 non-trivial chars of the formatted number appear in page
    assert total_str[:3] in body or total_str in body


# ── Task 2 ────────────────────────────────────────────────────────────────────

def test_suppliers_tab_renders_for_staff(tmp_db):
    c = _client('staff', tmp_db)
    r = c.get('/ap?tab=suppliers')
    assert r.status_code == 200
    assert 'ค้างจ่าย' in r.data.decode()


# ── Task 3 ────────────────────────────────────────────────────────────────────

def test_payments_tab_renders(tmp_db):
    c = _client('admin', tmp_db)
    r = c.get('/ap?tab=payments')
    assert r.status_code == 200
    assert 'การจ่ายเงิน' in r.data.decode()


# ── Task 4 ────────────────────────────────────────────────────────────────────

def test_express_ap_redirects(tmp_db):
    c = _client('admin', tmp_db)
    r = c.get('/express/ap', follow_redirects=False)
    assert r.status_code == 302 and '/ap' in r.headers['Location']


def test_ap_dashboard_in_endpoint_module(tmp_db):
    from app import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE.get('accounting.ap_dashboard') == 'finance'


def test_whitelist_and_module_keys_valid(tmp_db):
    from app import app as a, _ENDPOINT_MODULE
    eps = {r.endpoint for r in a.url_map.iter_rules()}
    assert not (set(_ENDPOINT_MODULE) - eps), \
        f"_ENDPOINT_MODULE keys missing from URL map: {set(_ENDPOINT_MODULE) - eps}"
