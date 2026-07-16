"""Nav reorg (PR1): module 'accounting' ('การค้า & บัญชี') splits into
'trade' ('การค้า') + 'finance' ('การเงิน'). See
projects/sendy-nav-finance-revamp/plan.md Phase 1 for the design.

Frozen partition: the exact endpoint set that mapped to 'accounting' BEFORE
this split (66 total, per the plan's manual count) must all still exist,
none may still say 'accounting', and each must land in exactly one of
{trade, finance, data}.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

_OLD_ACCOUNTING_ENDPOINTS = frozenset([
    # accounting.* → finance (14)
    'accounting.accounting_summary', 'accounting.cashflow_dashboard',
    'accounting.revenue_dashboard', 'accounting.revenue_unmapped_drilldown',
    'accounting.ar_followup', 'accounting.ar_followup_customer',
    'accounting.ar_followup_log_new', 'accounting.ar_followup_log_delete',
    'accounting.ar_followup_export', 'accounting.ar_dashboard',
    'accounting.express_ar_dashboard', 'accounting.express_ar_customer',
    'accounting.express_ap_dashboard', 'accounting.ap_dashboard',
    # commission.* → finance (12)
    'commission.commission_dashboard', 'commission.commission_record_payout',
    'commission.commission_delete_payout', 'commission.commission_payouts_list',
    'commission.commission_drilldown', 'commission.commission_invoice_detail',
    'commission.commission_export', 'commission.commission_overrides_list',
    'commission.commission_overrides_new', 'commission.commission_overrides_edit',
    'commission.commission_overrides_toggle', 'commission.commission_overrides_delete',
    # sales.payment_* → finance (2)
    'sales.payment_status', 'sales.payment_customers',
    # sales trade/purchases → trade (5)
    'sales.trade_dashboard', 'sales.sales_view', 'sales.sales_doc',
    'sales.purchases_view', 'sales.purchases_doc',
    # partners.* → trade (5)
    'partners.customer_list', 'partners.customer_summary', 'partners.customer_map',
    'partners.supplier_list', 'partners.supplier_summary',
    # call.* → trade (3)
    'call.call_list', 'call.call_card', 'call.call_mark_called',
    # ecommerce.* → trade (9)
    'ecommerce.ecommerce', 'ecommerce.ecommerce_import', 'ecommerce.ecommerce_sku_edit',
    'ecommerce.ecommerce_export', 'ecommerce.ecommerce_mapping_export',
    'ecommerce.ecommerce_mapping_import', 'ecommerce.ecommerce_listings_import',
    'ecommerce.ecommerce_listings_mapping_export', 'ecommerce.ecommerce_listings_mapping_import',
    # marketplace.* → trade (12)
    'marketplace.dashboard', 'marketplace.import_orders', 'marketplace.unmapped',
    'marketplace.settlement', 'marketplace.settlement_import', 'marketplace.link_iv',
    'marketplace.api_iv_candidates', 'marketplace.api_order_detail',
    'marketplace.reconciliation', 'marketplace.review_amount', 'marketplace.review',
    'marketplace.review_dismiss',
    # customer_review.* → data (4)
    'customer_review.normalize_list', 'customer_review.normalize_detail',
    'customer_review.normalize_confirm', 'customer_review.normalize_skip',
])


def test_old_accounting_endpoint_set_is_66():
    assert len(_OLD_ACCOUNTING_ENDPOINTS) == 66


def test_every_old_accounting_endpoint_repartitioned():
    from access_control import _ENDPOINT_MODULE
    missing = [e for e in _OLD_ACCOUNTING_ENDPOINTS if e not in _ENDPOINT_MODULE]
    assert not missing, f"endpoints dropped from _ENDPOINT_MODULE: {missing}"
    still_accounting = sorted(e for e in _OLD_ACCOUNTING_ENDPOINTS if _ENDPOINT_MODULE[e] == 'accounting')
    assert not still_accounting, f"endpoints still mapped to dead 'accounting' key: {still_accounting}"
    bad = {e: _ENDPOINT_MODULE[e] for e in _OLD_ACCOUNTING_ENDPOINTS
           if _ENDPOINT_MODULE[e] not in ('trade', 'finance', 'data')}
    assert not bad, f"endpoints mapped outside {{trade,finance,data}}: {bad}"


def test_partition_counts_match_plan():
    from access_control import _ENDPOINT_MODULE
    counts = {'trade': 0, 'finance': 0, 'data': 0}
    for e in _OLD_ACCOUNTING_ENDPOINTS:
        counts[_ENDPOINT_MODULE[e]] += 1
    assert counts == {'trade': 34, 'finance': 28, 'data': 4}


def test_accounting_module_key_removed_and_trade_finance_present():
    from access_control import _MODULE_DEFS
    keys = {m['key'] for m in _MODULE_DEFS}
    assert 'accounting' not in keys
    assert {'trade', 'finance'} <= keys


def test_mobile_finance_page_has_no_bottom_nav_slot():
    # SUPERSEDED (pwa-nav-redesign, 2026-07-16): the bottom bar no longer carries
    # a การเงิน (or บุคลากร) slot at all — see tests/test_mobile_nav.py's own
    # supersession note. Finance now lives in the drawer only; landing on it
    # must light no bottom-nav slot (เพิ่มเติม lights instead).
    from access_control import build_mobile_nav_slots
    slots = build_mobile_nav_slots('shareholder', 'accounting.accounting_summary')
    assert 'finance' not in {s['key'] for s in slots}
    assert [s['key'] for s in slots if s['active']] == []


# ── Render tests: a page's sidebar + the (always-rendered) mobile drawer ──────

def _client(role='admin', user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def test_trade_page_nav_links(tmp_db):
    html = _client().get('/trade-dashboard').get_data(as_text=True)
    # present on both sidebar (trade block) and drawer (trade group)
    for href in ('/suppliers', '/trade-dashboard', '/sales', '/purchases',
                 '/ecommerce', '/marketplace/review'):
        assert html.count(f'href="{href}"') >= 2, f"{href}: expected on sidebar+drawer"
    # sidebar-only on this page (not in the drawer's trade group)
    for href in ('/customers', '/call', '/marketplace'):
        assert html.count(f'href="{href}"') >= 1, f"{href}: expected on sidebar"
    assert 'AP / ซัพพลายเออร์' not in html
    assert 'Supplier' not in html
    assert 'การเงิน (Express)' not in html


def test_finance_page_nav_links(tmp_db):
    html = _client().get('/accounting').get_data(as_text=True)
    # present on both sidebar (finance block) and drawer (finance group)
    for href in ('/ap', '/commission'):
        assert html.count(f'href="{href}"') >= 2, f"{href}: expected on sidebar+drawer"
    # sidebar-only on this page
    for href in ('/accounting', '/cashflow'):
        assert html.count(f'href="{href}"') >= 1, f"{href}: expected on sidebar"
    assert 'href="/revenue"' not in html
    assert 'AP / ซัพพลายเออร์' not in html
    assert 'Supplier' not in html
    assert 'การเงิน (Express)' not in html


def test_data_page_has_customer_review_link(tmp_db):
    html = _client().get('/customers/normalize').get_data(as_text=True)
    assert html.count('href="/customers/normalize"') >= 1
