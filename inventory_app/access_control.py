"""Access control: role/permission constants, the request-scoped auth gate,
and the sidebar/mobile-nav context processor.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale.
"""
from flask import session, request, redirect, url_for, flash, abort

import models
import review_rules as rr
from nav import active_link, nav_sections


# ── Auth ──────────────────────────────────────────────────────────────────────
#
# Roles: admin > manager > staff
#   admin   – full access + user management
#   manager – see cost/GP/payments; cannot edit products/users
#   staff   – all import flows (Decision B: staff does data entry; every import
#             snapshots the DB first so a wrong import is recoverable) + read-only
#             views (no cost/GP)
#
# POST whitelist by role
_STAFF_POST_OK = frozenset([
    'login', 'logout',
    'bsn.mapping_save', 'bsn.unit_conversions_save', 'bsn.unit_conversions_edit',
    'bsn.unit_conversions_dismiss',
    # Decision B — staff may import everything; the unified box (/import-data)
    # snapshots the DB before writing (see _snapshot_before_import call sites).
    'bsn.unified_import', 'bsn.unified_import_confirm', 'bsn.express_dbf_upload',
    'marketplace.import_orders', 'marketplace.settlement_import', 'marketplace.upload', 'marketplace.link_iv',
    'products.product_location_save',
    'admin_exit_simulate',
    'inventory.conversion_pair', 'inventory.conversion_run', 'inventory.conversion_delete',
    'products.api_product_barcodes',
    'review.scan',
    'call.call_mark_called',
    'call.call_note',
    'call.call_crm',
    'call.call_contact',
    'call.call_log_delete',
    'customer_review.normalize_confirm',
    'customer_review.normalize_skip',
    'inventory.stock_adjust',
    # Phase 5 self-service leave — any employee may submit/edit/cancel their OWN
    # pending leave. Ownership is enforced inside each route via _my_employee()
    # (employee_id never read from form/URL); this gate only permits the POST to
    # reach the route. The 'general' kiosk role's wiring is added in Task 5.6.
    'me.leave_submit', 'me.leave_edit', 'me.leave_cancel',
])
_MANAGER_POST_OK = _STAFF_POST_OK | frozenset([
    'partners.customer_reassign', 'partners.customer_bulk_reassign',
    # Customer map geocoding (B5) — manager+ feature; staff doesn't need it.
    'partners.customer_geocode',
    'products.product_sku_code_save', 'products.product_regen_sku_code',
    'products.product_packaging_save',
    'bsn.mapping_suggestion_approve',
    'products.photos_review_assign', 'products.photos_review_delete',
    # Acknowledging a billed≠payout discrepancy is a manager+ action (not staff).
    'marketplace.review_amount',
    # Acknowledging a no-IV-exists order on /marketplace/review (mig 135).
    'marketplace.review_dismiss',
    # Master Naming cascade — preview is read-only but POSTed (JSON body);
    # apply mutates product_name in bulk. Manager/admin only.
    'naming.dict_preview', 'naming.dict_apply',
    'naming.product_preview_name', 'naming.product_save',
    # Phase 5 approval workflow — managers can approve/reject pending leave.
    'hr.leave_approve', 'hr.leave_reject',
    # (Phase 7 salary-advance CRUD routes removed in the cashbook Phase 2
    # overhaul — advances are now written via cashbook.new_transaction below,
    # plan.md C5c; /hr/advances is read-only.)
    # Cashbook manual entry (Phase 2) — managers can add/edit/delete manual
    # rows (salary pay-event rows are locked separately, see cashbook.py).
    'cashbook.new_transaction', 'cashbook.txn_edit', 'cashbook.txn_delete',
    # Salary pay-event posting (Phase 4) — manager can mark จ่ายแล้ว/ยกเลิกการจ่าย.
    'hr.payroll_item_pay', 'hr.payroll_item_unpay',
])
# partners.regions_admin POST is intentionally admin-only — gated inline at
# the top of the route. Other admin-only writes use _require_admin().
# admin can POST anything

_GENERAL_POST_OK = frozenset([
    'logout',
    'me.leave_submit', 'me.leave_edit', 'me.leave_cancel',
])
_ROLE_POST_OK = {
    'manager':     _MANAGER_POST_OK,
    'staff':       _STAFF_POST_OK,
    'general':     _GENERAL_POST_OK,
    # shareholder reads everything; write exceptions are cashbook manual
    # entry (Phase 2 — manager + shareholder gain add/edit/delete on
    # cashbook_transactions, salary pay-event rows stay locked) and salary
    # pay-event posting (Phase 4 — she must be able to record real transfers
    # she makes; _require_pay_role in blueprints/hr.py mirrors this set).
    'shareholder': frozenset([
        'logout',
        'cashbook.new_transaction', 'cashbook.txn_edit', 'cashbook.txn_delete',
        'hr.payroll_item_pay', 'hr.payroll_item_unpay',
    ]),
}

# GET allowlist for the 'general' role (PWA stock-lookup kiosk + own leave).
# Everything not in this set → redirect to mobile.stock_search.
_GENERAL_ALLOWED = frozenset([
    'mobile.stock_search', 'mobile.stock_search_api',
    'logout',
    'me.leave', 'me.leave_submit', 'me.leave_edit', 'me.leave_cancel',
    'me.payslip_list', 'me.payslip_detail',   # Phase 6: self-service payslip
])

# ── Roles: the single source of role display (label / badge / description) ─────
# The keys ARE the role enum — every role guard validates against `ROLES`. The
# permission descriptions here mirror the POST whitelists + GET gating above and
# are rendered verbatim on /users (badges + the "สิทธิ์แต่ละ Role" summary) and in
# the topbar badge. Update this table whenever a role's real permissions change.
ROLES = {
    'admin':       {'label': 'ผู้ดูแลระบบ',    'badge': 'bg-danger',
                    'icon': 'bi-shield-fill-check',
                    'desc': 'เต็มสิทธิ์: จัดการผู้ใช้, แก้ไขสินค้า/ข้อมูลทุกอย่าง, เห็นต้นทุน/กำไร, ทุกโมดูล'},
    'manager':     {'label': 'ผู้จัดการ',      'badge': 'bg-warning text-dark',
                    'icon': 'bi-shield-fill',
                    'desc': 'เห็นต้นทุน/กำไร + สถานะชำระหนี้, อนุมัติลา/เบิกเงิน, แก้ชื่อสินค้า, เข้า HR + บัญชี; จัดการผู้ใช้ไม่ได้'},
    'staff':       {'label': 'พนักงานออฟฟิศ',  'badge': 'bg-secondary',
                    'icon': 'bi-person-fill',
                    'desc': 'นำเข้าไฟล์ทุกชนิด + ดูสต็อก/ยอดขาย, ปรับสต็อก, ผูกรหัส; ไม่เห็นต้นทุน, เข้า HR/บัญชีไม่ได้'},
    'shareholder': {'label': 'ผู้ถือหุ้น',     'badge': 'bg-info',
                    'icon': 'bi-eye-fill',
                    'desc': 'ดูได้ทุกหน้า (รวมต้นทุน/กำไร, HR, บัญชี) แต่แก้ไขอะไรไม่ได้เลย'},
    'general':     {'label': 'พนักงานทั่วไป',   'badge': 'bg-success',
                    'icon': 'bi-phone-fill',
                    'desc': 'มือถือเท่านั้น: ค้นหาสต็อก + ลาของตัวเอง + ดูสลิปเงินเดือนตัวเอง'},
}
ROLE_ORDER = ['admin', 'manager', 'staff', 'shareholder', 'general']


# ── Module definitions for sidebar switcher ──────────────────────────────────
# Each entry: key, name_th, icon (bootstrap-icons class), first_endpoint
# (first_endpoint is used to build the switcher navigation target).
# Roles 'admin' and 'manager' can see 'hr'; only 'admin' sees 'admin_module'.
_MODULE_DEFS = [
    {
        'key': 'overview',
        'name': 'ภาพรวม',
        'icon': 'bi-speedometer2',
        'first_endpoint': 'dashboard',
        'roles': None,  # all roles
    },
    {
        'key': 'operation',
        'name': 'คลังสินค้า',
        'icon': 'bi-box-seam',
        'first_endpoint': 'products.product_list',
        'roles': None,
    },
    {
        'key': 'trade',
        'name': 'การค้า',
        'icon': 'bi-bar-chart-line',
        'first_endpoint': 'sales.trade_dashboard',  # staff-safe landing (sales/purchases/customers)
        'roles': None,
    },
    {
        'key': 'finance',
        'name': 'การเงิน',
        'icon': 'bi-cash-coin',
        'first_endpoint': 'accounting.accounting_summary',
        'roles': ('admin', 'manager', 'shareholder'),
    },
    {
        'key': 'hr',
        'name': 'บุคลากร (HR)',
        'icon': 'bi-people',
        'first_endpoint': 'hr.dashboard',
        'roles': ('admin', 'manager', 'shareholder'),
    },
    {
        'key': 'cashbook',
        'name': 'บัญชีรับ-จ่าย',
        'icon': 'bi-journal-text',
        'first_endpoint': 'cashbook.dashboard',
        'roles': ('admin', 'manager', 'shareholder'),
    },
    {
        'key': 'data',
        'name': 'นำเข้าข้อมูล',
        'icon': 'bi-upload',
        'first_endpoint': 'bsn.unified_import',   # /import-data (the consolidated box)
        'roles': None,
    },
    {
        'key': 'admin_module',
        'name': 'ระบบ',
        'icon': 'bi-gear',
        'first_endpoint': 'admin.user_list',
        'roles': ('admin',),
    },
]

# Map each endpoint to the module key it belongs to.
#
# ⚠ Endpoints not listed here fall back to 'overview' — a SILENT default that
# never errors, it just paints the wrong sidebar. Eight real pages sat in that
# hole until 2026-07-16 (/products/<id>/pricing and friends showed the ภาพรวม
# sidebar instead of คลังสินค้า). `_NAV_EXEMPT_ENDPOINTS` below + the hygiene test
# in tests/test_endpoint_module_coverage.py now force every GET-able endpoint to
# be listed in exactly one of the two, so a new page cannot silently regress.
#
# Module key 'mobile' is a SENTINEL: it has no _MODULE_DEFS entry and no sidebar
# section in base.html, so mobile-only PWA pages render no desktop module nav.
# That is deliberate — access_control.py's inject_auth already declares "general
# is mobile-only; desktop sidebar shows nothing", and landing those pages on
# 'overview' was what put three dead links (they all redirect to stock search)
# in the kiosk role's sidebar.
_ENDPOINT_MODULE = {
    # overview
    'dashboard': 'overview',
    'inventory.alerts_view': 'overview',
    'review.index': 'overview',
    'review.scan': 'overview',
    # operation
    'products.product_list': 'operation',
    'products.product_detail': 'operation',
    'products.product_new': 'operation',
    'products.product_edit': 'operation',
    'products.product_pricing': 'operation',
    'products.product_trade_summary': 'operation',
    'products.product_categorize': 'operation',
    'products.photos_review': 'operation',
    'products.promotion_new': 'operation',
    'inventory.stock_adjust': 'operation',
    'inventory.transaction_history': 'operation',
    'inventory.conversion_list': 'operation',
    'inventory.conversion_pair': 'operation',
    'inventory.conversion_run': 'operation',
    'inventory.conversion_delete': 'operation',
    'inventory.conversion_deactivate': 'operation',
    'inventory.conversion_activate': 'operation',
    'inventory.conversion_history': 'operation',
    'labels.manage': 'operation',
    'labels.edit': 'operation',
    'labels.bulk_size': 'operation',
    'labels.company_block': 'operation',
    'labels.print_page': 'operation',
    'labels.search_api': 'operation',
    # finance (formerly 'accounting': accounting.* + commission.* + sales.payment_*)
    'accounting.accounting_summary': 'finance',
    'accounting.cashflow_dashboard': 'finance',
    'accounting.revenue_dashboard': 'finance',
    'accounting.revenue_unmapped_drilldown': 'finance',
    'accounting.ar_followup': 'finance',
    'accounting.ar_followup_customer': 'finance',
    'accounting.ar_followup_log_new': 'finance',
    'accounting.ar_followup_log_delete': 'finance',
    'accounting.ar_followup_export': 'finance',
    'accounting.ar_dashboard': 'finance',
    'accounting.express_ar_dashboard': 'finance',
    'accounting.express_ar_customer': 'finance',
    'accounting.express_ap_dashboard': 'finance',
    'accounting.ap_dashboard': 'finance',
    'commission.commission_dashboard': 'finance',
    'commission.commission_record_payout': 'finance',
    'commission.commission_delete_payout': 'finance',
    'commission.commission_payouts_list': 'finance',
    'commission.commission_drilldown': 'finance',
    'commission.commission_invoice_detail': 'finance',
    'commission.commission_export': 'finance',
    'commission.commission_overrides_list': 'finance',
    'commission.commission_overrides_new': 'finance',
    'commission.commission_overrides_edit': 'finance',
    'commission.commission_overrides_toggle': 'finance',
    'commission.commission_overrides_delete': 'finance',
    'sales.payment_status': 'finance',
    'sales.payment_customers': 'finance',
    # trade (formerly 'accounting': sales trade/purchases + partners + call + ecommerce + marketplace)
    'sales.trade_dashboard': 'trade',
    'sales.sales_view': 'trade',
    'sales.sales_doc': 'trade',
    'sales.purchases_view': 'trade',
    'sales.purchases_doc': 'trade',
    'partners.customer_list': 'trade',
    'partners.customer_summary': 'trade',
    'partners.customer_map': 'trade',
    'partners.customer_bulk_reassign': 'trade',
    'partners.regions_admin': 'trade',
    'partners.supplier_list': 'trade',
    'partners.supplier_summary': 'trade',
    'ecommerce.ecommerce': 'trade',
    'ecommerce.ecommerce_import': 'trade',
    'ecommerce.ecommerce_sku_edit': 'trade',
    'ecommerce.ecommerce_export': 'trade',
    'ecommerce.ecommerce_mapping_export': 'trade',
    'ecommerce.ecommerce_mapping_import': 'trade',
    'ecommerce.ecommerce_listings_import': 'trade',
    'ecommerce.ecommerce_listings_mapping_export': 'trade',
    'ecommerce.ecommerce_listings_mapping_import': 'trade',
    'marketplace.dashboard': 'trade',
    'marketplace.import_orders': 'trade',
    'marketplace.unmapped': 'trade',
    'marketplace.settlement': 'trade',
    'marketplace.settlement_import': 'trade',
    'marketplace.link_iv': 'trade',
    'marketplace.api_iv_candidates': 'trade',
    'marketplace.api_order_detail': 'trade',
    'marketplace.reconciliation': 'trade',
    'marketplace.review_amount': 'trade',
    'marketplace.review': 'trade',
    'marketplace.review_dismiss': 'trade',
    'marketplace.returns_cancelled': 'trade',
    # ── ของฉัน (self-service) — explicitly 'overview', which is exactly what the
    # silent fallback already resolved these to. Listed so the hygiene test can
    # tell "decided" from "forgotten"; mapping them anywhere else would change
    # which sidebar staff/manager see on ลาของฉัน/สลิป pages. The ของฉัน sidebar
    # section is role-gated (not module-gated), so it shows regardless.
    # No bottom-nav slot maps to 'overview' → เพิ่มเติม lights on me.* pages.
    'me.leave': 'overview',
    'me.payslip_list': 'overview',
    'me.payslip_detail': 'overview',
    # ── mobile-only PWA pages → the 'mobile' sentinel (no desktop module nav).
    'mobile.stock_search': 'mobile',
    'mobile.sales_trip': 'mobile',
    'mobile.customer_detail': 'mobile',
    'help_install': 'mobile',
    # hr
    'hr.dashboard': 'hr',
    'hr.employee_list': 'hr',
    'hr.employee_new': 'hr',
    'hr.employee_detail': 'hr',
    'hr.employee_entitlements': 'hr',
    'hr.leave_list': 'hr',
    'hr.leave_new': 'hr',
    'hr.advance_list': 'hr',
    'hr.payroll_list': 'hr',
    'hr.payroll_detail': 'hr',
    'hr.payslip': 'hr',
    # data
    'bsn.unified_import': 'data',
    'bsn.unified_import_confirm': 'data',
    'bsn.express_dbf_import': 'data',
    'bsn.express_dbf_upload': 'data',
    'bsn.mapping': 'data',
    'bsn.mapping_save': 'data',
    'bsn.mapping_suggest': 'data',
    'bsn.mapping_suggestion_approve': 'data',
    'bsn.unit_conversions': 'data',
    'bsn.unit_conversions_save': 'data',
    'bsn.unit_conversions_edit': 'data',
    'bsn.unit_conversions_dismiss': 'data',
    'naming.index': 'data',
    'naming.dict_preview': 'data',
    'naming.dict_apply': 'data',
    'naming.product_preview_name': 'data',
    'naming.product_save': 'data',
    # cashbook
    'cashbook.dashboard':     'cashbook',
    'cashbook.account_ledger': 'cashbook',
    'cashbook.new_transaction': 'cashbook',
    # admin_module
    'admin.user_list': 'admin_module',
    'admin.user_new': 'admin_module',
    'admin.user_edit': 'admin_module',
    'admin.cashbook_account_list': 'admin_module',
    'admin.cashbook_account_new': 'admin_module',
    'admin.cashbook_account_edit': 'admin_module',
    'admin.cashbook_account_delete': 'admin_module',
    'admin.toggle_db_routes': 'admin_module',
    'admin.upload_db': 'admin_module',
    'admin.upload_db_confirm': 'admin_module',
    'admin.download_db': 'admin_module',
    'admin.backups_list': 'admin_module',
    'admin.backup_download': 'admin_module',
    'admin.backup_restore': 'admin_module',
    'admin.backup_delete': 'admin_module',
    'admin_simulate_role': 'admin_module',
    'admin_exit_simulate': 'admin_module',
    # call-card
    'call.call_list': 'trade',
    'call.call_card': 'trade',
    'call.call_mark_called': 'trade',
    # customer contact review
    'customer_review.normalize_list':    'data',
    'customer_review.normalize_detail':  'data',
    'customer_review.normalize_confirm': 'data',
    'customer_review.normalize_skip':    'data',
}

# GET-able endpoints that deliberately carry NO module: they never render a
# sidebar, so the 'overview' fallback is harmless for them. Kept explicit (rather
# than just absent) so tests/test_endpoint_module_coverage.py can tell a decision
# apart from an oversight — being absent from BOTH lists is the bug it catches.
_NAV_EXEMPT_ENDPOINTS = frozenset([
    # JSON APIs
    'cashbook.advance_history', 'cashbook.detail_api',
    'marketplace.api_payout_orders', 'mobile.stock_search_api',
    'products.api_product_barcodes', 'products.api_products_search',
    'products.product_cost_history', 'products.product_parse_name',
    # file / binary responses
    'hr.payroll_export', 'products.serve_catalog_photo', 'serve_sw',
    # legacy redirect stubs kept so old bookmarks don't 404 (both → /import-data)
    'accounting.express_import', 'bsn.import_weekly',
    # infrastructure / pre-auth
    'healthz', 'bootstrap_upload_db', 'login',
])


# ── Mobile bottom-nav slots ───────────────────────────────────────────────────
# Put's decision (2026-07-16, pwa-nav-redesign): the bar slims to 3 module
# slots — หน้าหลัก · สินค้า · การค้า — the same 3 for every office role (admin/
# manager/staff/shareholder); the 4th "slot" is the เพิ่มเติม drawer button,
# rendered separately in _mobile_bottom_nav.html. บุคลากร/การเงิน are no longer
# bottom-nav slots (they're admin/manager(+shareholder)-only anyway, hence the
# now-removed `manager_only` filtering) — they live in the drawer's NAV-derived
# sections instead (nav.py). ลาของฉัน/สลิป also moved into the drawer's ของฉัน
# section: "if they want payslip or leave just go see in เพิ่มเติม tab" (Put).
# ตรวจบิล was never a slot (drawer + dashboard banner instead).
_MOBILE_NAV_SLOTS = [
    {'key': 'home',     'label': 'หน้าหลัก', 'icon': 'bi-house-door',     'endpoint': 'dashboard'},
    {'key': 'products', 'label': 'สินค้า',   'icon': 'bi-box-seam',       'endpoint': 'products.product_list'},
    {'key': 'trade',    'label': 'การค้า',   'icon': 'bi-bar-chart-line', 'endpoint': 'sales.trade_dashboard'},
]


def _mobile_active_slot(endpoint):
    """Which bottom-nav slot key (if any) the current endpoint should highlight.

    หน้าหลัก owns the whole ภาพรวม nav GROUP — Dashboard + แจ้งเตือน + ตรวจบิล
    (Put, 2026-07-16: they are the Dashboard group and should read as หน้าหลัก
    rather than leaving เพิ่มเติม lit). The group is resolved from nav.py's ภาพรวม
    SECTION, so it is defined in exactly one place: add a link to that section and
    the bar follows automatically, matchers (e.g. review.*'s prefix) included.

    ⚠ Keyed on the SECTION, deliberately NOT on the 'overview' MODULE. Phase 1.5
    maps me.leave/me.payslip* to 'overview' too (so the desktop sidebar stays
    unchanged on those pages), so a module-keyed check would wrongly light หน้าหลัก
    on ลาของฉัน/สลิป. Those links live in NAV's ของฉัน section (module=None), so
    resolving by section keeps them out and lets เพิ่มเติม — which actually holds
    them — light instead. Pinned by tests/test_mobile_nav.py."""
    module = _ENDPOINT_MODULE.get(endpoint, 'overview')
    slot = {'operation': 'products', 'trade': 'trade'}.get(module)
    if slot:
        return slot
    # active_link scopes to the module + `always` sections; only a hit inside the
    # ภาพรวม section itself (module 'overview') counts as หน้าหลัก.
    hit = active_link(endpoint, 'overview')
    return 'home' if hit and hit[0] == 'overview' else None


def build_mobile_nav_slots(role, endpoint=''):
    """Role-filtered bottom-nav slots with the active one flagged.

    `general` (บอล's PWA kiosk role) gets its own untouched 3-slot bar — see the
    module docstring section on the 'general' role. Every other role (including
    '' or an unexpected value) gets the identical 3 office slots: none of their
    landings (dashboard / products / trade-dashboard) 403 or redirect for any
    role, so there is nothing left to hide."""
    if role == 'general':
        return [
            {'key': 'stock', 'label': 'สต็อก', 'icon': 'bi-search',
             'endpoint': 'mobile.stock_search', 'active': endpoint == 'mobile.stock_search'},
            {'key': 'my_leave', 'label': 'ลาของฉัน', 'icon': 'bi-calendar-x',
             'endpoint': 'me.leave', 'active': endpoint == 'me.leave'},
            {'key': 'my_payslip', 'label': 'สลิป', 'icon': 'bi-receipt',
             'endpoint': 'me.payslip_list', 'active': endpoint == 'me.payslip_list'},
        ]
    active = _mobile_active_slot(endpoint)
    return [
        {'key': s['key'], 'label': s['label'], 'icon': s['icon'],
         'endpoint': s['endpoint'], 'active': s['key'] == active}
        for s in _MOBILE_NAV_SLOTS
    ]


def inject_auth():
    role = session.get('role', '')
    real_role = session.get('_real_role')
    endpoint = request.endpoint or ''
    active_module = _ENDPOINT_MODULE.get(endpoint, 'overview')
    # Build the list of modules visible to the current role
    visible_modules = []
    for m in _MODULE_DEFS:
        if m['roles'] is None or role in m['roles']:
            visible_modules.append(m)
    if role == 'general':
        visible_modules = []   # general is mobile-only; desktop sidebar shows nothing
    # Desktop sidebar (base.html <nav class="sidebar-nav">) — module-scoped NAV
    # sections + which link (if any) highlights. `active_link` needs the request's
    # actual `active_module`, not the section's own `module` key, because
    # base.html only ever evaluates a matcher INSIDE the module currently
    # rendered (see nav.py::active_link's docstring — an unscoped scan can cross
    # modules, e.g. a naming.* page wrongly lighting up สินค้า via its substring
    # matcher).
    _active_hit = active_link(endpoint, active_module)
    return {
        'is_admin':      role == 'admin',
        'is_manager':    role in ('admin', 'manager'),
        # Cashbook manual-entry write access (Phase 2 design decision — manager
        # AND shareholder gain add/edit/delete; staff stays blocked entirely).
        'can_edit_cashbook': role in ('admin', 'manager', 'shareholder'),
        'current_user':  session.get('display_name', ''),
        'current_role':  role,
        'simulating_as': session.get('display_name') if real_role else None,
        'simulating_as_role': (ROLES.get(role, {}).get('label', role)) if real_role else None,
        'real_role':     real_role,
        'alert_count':   models.count_stock_alerts(),
        'suspicious_count': rr.suspicious_count(since_date=rr.default_since()),
        'db_routes_enabled': session.get('db_routes_enabled', False),
        'pending_suggestions_count': models.count_pending_suggestions(),
        'active_module': active_module,
        'visible_modules': visible_modules,
        'mobile_nav_slots': build_mobile_nav_slots(role, endpoint),
        # Flat, role-filtered NAV sections for the mobile drawer (_mobile_drawer.
        # html) — see nav.py::nav_sections. Precomputed here (once per request,
        # role already resolved) rather than exposing nav_sections() itself to
        # Jinja, so the template just loops over plain dicts.
        'drawer_sections': nav_sections(role),
        # Desktop sidebar — module-scoped (mirrors base.html's per-module `{% if
        # active_module == 'x' %}` blocks) + the endpoint of whichever link
        # should render `active` (None if nothing matches, same as today).
        'sidebar_sections': nav_sections(role, module=active_module),
        'sidebar_active_ep': _active_hit[1] if _active_hit else None,
        'roles': ROLES,
        'role_order': ROLE_ORDER,
    }


def _role_home(role):
    return url_for('mobile.stock_search') if role == 'general' else url_for('dashboard')


def require_login():
    endpoint = request.endpoint
    # Allow static files, login page, healthcheck, and the bootstrap DB
    # upload (which is itself token-gated) without authentication.
    # bsn.express_dbf_upload used to be here too (a token-gated, no-session
    # script upload) — it now goes through the normal login gate like any
    # other Sendy POST, since a logged-in team member uploads the daily
    # Express DBF zip through the website (see blueprints/bsn.py).
    if endpoint in ('login', 'static', 'healthz', 'bootstrap_upload_db',
                    'serve_sw', 'help_install'):
        return
    role = session.get('role', '')
    if not role:
        flash('กรุณาเข้าสู่ระบบก่อน', 'warning')
        return redirect(url_for('login', next=request.url))
    # While impersonating, the impersonation controls must ALWAYS be reachable,
    # whatever the impersonated role's gates do (general redirects everything to
    # stock-search; shareholder may only POST logout). 'exit' returns to the real
    # admin; 'simulate-role' switches to another user (keeping the original admin
    # stashed). Only a current impersonator (_real_role set) can hit either, and
    # the route itself only ever lands on the real admin or a non-admin target → safe.
    if endpoint in ('admin_exit_simulate', 'admin_simulate_role') and session.get('_real_role'):
        return
    # admin_module is admin-only at the module level (defense-in-depth).
    # Exception: an admin who is simulating another role still has _real_role set,
    # so they must be able to reach admin_exit_simulate (and other admin endpoints).
    if _ENDPOINT_MODULE.get(endpoint) == 'admin_module' and role != 'admin' and not session.get('_real_role'):
        abort(403)
    # general: PWA stock-lookup + own leave only — everything else → stock search
    if role == 'general' and endpoint not in _GENERAL_ALLOWED:
        return redirect(url_for('mobile.stock_search'))
    # HR module: staff cannot access any hr.* endpoint (GET or POST)
    if (endpoint or '').startswith('hr.') and role == 'staff':
        flash('ไม่มีสิทธิ์เข้าถึงระบบบุคลากร', 'danger')
        return redirect(url_for('dashboard'))
    # Cashbook module: staff cannot access any cashbook.* endpoint (GET or POST)
    if (endpoint or '').startswith('cashbook.') and role == 'staff':
        flash('ไม่มีสิทธิ์เข้าถึงระบบบัญชีรับ-จ่าย', 'danger')
        return redirect(url_for('dashboard'))
    # Master Naming: staff cannot access any naming.* endpoint (GET or POST) —
    # bulk name cascades are manager/admin work.
    if (endpoint or '').startswith('naming.') and role == 'staff':
        flash('ไม่มีสิทธิ์เข้าถึงระบบตั้งชื่อสินค้า', 'danger')
        return redirect(url_for('dashboard'))
    if request.method != 'POST':
        return
    if role == 'admin':
        return
    if endpoint not in _ROLE_POST_OK.get(role, frozenset()):
        flash('ไม่มีสิทธิ์ดำเนินการนี้', 'danger')
        return redirect(_role_home(role))


def init_access_control(app):
    """Register the before_request auth gate and the auth context processor.

    Must be called with app-scoped registration (not from a blueprint) so
    these fire for every route, including other blueprints' routes.
    """
    app.before_request(require_login)
    app.context_processor(inject_auth)
