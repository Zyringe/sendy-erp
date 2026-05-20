"""Happy-path integration tests for the /cashflow customer-credit-balance
section.

Uses tmp_db so route + cashflow.py + payments_alloc.py + template all run
against a real schema copy. Guards against template-variable rename
regressions that unit tests miss.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def route_db(tmp_db, monkeypatch):
    """Patch DATABASE_PATH on modules that captured it via
    `from config import DATABASE_PATH` at import time."""
    import payments_alloc, cashflow
    for mod in (payments_alloc, cashflow):
        monkeypatch.setattr(mod, 'DATABASE_PATH', tmp_db, raising=True)
    return tmp_db


def _client_as_admin():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'test-admin'
    return c


def _client_as_staff():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'staff'
        s['username'] = 'test-staff'
    return c


def test_cashflow_route_renders_credit_section_header(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'ยอดเครดิตลูกค้าค้างคืน' in body, (
        "Expected the new credit-balance section header to render in "
        "/cashflow output but it was not found."
    )


def test_cashflow_route_default_filter_offers_show_all_link(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    # Default-filter view has the show-all toggle visible.
    assert 'show_all=1' in body, (
        "Expected ?show_all=1 toggle link in default-filter view."
    )


def test_cashflow_route_show_all_offers_hide_link(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow?show_all=1')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # show_all view replaces the toggle text with the inverse.
    assert 'ซ่อนรายการต่ำกว่า' in body, (
        "Expected the 'hide low-value rows' link when show_all=1 is on."
    )


def test_cashflow_route_staff_role_blocked(route_db):
    c = _client_as_staff()
    resp = c.get('/cashflow', follow_redirects=False)
    # Existing admin/manager gate — must still 302 to /, never leaking
    # the new section.
    assert resp.status_code == 302


def test_cashflow_route_renders_a_real_credit_row(route_db):
    """Smoke that the populated table actually emits at least one row
    against the live-DB clone — guards against template-side fmt_price /
    url_for breakage that the header-only tests above would miss.

    Asserted against the post-VAT-fix live data described in
    memory/project_2026_05_20_resume_here.md (commit 339e92a):
    IV6800219 / หน้าร้านS / ฿290 is the only material credit row.
    """
    import payments_alloc as pa
    rows = pa.customer_credit_rows(threshold=5.0)
    if not rows:
        pytest.skip(
            "No overpaid invoices in live DB clone — test is a no-op until "
            "the data set surfaces at least one row ≥ ฿5. See memory entry "
            "project_2026_05_20_resume_here.md for the post-VAT-fix state."
        )

    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    top = rows[0]

    # The biggest credit row's doc_base + url_for(sales_doc) link must
    # appear in the rendered HTML.
    assert top['doc_base'] in body
    assert f"/sales/doc/{top['doc_base']}" in body
    # Header "รวม" line must be present with credit_total computed.
    assert f'{len(rows)} รายการ' in body
