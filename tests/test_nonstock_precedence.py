"""Task 7: Write-path precedence — the constant cannot be overridden.

A NON_STOCK_BSN_CODES code can never legitimately be marked "ไม่นำเข้า"
(is_ignored=1) — doing so silently drops real revenue (see stock_filters.py,
imports.py, mapping.py). This file pins the three layers that enforce that:
upsert_mapping refuses the write, the /mapping/save route turns the refusal
into a clean 400, and an import that meets a stale is_ignored=1 anyway (a
restored pre-mig-155 backup, which never passes through upsert_mapping) keeps
the revenue and raises an alert rather than staying silent.

Design: projects/nonstock-billable-line/design.md v4 (task-7-brief.md).
"""
import pytest
import models
from models.stock_filters import NonStockCodeError


def test_upsert_mapping_refuses_to_ignore_a_protected_code(tmp_db_conn):
    with pytest.raises(NonStockCodeError) as exc:
        models.upsert_mapping('ZZZ', 'ส่วนลดพิเศษ', is_ignored=1)
    assert 'ZZZ' in str(exc.value)


def test_upsert_mapping_still_allows_ignoring_other_codes(tmp_db_conn):
    """CONTROL — the refusal must be narrow."""
    models.upsert_mapping('888ค8887', 'ค่าVAT', is_ignored=1)
    row = tmp_db_conn.execute(
        "SELECT is_ignored FROM product_code_mapping WHERE bsn_code='888ค8887'"
    ).fetchone()
    assert row is not None
    assert row['is_ignored'] == 1


@pytest.fixture
def admin_client(tmp_db):
    """Authed admin test client. Session injection, NOT a real login: this
    machine's Python has no hashlib.scrypt, so a real HTTP login against a
    scrypt-hashed user throws. Copied from the pattern already established in
    tests/test_unified_import.py:42-50 — do not invent a new one."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def test_mapping_route_returns_400_not_500(admin_client):
    """A crafted/stale POST must be a clean validation error, not an HTTP 500.
    Asserted at the ROUTE — a model-level test passes while the route 500s.

    ⚠ Verified against blueprints/bsn.py::mapping_save before relying on it:
    the route reads `data.get('mappings', [])`, not 'items' — a payload keyed
    'items' would silently no-op (empty loop, 200 OK) and never reach the
    `except models.NonStockCodeError` branch this test exists to pin. Using
    the real key here."""
    resp = admin_client.post('/mapping/save', json={'mappings': [
        {'bsn_code': 'ZZZ', 'bsn_name': 'ส่วนลดพิเศษ', 'action': 'ignore'}]})
    assert resp.status_code == 400, resp.status_code
    assert 'ZZZ' in resp.get_data(as_text=True)


def test_restored_backup_contradiction_keeps_revenue_and_alerts(tmp_db_conn):
    """A pre-mig-155 backup has is_ignored=1 on a protected code and never
    passes through upsert_mapping. The constant wins, and it is not silent."""
    conn = tmp_db_conn
    # tmp_db_conn clones the LIVE dev DB wholesale (same hazard documented in
    # test_nonstock_line_sync.py's _map()/test_non_stock_line_is_imported_
    # as_revenue): it already carries a real product_code_mapping row
    # (bsn_unit='') and years of sales_transactions history for 888ค8888, and
    # system_alerts may carry unrelated open alerts from the snapshot. Force
    # every piece of state this test reads or writes rather than inheriting
    # it — a raw INSERT below would otherwise hit the (bsn_code, bsn_unit)
    # UNIQUE constraint, and the count/alert assertions would read someone
    # else's data.
    conn.execute("DELETE FROM sales_transactions WHERE bsn_code='888ค8888'")
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code='888ค8888'")
    conn.execute("DELETE FROM system_alerts")
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง','ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id,"
        " is_ignored, bsn_unit) VALUES ('888ค8888','ค่าขนส่ง',?,1,'')", (pid,))
    conn.commit()

    stats = models.import_weekly([{
        'date_iso': '2026-06-15', 'doc_no': 'IV9400-1',
        'product_code_raw': '888ค8888', 'product_name_raw': 'ค่าขนส่ง',
        'party': 'วรสวัสดิ์', 'party_code': '01อ35', 'qty': 1.0, 'unit': 'ใบ',
        'unit_price': 30.0, 'vat_type': 2, 'discount': '', 'total': 30.0,
        'net': 30.0, 'line_seq': 1}], 'sales', 'test.csv')

    assert stats['non_stock'] == 1, stats
    n = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE bsn_code='888ค8888'"
    ).fetchone()[0]
    assert n == 1, "revenue must be kept despite the stale is_ignored flag"

    alerts = conn.execute(
        "SELECT kind, message FROM system_alerts WHERE resolved_at IS NULL"
    ).fetchall()
    assert len(alerts) == 1, alerts
    assert '888ค8888' in alerts[0]['message']


def test_dismiss_flash_is_honest_for_a_protected_group(admin_client, tmp_db_conn):
    """Step 4b (added after Task 5's review): a refused dismiss must render
    as a warning, not a green 'ยกเลิก 0 แถว' success. Route-level because a
    model-level test can't see a flash — Task 5's own tests already pin
    `dismiss_pending_unit_conversion(...) == 0` at that layer; this pins what
    the OPERATOR sees. admin_client (not tmp_db_conn's connection) does the
    POST, but both fixtures resolve the same underlying `tmp_db` file."""
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง','ตัว')"
    ).lastrowid
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
        " product_id, bsn_code, product_name_raw, customer, qty, unit, net,"
        " synced_to_stock) VALUES (1,'2026-06-15','IV9401-1','IV9401',?,"
        "'888ค8888','ค่าขนส่ง','วรสวัสดิ์',1,'ใบ',30.0,0)", (pid,))
    conn.commit()

    resp = admin_client.post('/unit-conversions/dismiss',
                             data={'product_id': pid, 'bsn_unit': 'ใบ'},
                             follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'ไม่ได้ยกเลิกรายการใด' in body, body
    assert 'ยกเลิก 0 แถว' not in body, body


def test_saveSingle_surfaces_the_route_error_to_the_operator():
    """I3 (final review): the 400 that names the code must reach the operator.

    `mapping_save` returns {'ok': False, 'error': '<message naming the code>'}
    and the test above pins that. `saveSingle` in templates/mapping.html is
    its ONLY caller, and it used to do `alert('Save failed')` — discarding
    `d.error`, so design §9b's "a clear refusal naming the code" was satisfied
    at the route and thrown away at the client. A route-level test cannot see
    that layer; this one reads the template.

    ⚠ Scoped to the saveSingle FUNCTION BODY on purpose. A file-wide
    `'d.error' in body` could not fail: the approve-suggestion handler ~700
    lines above already contains `alert('Error: ' + (d.error || 'unknown'))`,
    so the whole-file check is satisfied by a sibling and says nothing about
    the handler under test. Same substring trap the rules file warns about.
    """
    import os
    tpl = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'inventory_app', 'templates', 'mapping.html')
    with open(tpl, encoding='utf-8') as f:
        body = f.read()

    marker = 'function saveSingle('
    start = body.find(marker)
    assert start != -1, 'saveSingle no longer exists in mapping.html'
    assert body.count(marker) == 1, 'more than one saveSingle — rescope this test'
    # closing brace at column 0 — saveSingle is the last `function` in the
    # file, so scanning for the next one finds nothing.
    end = body.find('\n}', start)
    assert end != -1, 'could not find the end of saveSingle'
    fn = body[start:end + 2]

    # controls: the slice really is the handler, and it did not swallow the
    # rest of the file (which would re-open the sibling-handler substring trap)
    assert 'mapping_save' in fn, 'sliced the wrong function'
    assert 'd.ok' in fn, 'sliced the wrong function'
    assert len(fn) < 1500, 'slice too large — it may include a sibling handler'
    assert 'unknown' not in fn, (
        'slice reached the approve-suggestion handler — rescope')

    assert 'd.error' in fn, (
        "saveSingle discards the server's error message — the 400 naming the "
        "refused code never reaches the operator")
    assert "alert('Save failed')" not in fn, (
        'saveSingle still shows the generic message instead of d.error')
