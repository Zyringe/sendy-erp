"""Unit tests for the role-aware mobile bottom-nav slot builder.

SUPERSEDES Put's 2026-06-11 decision (module-header bar: สินค้า·การค้า·บุคลากร·
การเงิน, plus an unconditional ลาของฉัน/สลิป append for every role — 5-7 slots
total). Put's 2026-07-16 call (pwa-nav-redesign, plan.md): the bar slims to 3
module slots — หน้าหลัก·สินค้า·การค้า — plus the เพิ่มเติม drawer button.
ลาของฉัน/สลิป move into the drawer's ของฉัน section ("if they want payslip or
leave just go see in เพิ่มเติม tab" — Put). The HR/การเงิน module slots are
dropped too; those modules now live in the drawer (nav.py's 'บุคลากร (HR)'/
'การเงิน' sections), reachable via เพิ่มเติม.

`general` (บอล's PWA kiosk role) is UNCHANGED here: สต็อก·ลาของฉัน·สลิป +
เพิ่มเติม — its only working functions, load-bearing, explicitly out of scope
for this redesign (see plan.md's "general kiosk is fragile" gotcha).

Landing-route safety verified in access_control.py: dashboard / products.
product_list / sales.trade_dashboard are open to every non-general role (no
403/redirect for any of them), so — unlike the old hr/finance manager_only
split — there is nothing left to role-filter on the office bar; all four
office roles see the identical 3 slots.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from app import build_mobile_nav_slots


def _keys(role, endpoint=''):
    return [s['key'] for s in build_mobile_nav_slots(role, endpoint)]


@pytest.mark.parametrize('role', ['admin', 'manager', 'staff', 'shareholder'])
def test_office_roles_see_exactly_home_products_trade(role):
    assert _keys(role) == ['home', 'products', 'trade']


def test_unknown_or_empty_role_falls_back_to_office_visibility():
    # No slot here 403s/redirects for any role, so there is nothing left to hide
    # from an unexpected/blank role (unlike the old hr/finance manager_only gate).
    assert _keys('') == ['home', 'products', 'trade']
    assert _keys('something-else') == ['home', 'products', 'trade']


def test_general_kiosk_bar_unchanged():
    assert _keys('general') == ['stock', 'my_leave', 'my_payslip']


def test_slot_labels():
    by_key = {s['key']: s['label'] for s in build_mobile_nav_slots('admin')}
    assert by_key == {'home': 'หน้าหลัก', 'products': 'สินค้า', 'trade': 'การค้า'}


def test_every_slot_has_required_render_fields():
    for s in build_mobile_nav_slots('admin', 'dashboard'):
        assert set(['key', 'label', 'icon', 'endpoint', 'active']).issubset(s)
        assert s['icon'].startswith('bi-')
        assert isinstance(s['active'], bool)


@pytest.mark.parametrize('endpoint,expected_active', [
    ('dashboard', 'home'),
    # Put's call (2026-07-16): หน้าหลัก owns the whole ภาพรวม nav GROUP, not just
    # the dashboard endpoint — แจ้งเตือน/ตรวจบิล are the Dashboard group and should
    # read as หน้าหลัก, rather than leaving เพิ่มเติม lit. The group is defined by
    # nav.py's ภาพรวม section (single source), so adding a link there follows here.
    ('inventory.alerts_view', 'home'),
    ('review.index', 'home'),
    ('review.scan', 'home'),                            # prefix matcher, via NAV
    ('products.product_list', 'products'),
    ('products.product_detail', 'products'),
    ('inventory.transaction_history', 'products'),      # operation module
    ('sales.trade_dashboard', 'trade'),
    ('sales.sales_view', 'trade'),
    ('partners.customer_list', 'trade'),
    ('ecommerce.ecommerce', 'trade'),
])
def test_active_slot_highlight_for_admin(endpoint, expected_active):
    slots = build_mobile_nav_slots('admin', endpoint)
    active = [s['key'] for s in slots if s['active']]
    assert active == [expected_active], f"{endpoint}: {active}"


@pytest.mark.parametrize('endpoint', [
    'accounting.accounting_summary', 'accounting.cashflow_dashboard',  # finance — now drawer-only
    'hr.dashboard', 'hr.payroll_list',                                 # hr — now drawer-only
    'bsn.unified_import', 'admin.user_list', 'cashbook.dashboard',     # data/admin/cashbook
    'me.leave', 'me.payslip_list', 'me.payslip_detail',                # ของฉัน — เพิ่มเติม must light
])
def test_no_slot_active_lets_more_button_light(endpoint):
    # All of these live in the drawer now. No bottom-nav slot may claim them, or
    # a role landing there would see the wrong tab highlighted while เพิ่มเติม
    # (which actually holds the page) stays dark.
    #
    # ⚠ me.* is the landmine: _ENDPOINT_MODULE maps me.leave/me.payslip* to
    # 'overview' (Phase 1.5, so the desktop sidebar stays unchanged there), so a
    # หน้าหลัก slot keyed on the MODULE would wrongly light on ลาของฉัน/สลิป.
    # Keying it on nav.py's ภาพรวม SECTION avoids that: me.* lives in the ของฉัน
    # section (module=None), so it can never resolve to หน้าหลัก.
    slots = build_mobile_nav_slots('admin', endpoint)
    assert [s['key'] for s in slots if s['active']] == [], endpoint


def test_no_role_gets_a_slot_whose_landing_bounces():
    # dashboard / products.product_list / sales.trade_dashboard are all open to
    # every non-general role — verified by the identical 3-slot set below.
    for role in ('admin', 'manager', 'staff', 'shareholder'):
        assert _keys(role) == ['home', 'products', 'trade']
