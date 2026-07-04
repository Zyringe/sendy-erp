"""Unit tests for the role-aware mobile bottom-nav slot builder.

Put's decision (2026-06-11): the mobile bottom nav = module headers
สินค้า · การค้า · บุคลากร · บัญชี · เพิ่มเติม(drawer). Slots are role-aware:
a slot is hidden when the session role cannot GET its landing page (it would
403/redirect). Staff see only สินค้า/การค้า; admin/manager also see บุคลากร/บัญชี.
ตรวจบิล is NOT a bottom-nav slot (it lives in the drawer + dashboard banner).

These test the pure builder so the "never show a slot that 403s" invariant is
verified without rendering a full page. Landing-route gating verified in app.py:
  - products.product_list / trade_dashboard → all roles
  - hr.dashboard → admin/manager only
  - accounting_summary (/accounting) → redirects staff (admin/manager only)
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from app import build_mobile_nav_slots


def _keys(role, endpoint=''):
    return [s['key'] for s in build_mobile_nav_slots(role, endpoint)]


def test_staff_sees_only_products_and_trade():
    assert _keys('staff') == ['products', 'trade']


def test_manager_sees_all_four_module_slots():
    assert _keys('manager') == ['products', 'trade', 'hr', 'accounting']


def test_admin_sees_all_four_module_slots():
    assert _keys('admin') == ['products', 'trade', 'hr', 'accounting']


def test_unknown_or_empty_role_falls_back_to_staff_visibility():
    # An unexpected/blank role must NOT leak manager-only slots.
    assert _keys('') == ['products', 'trade']
    assert _keys('something-else') == ['products', 'trade']


def test_slot_labels_match_puts_decision():
    by_key = {s['key']: s['label'] for s in build_mobile_nav_slots('admin')}
    assert by_key['products'] == 'สินค้า'
    assert by_key['trade'] == 'การค้า'
    assert by_key['hr'] == 'บุคลากร'
    assert by_key['accounting'] == 'บัญชี'


def test_every_slot_has_required_render_fields():
    for s in build_mobile_nav_slots('admin', 'dashboard'):
        assert set(['key', 'label', 'icon', 'endpoint', 'active']).issubset(s)
        assert s['icon'].startswith('bi-')
        assert isinstance(s['active'], bool)


@pytest.mark.parametrize('endpoint,expected_active', [
    ('products.product_list', 'products'),
    ('products.product_detail', 'products'),
    ('inventory.transaction_history', 'products'),     # operation module
    ('sales.trade_dashboard', 'trade'),
    ('sales.sales_view', 'trade'),                  # accounting module, trade side
    ('partners.customer_list', 'trade'),
    ('ecommerce.ecommerce', 'trade'),
    ('accounting.accounting_summary', 'accounting'),     # accounting module, finance side
    ('accounting.cashflow_dashboard', 'accounting'),
    ('accounting.revenue_dashboard', 'accounting'),
    ('accounting.ar_followup', 'accounting'),
    ('hr.dashboard', 'hr'),
    ('hr.payroll_list', 'hr'),
])
def test_active_slot_highlight_for_admin(endpoint, expected_active):
    slots = build_mobile_nav_slots('admin', endpoint)
    active = [s['key'] for s in slots if s['active']]
    assert active == [expected_active], f"{endpoint}: {active}"


def test_no_slot_active_on_overview_and_data_pages():
    # dashboard (overview), import (data), user_list (admin) have no bottom-nav
    # slot — none should highlight (those modules live in the drawer).
    for endpoint in ('dashboard', 'inventory.alerts_view', 'unified_import', 'user_list'):
        slots = build_mobile_nav_slots('admin', endpoint)
        assert [s['key'] for s in slots if s['active']] == [], endpoint


def test_trade_and_accounting_are_mutually_exclusive():
    # การค้า and บัญชี share the 'accounting' module — exactly one (or neither)
    # may light up, never both, or the nav looks buggy.
    for endpoint in ('sales.trade_dashboard', 'sales.sales_view', 'accounting.accounting_summary',
                     'accounting.cashflow_dashboard', 'accounting.ar_followup'):
        active = [s['key'] for s in build_mobile_nav_slots('admin', endpoint)
                  if s['active'] and s['key'] in ('trade', 'accounting')]
        assert len(active) <= 1, f"{endpoint} lit both: {active}"
