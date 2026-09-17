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

def test_general_drawer_is_only_of_chan_settings_and_app():
    """general's flat drawer (nav_sections('general'), module=None) must be
    EXACTLY ของฉัน (leave/payslip) + ตั้งค่า (บัญชีของฉัน — self-service account)
    + แอป's help_install — nothing that would bounce back to stock search.
    mobile.sales_trip is the one link excluded from แอป for general specifically
    (roles_exclude), matching access_control._GENERAL_ALLOWED."""
    eps = {link['ep'] for section in nav_sections('general') for link in section['links']}
    allowed = _GENERAL_ALLOWED | {'help_install'}
    assert eps <= allowed, f"general drawer leaks dead links: {eps - allowed}"
    assert eps == {'me.leave', 'me.payslip_list', 'me.account', 'help_install'}


def test_finance_shows_staff_only_the_two_debt_pages():
    """`staff` chases debt, so it gets การเงิน — holding AR and AP and nothing
    else. It used to be hidden outright while /ar and /ap stayed openable by
    URL, which is the drift this project exists to stop (Put, Q3: AR yes,
    commission no)."""
    import permissions as P
    eps = [l['ep'] for s in nav_sections('staff')
           if s['section'] == 'การเงิน' for l in s['links']]
    assert eps == ['accounting.ar_dashboard', 'accounting.ap_dashboard'], eps
    # The three it must NOT get, named rather than implied by the list above.
    for ep in ('accounting.accounting_summary', 'accounting.cashflow_dashboard',
               'commission.commission_dashboard'):
        assert not P.may_see('staff', ep), ep


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


def test_hr_section_includes_the_shareholder_who_could_always_reach_it():
    """The `is_manager` landmine, closed.

    The module switcher has always let the shareholder into 'hr' while this
    section's own role list rendered nothing for her, so the tab opened a blank
    sidebar. Both now read `permissions`, where `hr` is MANAGEMENT.
    """
    for role in ('admin', 'manager', 'shareholder'):
        assert any(s['section'] == 'บุคลากร (HR)' for s in nav_sections(role)), role
    # Control: the section is still gated — staff and the kiosk do not get it.
    for role in ('staff', 'general'):
        assert not any(s['section'] == 'บุคลากร (HR)' for s in nav_sections(role)), role


# ── internal consistency ──────────────────────────────────────────────────────

def test_every_section_has_at_least_one_link():
    assert all(section['links'] for section in NAV)


def test_no_duplicate_endpoints_within_a_section():
    for section in NAV:
        eps = [link['ep'] for link in section['links']]
        assert len(eps) == len(set(eps)), section['section']


@pytest.mark.parametrize('role', ['admin', 'manager', 'staff', 'shareholder'])
def test_module_scoped_is_subset_of_flat(role):
    """module='x' scoping (the desktop) never surfaces a link that module=None
    (the drawer) doesn't already have — true for every role EXCEPT 'general'
    (see the next test): the drawer is a NEW, tighter fix for general (this
    project's whole point), while the desktop sidebar faithfully reproduces
    code that never restricted general at the section level to begin with."""
    flat_eps = {link['ep'] for s in nav_sections(role) for link in s['links']}
    modules = {s['module'] for s in NAV if s['module']}
    for module in modules:
        scoped_eps = {link['ep'] for s in nav_sections(role, module) for link in s['links']}
        assert scoped_eps <= flat_eps, (role, module, scoped_eps - flat_eps)


def test_general_gets_the_same_answer_flat_or_module_scoped():
    """The flat-versus-scoped asymmetry is gone.

    `roles=None` used to mean "everyone but general" in the drawer and
    "literally everyone" on the desktop, because the desktop sidebar was a
    frozen port of a base.html that only ever gated by `active_module`. The
    kiosk was therefore handed the full คลังสินค้า / การค้า / นำเข้าข้อมูล /
    ภาพรวม content in every module-scoped call — dead, since `inject_auth`
    blanks `visible_modules` for it, but dead code that read as a policy.
    Filtering each link through `may_see` answers both surfaces at once.
    """
    flat = {l['ep'] for s in nav_sections('general') for l in s['links']}
    for module in ('overview', 'operation', 'trade', 'data', 'finance', 'hr',
                   'cashbook', 'admin_module', 'settings'):
        scoped = {l['ep'] for s in nav_sections('general', module) for l in s['links']}
        assert scoped <= flat, (module, sorted(scoped - flat))
    # Control: the kiosk really is offered something, so this is not passing on
    # two empty sets.
    assert 'me.leave' in flat and len(flat) >= 3, sorted(flat)


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

@pytest.mark.parametrize('endpoint,module,expected_ep', [
    ('dashboard', 'overview', 'dashboard'),                             # exact
    ('review.scan', 'overview', 'review.index'),                        # prefix
    ('products.product_pricing', 'operation', 'products.product_list'),  # substring, not prefix
    ('labels.edit', 'operation', 'labels.manage'),                       # prefix
    ('marketplace.unmapped', 'trade', 'marketplace.dashboard'),          # prefix
    ('marketplace.review', 'trade', 'marketplace.review'),               # prefix-with-exclusion boundary
    ('accounting.ar_followup', 'finance', 'accounting.ar_dashboard'),    # or-list
    ('accounting.ar_followup_customer', 'finance', 'accounting.ar_dashboard'),  # or-list
    ('hr.payslip', 'hr', 'hr.payroll_list'),                             # or-list beyond the prefix
    ('hr.payroll_detail', 'hr', 'hr.payroll_list'),                      # prefix
    ('inventory.conversion_history', 'operation', 'inventory.conversion_list'),  # prefix
    ('me.payslip_detail', 'overview', 'me.payslip_list'),                # prefix, via an `always` section
    ('customer_review.normalize_detail', 'data', 'customer_review.normalize_list'),  # prefix
])
def test_active_link_matchers(endpoint, module, expected_ep):
    result = active_link(endpoint, module)
    assert result is not None and result[1] == expected_ep, (endpoint, result)


@pytest.mark.parametrize('endpoint,module,expected_ep', [
    # 'naming.product_save' CONTAINS 'product', so products.product_list's substring
    # matcher catches it — but base.html never renders คลังสินค้า on a data-module
    # page, so สินค้า cannot be the highlight there. Unscoped matching returned
    # ('operation','products.product_list'); scoping to the module is the fix.
    # Same bug family as PR #291 (see tests/test_nav_active_highlight.py).
    ('naming.product_save', 'data', 'naming.index'),
    ('naming.product_preview_name', 'data', 'naming.index'),
])
def test_active_link_does_not_cross_modules(endpoint, module, expected_ep):
    result = active_link(endpoint, module)
    assert result is not None and result[1] == expected_ep, (endpoint, result)
    assert result[0] == module, f"{endpoint} highlighted a link outside module {module}: {result}"


def test_labels_print_page_excluded_from_labels_manage():
    assert active_link('labels.print_page', 'operation') == ('operation', 'labels.print_page')


def test_admin_download_db_never_highlights():
    # base.html gives this link no active-state clause at all.
    assert active_link('admin.download_db', 'admin_module') is None


def test_active_link_unknown_or_empty_endpoint_is_none():
    assert active_link('', 'overview') is None
    assert active_link(None, 'overview') is None
    assert active_link('nonexistent.endpoint', 'overview') is None


# ── parity: the render gate never offers what the access gate refuses ─────────
# The two gates are separate on purpose (`permissions.py`, "Two gates, never
# one"), which is exactly why they can disagree: for eight weeks `staff` could
# open /ar by URL while the การเงิน section hid the link. Nothing noticed,
# because nothing knew the two were about the same question. This is the guard
# that notices.

_NAV_MODULES = ('overview', 'operation', 'trade', 'finance', 'hr',
                'cashbook', 'data', 'admin_module', 'settings')


def _nav_links(role, module=None):
    return [(section['section'], link['ep'])
            for section in nav_sections(role, module=module)
            for link in section['links']]


def test_the_drawer_never_offers_a_link_the_gate_refuses():
    """Every role, every link in the mobile drawer."""
    import permissions as P
    offered, refused = 0, []
    for role in ('admin', 'manager', 'staff', 'shareholder', 'general'):
        for section, ep in _nav_links(role):
            offered += 1
            if not P.may_see(role, ep):
                refused.append((role, section, ep))
    assert offered > 100, offered          # control: the sweep really swept
    assert refused == [], refused


def test_the_desktop_sidebar_never_offers_a_link_the_gate_refuses():
    """Every role, every module — `general` included now.

    It was exempt when this landed, because the module-scoped view handed the
    kiosk a frozen port of a sidebar that never had a role gate. The exemption
    carried a control asserting it was still needed, and that control is what
    went red once nav started filtering on `may_see`, which is how the
    exemption came out rather than lingering.
    """
    import permissions as P
    offered, refused = 0, []
    for role in ('admin', 'manager', 'staff', 'shareholder', 'general'):
        for module in _NAV_MODULES:
            for section, ep in _nav_links(role, module):
                offered += 1
                if not P.may_see(role, ep):
                    refused.append((role, module, section, ep))
    assert offered > 100, offered
    assert refused == [], refused


def test_every_module_tab_lands_somewhere_its_role_may_open():
    """A tab that bounces is worse than a hidden one.

    The switcher used to link at a hardcoded `first_endpoint`, so opening
    `finance` to `staff` would have handed them a tab pointing at /accounting —
    a page the gate refuses. `visible_modules_for` derives the landing from the
    first link the role may actually see instead.
    """
    import permissions as P
    from access_control import visible_modules_for
    landings = [(role, m['key'], m['landing'])
                for role in ('admin', 'manager', 'staff', 'shareholder')
                for m in visible_modules_for(role)]
    assert len(landings) > 20, landings          # control: the sweep swept
    bad = [x for x in landings if not P.may_see(x[0], x[2])]
    assert bad == [], bad
    # Control: the staff/finance pair — the whole reason this exists — is in
    # the sweep, and lands on /ar rather than the module's canonical endpoint.
    assert ('staff', 'finance', 'accounting.ar_dashboard') in landings


def test_a_module_with_nothing_to_offer_gets_no_tab():
    """Visibility is derived, so a role with no link in a module has no tab."""
    from access_control import visible_modules_for
    keys = {role: {m['key'] for m in visible_modules_for(role)}
            for role in ('admin', 'manager', 'staff', 'shareholder', 'general')}
    assert keys['general'] == set(), keys['general']
    assert 'admin_module' in keys['admin']
    for role in ('manager', 'staff', 'shareholder'):
        assert 'admin_module' not in keys[role], role
    for absent in ('hr', 'cashbook'):
        assert absent not in keys['staff'], absent
    assert {'hr', 'cashbook', 'finance'} <= keys['shareholder']
    # Control: staff is not simply empty — it holds the tabs it should.
    assert {'overview', 'operation', 'trade', 'data', 'finance'} <= keys['staff']
