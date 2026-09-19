"""TDD for Phase 1 of projects/customer-edit-card/plan.md — code-keyed
customer route + name→code redirect shim.

Fixes two live bugs (see the plan for the full write-up):
  BUG 1 — customer_reassign's save redirect used the MASTER name
          (`customers.name`), but the page was keyed on the BILL name
          (`sales_transactions.customer`) — 198/274 customers land on an
          empty page after saving. The fix redirects by code instead.
  BUG 2 — one bill name can span >1 physical company (exactly one case
          today: 'ทรัพย์ทวี' = 43ท013 'ร้าน ทรัพย์ทวี' + 01พ14
          'บจก. พงศ์ทรัพย์ทวี'). The old name-keyed page silently merged
          both companies' sales into one page via `LIMIT 1`.

Also required: the 2,390 customers with a master row but zero
sales_transactions rows were unreachable under the old name-keyed route at
all — the new /customer/code/<code> route resolves the master directly from
`customers`, so they render too.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from urllib.parse import quote, unquote


TWIN_NAME = 'ทรัพย์ทวี'
TWIN_CODES = ('43ท013', '01พ14')
AR_CODE = 'ZZCODEAR'
AR_NAME = 'ลูกค้าทดสอบบิลค้างตามรหัส'
AR_DOC = 'ZZCODEAR-IV'


def _seed_twins(db_path):
    """Force the duplicated bill name and each code's document count."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM sales_transactions WHERE customer = ? OR customer_code IN (?, ?)",
            (TWIN_NAME,) + TWIN_CODES)
        conn.execute("DELETE FROM customers WHERE code IN (?, ?)", TWIN_CODES)
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)",
                     (TWIN_CODES[0], 'ร้าน ทรัพย์ทวี'))
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)",
                     (TWIN_CODES[1], 'บจก. พงศ์ทรัพย์ทวี'))
        for code, line_counts in ((TWIN_CODES[0], [6] * 12 + [10]),
                                  (TWIN_CODES[1], [1])):
            for i, line_count in enumerate(line_counts):
                doc = 'ZZCODE-{}-{:02d}'.format(code, i)
                for suffix in range(1, line_count + 1):
                    conn.execute(
                        """INSERT INTO sales_transactions
                             (date_iso, doc_no, doc_base, customer, customer_code,
                              qty, unit, unit_price, vat_type, total, net)
                           VALUES ('2026-01-01', ?, ?, ?, ?, 1, 'ตัว', 100, 1, 100, 100)""",
                        ('{}-{}'.format(doc, suffix), doc, TWIN_NAME, code))
        conn.commit()
    finally:
        conn.close()


def _seed_unpaid_bill(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        snap = conn.execute(
            "SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity='BSN'"
        ).fetchone()[0]
        batch_id = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert snap and batch_id
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ? OR doc_no = ?",
                     (AR_CODE, AR_DOC))
        conn.execute("DELETE FROM ar_writeoffs WHERE doc_no = ?", (AR_DOC,))
        conn.execute(
            """INSERT INTO express_ar_outstanding
                 (batch_id, snapshot_date_iso, customer_code, customer_name,
                  doc_date_iso, doc_no, is_anomalous, bill_amount, paid_amount,
                  outstanding_amount, entity)
               VALUES (?, ?, ?, ?, '2026-01-01', ?, 0, 750, 0, 750, 'BSN')""",
            (batch_id, snap, AR_CODE, AR_NAME, AR_DOC))
        conn.commit()
        return snap
    finally:
        conn.close()


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


# ── resolve_customer_codes ──────────────────────────────────────────────────

def test_resolve_customer_codes_single(tmp_db):
    import models
    assert models.resolve_customer_codes('คิมเฮง') == ['11ค09']


def test_resolve_customer_codes_ambiguous_name(tmp_db):
    import models
    _seed_twins(tmp_db)
    assert sorted(models.resolve_customer_codes(TWIN_NAME)) == ['01พ14', '43ท013']


def test_resolve_customer_codes_unknown_name(tmp_db):
    import models
    assert models.resolve_customer_codes('ไม่มีลูกค้านี้แน่นอน') == []


# ── /customer/code/<code> ───────────────────────────────────────────────────

def test_by_code_renders_for_billing_customer(tmp_db):
    c = _client(tmp_db)
    r = c.get(f'/customer/code/{quote("11ค09")}')
    assert r.status_code == 200
    assert '>คิมเฮง<' in r.data.decode()


def test_by_code_renders_for_billless_customer(tmp_db):
    """01ก01 has a customers-master row and ZERO sales_transactions rows —
    unreachable via the old name-keyed route at all. Must not error."""
    c = _client(tmp_db)
    r = c.get(f'/customer/code/{quote("01ก01")}')
    assert r.status_code == 200


def test_bug2_two_companies_render_separate_totals(tmp_db):
    """43ท013 (13 docs, 82 lines — #493 groups by doc_base, not doc_no) and
    01พ14 (1 doc) must never merge."""
    import models
    _seed_twins(tmp_db)
    big = models.get_customer_summary_by_code('43ท013')
    small = models.get_customer_summary_by_code('01พ14')
    assert big['summary']['doc_count'] == 13
    assert small['summary']['doc_count'] == 1
    assert len(big['docs']) == 13
    assert len(small['docs']) == 1
    assert big['customer_info']['name'] == 'ร้าน ทรัพย์ทวี'
    assert small['customer_info']['name'] == 'บจก. พงศ์ทรัพย์ทวี'


def test_bug2_two_companies_render_separate_totals_via_route(tmp_db):
    """Same proof, through the real route/template render (not just the model)."""
    _seed_twins(tmp_db)
    c = _client(tmp_db)
    big = c.get(f'/customer/code/{quote("43ท013")}').data.decode()
    small = c.get(f'/customer/code/{quote("01พ14")}').data.decode()
    assert big != small


# ── /customer/<name> shim ───────────────────────────────────────────────────

def test_name_shim_redirects_to_code_preserving_date_filters(tmp_db):
    c = _client(tmp_db)
    r = c.get(f'/customer/{quote("คิมเฮง")}?date_from=2025-01-01&date_to=2025-12-31',
               follow_redirects=False)
    assert r.status_code == 302
    location = unquote(r.headers['Location'])
    assert '/customer/code/11ค09' in location
    assert 'date_from=2025-01-01' in location
    assert 'date_to=2025-12-31' in location


def test_name_shim_end_to_end_reaches_code_page(tmp_db):
    c = _client(tmp_db)
    r = c.get(f'/customer/{quote("คิมเฮง")}', follow_redirects=True)
    assert r.status_code == 200
    assert len(r.history) == 1
    assert r.history[0].status_code == 302


def test_name_shim_ambiguous_name_redirects_to_customer_list_not_a_code(tmp_db):
    """ทรัพย์ทวี spans 2 codes (BUG 2) — must send Put to pick, never guess."""
    _seed_twins(tmp_db)
    c = _client(tmp_db)
    r = c.get(f'/customer/{quote("ทรัพย์ทวี")}', follow_redirects=False)
    assert r.status_code == 302
    location = unquote(r.headers['Location'])
    assert location.startswith('/customers?q=')
    assert 'ทรัพย์ทวี' in location
    assert '/customer/code/' not in location


def test_name_shim_unknown_name_redirects_to_customer_list(tmp_db):
    c = _client(tmp_db)
    r = c.get(f'/customer/{quote("ไม่มีลูกค้านี้แน่นอน")}', follow_redirects=False)
    assert r.status_code == 302
    assert unquote(r.headers['Location']).startswith('/customers?q=')


# ── BUG 1: reassign redirect lands on a page that has content ──────────────

def test_reassign_redirect_lands_on_code_page_not_master_name(tmp_db):
    """21ก002's bill name (กมลค้าวัสดุภัณฑ์) differs from its master name
    (หจก. กมลค้าวัสดุภัณฑ์) — the old redirect (by master name) landed on a
    page keyed on a name nothing links to. The fix redirects by code, which
    is identical either way."""
    c = _client(tmp_db)
    r = c.post('/customer/21ก002/reassign',
               data={'salesperson': '00', 'region_id': ''},
               follow_redirects=False)
    assert r.status_code == 302
    assert unquote(r.headers['Location']).endswith('/customer/code/21ก002')


def test_reassign_redirect_destination_actually_renders(tmp_db):
    c = _client(tmp_db)
    r = c.post('/customer/21ก002/reassign',
               data={'salesperson': '00', 'region_id': ''},
               follow_redirects=True)
    assert r.status_code == 200
    assert len(r.history) == 1 and r.history[0].status_code == 302


# ── get_customer_unpaid_bills_by_code ───────────────────────────────────────

def test_unpaid_bills_by_code_returns_the_owned_customer(tmp_db):
    import models

    expected_snapshot = _seed_unpaid_bill(tmp_db)
    rows, snapshot = models.get_customer_unpaid_bills_by_code(AR_CODE)

    assert snapshot == expected_snapshot
    assert len(rows) == 1
    assert rows[0]['doc_base'] == AR_DOC
    assert rows[0]['customer_code'] == AR_CODE


def test_unpaid_bills_by_code_returns_list_for_customer_with_no_ar(tmp_db):
    import models
    rows, _snap = models.get_customer_unpaid_bills_by_code('43ท013')
    assert isinstance(rows, list)


# ── three-nav-surfaces: _ENDPOINT_MODULE + break-it-once ───────────────────

def test_endpoint_module_maps_customer_detail_to_trade():
    from access_control import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE['partners.customer_detail'] == 'trade'


def test_nav_match_list_highlights_customer_link_on_detail_page():
    """nav.py:90's match list — the 'ลูกค้า' sidebar link must highlight on
    the new code-keyed page too, same as it already does for customer_summary.
    Deliberately NOT touching tests/nav_snapshot.json for this (regenerating
    it re-captures every role x module snapshot in one shot — out of
    proportion for a cosmetic highlight check); this pins the underlying
    match-list DATA directly instead."""
    import nav
    assert (nav.active_link('partners.customer_detail', 'trade')
            == nav.active_link('partners.customer_summary', 'trade')
            == ('trade', 'partners.customer_list'))


def test_endpoint_module_break_it_once():
    """Proves the guard this entry exists for actually catches its absence:
    per erp-engineering-discipline's 'A navigable page lives in THREE nav
    surfaces' rule, a missing _ENDPOINT_MODULE entry makes
    `active_module = _ENDPOINT_MODULE.get(endpoint, 'overview')` silently
    fall back to 'overview' — the WHOLE trade sidebar would vanish on this
    page, not just its own link."""
    import access_control
    saved = access_control._ENDPOINT_MODULE.pop('partners.customer_detail')
    try:
        assert access_control._ENDPOINT_MODULE.get(
            'partners.customer_detail', 'overview') == 'overview'
    finally:
        access_control._ENDPOINT_MODULE['partners.customer_detail'] = saved
    assert access_control._ENDPOINT_MODULE['partners.customer_detail'] == 'trade'


# ── The page's OWN links must keep you on the page ──────────────────────────
# Review gap (2026-08-01): every test above checked that the shim redirects
# correctly, but none checked where the RENDERED page points. The clear-filter
# button was still built with url_for('partners.customer_summary', name=...),
# so on both ทรัพย์ทวี companies — the pair this phase exists to split — and on
# every bill-less customer it bounced through the shim and ejected the user to
# /customers. Assert on the href, never the Thai label: a bare substring check
# passes on an unchanged page.

def _clear_filter_href(html):
    """The 'ล้าง' button's href, located by its class, not by its label."""
    import re
    m = re.search(
        r'href="([^"]*)"[^>]*class="btn btn-sm btn-outline-secondary ms-1"', html)
    assert m, 'clear-filter link not found on the page'
    return unquote(m.group(1))


def test_clear_filter_link_stays_on_the_code_page_ambiguous_name(tmp_db):
    """43ท013 and 01พ14 share the bill name ทรัพย์ทวี. A name-built link here
    resolves to 2 codes → the shim refuses to guess → user is ejected."""
    _seed_twins(tmp_db)
    c = _client(tmp_db)
    for code in ('43ท013', '01พ14'):
        html = c.get(f'/customer/code/{quote(code)}').data.decode()
        assert _clear_filter_href(html) == f'/customer/code/{code}'


def test_clear_filter_link_stays_on_the_code_page_billless(tmp_db):
    """A bill-less customer's data.customer is the MASTER name, which resolves
    to zero codes — a name-built link ejects with 'ไม่พบรหัสลูกค้า'."""
    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote("01ก01")}').data.decode()
    assert _clear_filter_href(html) == '/customer/code/01ก01'


def test_clear_filter_link_actually_round_trips(tmp_db):
    """Follow the link for real: 200 on the same URL, not a 302 to /customers."""
    c = _client(tmp_db)
    start = f'/customer/code/{quote("43ท013")}'
    html = c.get(f'{start}?date_from=2025-01-01').data.decode()
    r = c.get(quote(_clear_filter_href(html), safe='/?=&'))
    assert r.status_code == 200, f'clear-filter link left the page: {r.status_code} {r.headers.get("Location")}'
    assert 'ทรัพย์ทวี' in r.data.decode()


# ── Unknown code must not render a page titled after the URL ────────────────

def test_unknown_code_404s(tmp_db):
    c = _client(tmp_db)
    assert c.get('/customer/code/NOPE999').status_code == 404


def test_known_code_survives_a_date_filter_that_excludes_every_bill(tmp_db):
    """The 404 guard reads a date-INDEPENDENT `exists` flag. Keying it off the
    filtered rows instead would 404 a real customer mid-filter — this pins that."""
    c = _client(tmp_db)
    r = c.get(f'/customer/code/{quote("43ท013")}?date_from=2099-01-01')
    assert r.status_code == 200
    import models
    d = models.get_customer_summary_by_code('43ท013', '2099-01-01', None)
    assert d['exists'] is True and d['summary']['doc_count'] == 0
