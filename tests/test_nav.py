"""Tests for inventory_app/nav.py — the ONE shared NAV list (Phase 2 of the PWA
nav redesign). See projects/pwa-nav-redesign/plan.md ("NAV design" + "Tests" §,
tests 3-5) and the durable gotchas in
~/.claude/projects/-Users-putty-Sendai-Boonsawat/memory/project_2026_07_16_pwa_nav_redesign.md.

Written before nav.py existed (TDD, per erp-engineering-discipline.md — nav.py
feeds the desktop sidebar in a future phase, a risky change class).

Python 3.9 — no `X | None` syntax.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from nav import NAV, nav_sections, active_link
from access_control import _ENDPOINT_MODULE, _GENERAL_ALLOWED


# ── test 3: NAV covers today's sidebar ────────────────────────────────────────
# The exact endpoint set inside base.html's <nav class="sidebar-nav"> block,
# captured by grepping url_for(...) calls there (verified 2026-07-16 at commit
# 639e857, pre-refactor). admin.toggle_db_routes is the ONE documented exception
# — it's a POST form action, not a link, and stays hand-coded in base.html.
_SIDEBAR_ENDPOINTS = frozenset([
    'dashboard', 'inventory.alerts_view', 'review.index',
    'products.product_list', 'inventory.transaction_history', 'inventory.conversion_list',
    'labels.manage', 'labels.print_page',
    'sales.trade_dashboard', 'sales.sales_view', 'sales.purchases_view',
    'partners.customer_list', 'partners.supplier_list', 'call.call_list',
    'ecommerce.ecommerce', 'marketplace.dashboard', 'marketplace.review',
    'accounting.accounting_summary', 'accounting.cashflow_dashboard',
    'accounting.ar_dashboard', 'accounting.ap_dashboard', 'commission.commission_dashboard',
    'me.leave', 'me.payslip_list',
    'hr.dashboard', 'hr.employee_list', 'hr.leave_list', 'hr.advance_list', 'hr.payroll_list',
    'cashbook.dashboard',
    'bsn.unified_import', 'bsn.express_dbf_import', 'bsn.mapping', 'bsn.unit_conversions',
    'customer_review.normalize_list', 'naming.index',
    'admin.user_list', 'admin.cashbook_account_list', 'admin.backups_list',
    'admin.upload_db', 'admin.download_db',
])


def _nav_endpoints():
    return {link['ep'] for section in NAV for link in section['links']}


def test_sidebar_endpoint_fixture_is_41():
    # Pins the fixture itself so a future hand-edit to it is deliberate.
    assert len(_SIDEBAR_ENDPOINTS) == 41


def test_nav_covers_current_sidebar():
    missing = _SIDEBAR_ENDPOINTS - _nav_endpoints()
    assert not missing, f"endpoints in base.html's sidebar but missing from NAV: {missing}"


# ── test 4: every NAV endpoint has a module (the sidebar-disappears guard) ────

def test_nav_endpoints_in_endpoint_module():
    missing = sorted({link['ep'] for section in NAV for link in section['links']}
                      - set(_ENDPOINT_MODULE))
    assert not missing, f"NAV endpoints missing from _ENDPOINT_MODULE: {missing}"


# ── test 5: role filtering ────────────────────────────────────────────────────

def test_general_drawer_is_only_of_chan_and_app():
    """general's flat drawer (nav_sections('general'), module=None) must be
    EXACTLY ของฉัน (leave/payslip) + แอป's help_install — nothing that would
    bounce back to stock search. mobile.sales_trip is the one link excluded
    from แอป for general specifically (roles_exclude), matching
    access_control._GENERAL_ALLOWED."""
    eps = {link['ep'] for section in nav_sections('general') for link in section['links']}
    allowed = _GENERAL_ALLOWED | {'help_install'}
    assert eps <= allowed, f"general drawer leaks dead links: {eps - allowed}"
    assert eps == {'me.leave', 'me.payslip_list', 'help_install'}


def test_finance_hidden_from_staff():
    assert not any(s['section'] == 'การเงิน' for s in nav_sections('staff'))


def test_finance_visible_to_admin_manager_shareholder():
    for role in ('admin', 'manager', 'shareholder'):
        assert any(s['section'] == 'การเงิน' for s in nav_sections(role)), role


def test_of_chan_hidden_from_admin_and_shareholder():
    # Mirrors base.html's own gate: session.role in ['staff','manager','general'].
    for role in ('admin', 'shareholder'):
        assert not any(s['section'] == 'ของฉัน' for s in nav_sections(role)), role


def test_of_chan_visible_to_staff_manager_general():
    for role in ('staff', 'manager', 'general'):
        assert any(s['section'] == 'ของฉัน' for s in nav_sections(role)), role


def test_admin_module_is_admin_only():
    for role in ('manager', 'staff', 'shareholder', 'general'):
        assert not any(s['section'] == 'ระบบ' for s in nav_sections(role)), role
    assert any(s['section'] == 'ระบบ' for s in nav_sections('admin'))


def test_hr_section_excludes_shareholder_matching_base_html_landmine():
    """base.html's บุคลากร (HR) section gates session.role in ['admin','manager']
    — NOT the _MODULE_DEFS ('admin','manager','shareholder') set. This is the
    documented `is_manager` landmine (access_control.py :391 vs :420); NAV must
    reproduce the section as it ACTUALLY renders, not the module switcher's more
    generous role list."""
    assert not any(s['section'] == 'บุคลากร (HR)' for s in nav_sections('shareholder'))
    for role in ('admin', 'manager'):
        assert any(s['section'] == 'บุคลากร (HR)' for s in nav_sections(role)), role


# ── internal consistency ──────────────────────────────────────────────────────

def test_every_section_has_at_least_one_link():
    assert all(section['links'] for section in NAV)


def test_no_duplicate_endpoints_within_a_section():
    for section in NAV:
        eps = [link['ep'] for link in section['links']]
        assert len(eps) == len(set(eps)), section['section']


@pytest.mark.parametrize('role', ['admin', 'manager', 'staff', 'shareholder', 'general'])
def test_module_scoped_is_subset_of_flat(role):
    """module='x' scoping (the future desktop consumer) never surfaces a link
    that module=None (the drawer) doesn't already have."""
    flat_eps = {link['ep'] for s in nav_sections(role) for link in s['links']}
    modules = {s['module'] for s in NAV if s['module']}
    for module in modules:
        scoped_eps = {link['ep'] for s in nav_sections(role, module) for link in s['links']}
        assert scoped_eps <= flat_eps, (role, module, scoped_eps - flat_eps)


def test_desktop_false_sections_dropped_when_module_scoped():
    for role in ('admin', 'manager', 'staff', 'shareholder', 'general'):
        for module in {s['module'] for s in NAV if s['module']}:
            sections = nav_sections(role, module)
            assert not any(s.get('desktop') is False for s in sections)


def test_badge_model_is_dict_not_bare_string():
    """A bare-string badge model would leak bsn.mapping's pending_suggestions_count
    to shareholder (base.html:268 gates it to is_manager = admin/manager only)."""
    mapping_link = next(link for section in NAV for link in section['links']
                         if link['ep'] == 'bsn.mapping')
    assert isinstance(mapping_link['badge'], dict)
    assert mapping_link['badge']['roles'] == {'admin', 'manager'}


# ── active_link(): spot-check the heterogeneous matcher styles ported faithfully.
# Not one of the plan's numbered tests (no live consumer of active_link() until
# the desktop sidebar is switched over) — cheap, and de-risks that future phase.

@pytest.mark.parametrize('endpoint,expected_ep', [
    ('dashboard', 'dashboard'),                             # exact
    ('review.scan', 'review.index'),                        # prefix
    ('products.product_pricing', 'products.product_list'),  # substring, not prefix
    ('labels.edit', 'labels.manage'),                        # prefix
    ('marketplace.unmapped', 'marketplace.dashboard'),       # prefix
    ('marketplace.review', 'marketplace.review'),            # prefix-with-exclusion boundary
    ('accounting.ar_followup', 'accounting.ar_dashboard'),   # or-list
    ('accounting.ar_followup_customer', 'accounting.ar_dashboard'),  # or-list
    ('hr.payslip', 'hr.payroll_list'),                       # or-list beyond the prefix
    ('hr.payroll_detail', 'hr.payroll_list'),                # prefix
    ('inventory.conversion_history', 'inventory.conversion_list'),  # prefix
    ('me.payslip_detail', 'me.payslip_list'),                # prefix
    ('customer_review.normalize_detail', 'customer_review.normalize_list'),  # prefix
])
def test_active_link_matchers(endpoint, expected_ep):
    result = active_link(endpoint)
    assert result is not None and result[1] == expected_ep, (endpoint, result)


def test_labels_print_page_excluded_from_labels_manage():
    assert active_link('labels.print_page') == ('operation', 'labels.print_page')


def test_admin_download_db_never_highlights():
    # base.html gives this link no active-state clause at all.
    assert active_link('admin.download_db') is None


def test_active_link_unknown_or_empty_endpoint_is_none():
    assert active_link('') is None
    assert active_link(None) is None
    assert active_link('nonexistent.endpoint') is None
