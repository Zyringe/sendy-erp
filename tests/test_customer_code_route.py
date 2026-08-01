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
    assert sorted(models.resolve_customer_codes('ทรัพย์ทวี')) == ['01พ14', '43ท013']


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
    """43ท013 (82 docs) and 01พ14 (1 doc) must never merge."""
    import models
    big = models.get_customer_summary_by_code('43ท013')
    small = models.get_customer_summary_by_code('01พ14')
    assert big['summary']['doc_count'] == 82
    assert small['summary']['doc_count'] == 1
    assert big['customer_info']['name'] == 'ร้าน ทรัพย์ทวี'
    assert small['customer_info']['name'] == 'บจก. พงศ์ทรัพย์ทวี'


def test_bug2_two_companies_render_separate_totals_via_route(tmp_db):
    """Same proof, through the real route/template render (not just the model)."""
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

def test_unpaid_bills_by_code_matches_by_name_for_unambiguous_customer(tmp_db):
    import models
    by_name, snap_name = models.get_customer_unpaid_bills('เจริญกิจ บางโพ')
    by_code, snap_code = models.get_customer_unpaid_bills_by_code('01จ06')
    assert snap_name == snap_code
    assert len(by_code) == 1
    assert [dict(r) for r in by_code] == [dict(r) for r in by_name]


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
