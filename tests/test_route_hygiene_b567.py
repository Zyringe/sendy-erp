"""TDD tests for the B5/B6/B7 route-hygiene fixes (LOW-severity bundle).

B5 — /customers/geocode/<code> (POST) fires a live outbound Nominatim/OSM
call. Before the fix the endpoint was in NO POST whitelist at all, so the
existing `_STAFF_POST_OK`/`_MANAGER_POST_OK` gate in `before_request` already
blocked staff *and* manager (redirect, not a clean 403) — only 'admin' could
ever reach the view. The fix (a) adds the endpoint to `_MANAGER_POST_OK` so
manager can actually use the customer-map geocode feature, and (b) adds an
inline `abort(403)` gate at the top of the view (defense-in-depth, matching
the `mapping_suggestion_approve` idiom) in case the whitelist ever drifts.

B6 — `customer_import_bsn` opens a hardcoded CSV path via `_parse_bsn_customers()`
with no existence check; a missing file raises an unhandled `FileNotFoundError`
-> 500. Fixed with try/except + flash + redirect back to the customer map.

B7 — `commission_overrides_toggle`/`_delete` called `commission_mod.clear_override_cache()`
raw instead of the `_safe_clear_override_cache()` wrapper already used by
`_new`/`_edit`, so an exception there would 500 a request whose DB write had
already succeeded. Fixed by routing all four through the safe wrapper.

Run: cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_route_hygiene_b567.py -q
"""
import json
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


def _login(client, role, user_id=1):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['role'] = role


@pytest.fixture
def client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def _insert_customer(tmp_db, code='TEST_GEO_001', address='123 ถ.ทดสอบ กรุงเทพมหานคร'):
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO customers (code, name, address) VALUES (?, ?, ?)",
        (code, f'ร้าน {code}', address),
    )
    conn.commit()
    conn.close()


# ── B5: /customers/geocode/<code> role gate ─────────────────────────────────

def test_b5_staff_blocked_no_network_call(client, tmp_db, monkeypatch):
    """Staff POST must never reach the outbound geocode call."""
    _insert_customer(tmp_db)

    def _boom(*a, **k):
        raise AssertionError('outbound geocode HTTP call fired for a staff request')
    monkeypatch.setattr('urllib.request.urlopen', _boom)

    _login(client, 'staff')
    resp = client.post('/customers/geocode/TEST_GEO_001', follow_redirects=False)
    # Blocked by the POST whitelist in before_request (redirect) or, if the
    # whitelist ever changes, by the route's own inline gate (403). Either
    # way it must not reach urlopen (the monkeypatch above would raise).
    assert resp.status_code in (302, 403), resp.status_code


def test_b5_manager_allowed_and_geocodes(client, tmp_db, monkeypatch):
    """Manager POST reaches the real view and completes a geocode end-to-end."""
    _insert_customer(tmp_db)

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                [{'lat': '13.75', 'lon': '100.50', 'display_name': 'x'}]
            ).encode()

    calls = []

    def _fake_urlopen(req, timeout=8):
        calls.append(req)
        return _FakeResp()

    monkeypatch.setattr('urllib.request.urlopen', _fake_urlopen)

    _login(client, 'manager')
    resp = client.post('/customers/geocode/TEST_GEO_001', follow_redirects=False)
    assert resp.status_code == 200, resp.data[:300]
    body = resp.get_json()
    assert body['ok'] is True
    assert len(calls) == 1, 'manager POST should reach the real geocode call exactly once'

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT lat, lng FROM customers WHERE code = ?", ('TEST_GEO_001',)
    ).fetchone()
    conn.close()
    assert row == (13.75, 100.5)


def test_b5_endpoint_in_manager_whitelist():
    """Regression pin: without this, adding the inline gate alone is a no-op
    because before_request would still redirect manager away before the view
    (and its inline check) ever runs."""
    from app import _MANAGER_POST_OK
    assert 'customer_geocode' in _MANAGER_POST_OK


# ── B6: customer_import_bsn missing-CSV guard ───────────────────────────────

def test_b6_missing_csv_flashes_and_redirects_not_500(client, monkeypatch):
    import app as app_module

    def _raise_fnf():
        raise FileNotFoundError('no such file: bsn_customer_info.csv')
    monkeypatch.setattr(app_module, '_parse_bsn_customers', _raise_fnf)

    _login(client, 'admin')
    resp = client.post('/customers/import-bsn', follow_redirects=False)
    assert resp.status_code == 302, f'expected redirect, got {resp.status_code}: {resp.data[:300]!r}'
    assert resp.headers.get('Location', '').endswith('/customers/map')

    page = client.get(resp.headers['Location'], follow_redirects=False)
    assert page.status_code == 200
    assert 'ไม่พบไฟล์' in page.get_data(as_text=True)


def test_b6_happy_path_unaffected(client, monkeypatch):
    """The guard must not touch the success path: a normal parse result still
    imports and redirects with the usual success flash."""
    import app as app_module

    fake_customers = [{
        'code': 'HAPPY_001', 'name': 'ร้านทดสอบ', 'salesperson': 'SP01',
        'zone': 'Z1', 'customer_type': 'R', 'address': '', 'phone': '',
        'tax_id': '', 'credit_days': 0, 'contact': '',
    }]
    monkeypatch.setattr(app_module, '_parse_bsn_customers', lambda: fake_customers)

    _login(client, 'admin')
    resp = client.post('/customers/import-bsn', follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get('Location', '').endswith('/customers/map')
    page = client.get(resp.headers['Location'], follow_redirects=False)
    assert 'นำเข้าสำเร็จ' in page.get_data(as_text=True)


# ── B7: commission override cache-clear consistency ─────────────────────────

def _insert_override(tmp_db):
    """commission_overrides has CHECKs requiring product_id OR brand_id, and
    fixed_per_unit OR custom_rate_pct."""
    conn = sqlite3.connect(tmp_db)
    brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO commission_overrides "
        "(brand_id, salesperson_code, custom_rate_pct, is_active) "
        "VALUES (?, 'SP01', 5.0, 1)",
        (brand_id,),
    )
    conn.commit()
    override_id = cur.lastrowid
    conn.close()
    return override_id


def test_b7_toggle_survives_cache_clear_exception(client, tmp_db, monkeypatch):
    import commission as commission_mod

    def _boom():
        raise RuntimeError('cache backend unavailable')
    monkeypatch.setattr(commission_mod, 'clear_override_cache', _boom)

    override_id = _insert_override(tmp_db)
    _login(client, 'admin')
    resp = client.post(f'/commission/overrides/{override_id}/toggle', follow_redirects=False)
    assert resp.status_code == 302, f'toggle 500\'d instead of flashing: {resp.status_code}'

    page = client.get(resp.headers['Location'], follow_redirects=False)
    assert 'refresh cache ล้มเหลว' in page.get_data(as_text=True)

    conn = sqlite3.connect(tmp_db)
    is_active = conn.execute(
        "SELECT is_active FROM commission_overrides WHERE id = ?", (override_id,)
    ).fetchone()[0]
    conn.close()
    assert is_active == 0, 'the DB write must still have gone through despite the cache-clear error'


def test_b7_delete_survives_cache_clear_exception(client, tmp_db, monkeypatch):
    import commission as commission_mod

    def _boom():
        raise RuntimeError('cache backend unavailable')
    monkeypatch.setattr(commission_mod, 'clear_override_cache', _boom)

    override_id = _insert_override(tmp_db)
    _login(client, 'admin')
    resp = client.post(f'/commission/overrides/{override_id}/delete', follow_redirects=False)
    assert resp.status_code == 302, f'delete 500\'d instead of flashing: {resp.status_code}'

    page = client.get(resp.headers['Location'], follow_redirects=False)
    assert 'refresh cache ล้มเหลว' in page.get_data(as_text=True)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT 1 FROM commission_overrides WHERE id = ?", (override_id,)
    ).fetchone()
    conn.close()
    assert row is None, 'the DB delete must still have gone through despite the cache-clear error'
