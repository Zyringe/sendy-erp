"""Access control: role/permission constants, the request-scoped auth gate,
and the sidebar/mobile-nav context processor.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale.
"""
from flask import session, request, redirect, url_for, flash, abort

import models
import review_rules as rr


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
    'bsn.unified_import', 'bsn.unified_import_confirm',
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
        'key': 'accounting',
        'name': 'การค้า & บัญชี',
        'icon': 'bi-cash-coin',
        'first_endpoint': 'sales.trade_dashboard',  # staff-safe landing (sales/purchases/customers)
        'roles': None,  # module visible to all; only the /accounting cost link+route is admin/manager
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
        'first_endpoint': 'user_list',
        'roles': ('admin',),
    },
]

# Map each endpoint to the module key it belongs to.
# Endpoints not listed here fall back to 'overview'.
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
    'inventory.stock_adjust': 'operation',
    'inventory.transaction_history': 'operation',
    'inventory.conversion_list': 'operation',
    'inventory.conversion_pair': 'operation',
    'inventory.conversion_run': 'operation',
    'inventory.conversion_delete': 'operation',
    'inventory.conversion_deactivate': 'operation',
    'inventory.conversion_activate': 'operation',
    'inventory.conversion_history': 'operation',
    'products.labels_view': 'operation',
    'products.products_walkthrough': 'operation',
    # accounting
    'accounting_summary': 'accounting',
    'cashflow_dashboard': 'accounting',
    'revenue_dashboard': 'accounting',
    'revenue_unmapped_drilldown': 'accounting',
    'ar_followup': 'accounting',
    'ar_followup_customer': 'accounting',
    'ar_followup_log_new': 'accounting',
    'ar_followup_log_delete': 'accounting',
    'ar_followup_export': 'accounting',
    'sales.trade_dashboard': 'accounting',
    'sales.sales_view': 'accounting',
    'sales.sales_doc': 'accounting',
    'sales.purchases_view': 'accounting',
    'sales.purchases_doc': 'accounting',
    'partners.customer_list': 'accounting',
    'partners.customer_summary': 'accounting',
    'partners.customer_map': 'accounting',
    'partners.supplier_list': 'accounting',
    'partners.supplier_summary': 'accounting',
    'sales.payment_status': 'accounting',
    'sales.payment_customers': 'accounting',
    'commission.commission_dashboard': 'accounting',
    'commission.commission_record_payout': 'accounting',
    'commission.commission_delete_payout': 'accounting',
    'commission.commission_payouts_list': 'accounting',
    'commission.commission_drilldown': 'accounting',
    'commission.commission_invoice_detail': 'accounting',
    'commission.commission_export': 'accounting',
    'commission.commission_overrides_list': 'accounting',
    'commission.commission_overrides_new': 'accounting',
    'commission.commission_overrides_edit': 'accounting',
    'commission.commission_overrides_toggle': 'accounting',
    'commission.commission_overrides_delete': 'accounting',
    'ar_dashboard': 'accounting',
    'express_ar_dashboard': 'accounting',
    'express_ar_customer': 'accounting',
    'express_ap_dashboard': 'accounting',
    'ap_dashboard': 'accounting',
    'ecommerce.ecommerce': 'accounting',
    'ecommerce.ecommerce_import': 'accounting',
    'ecommerce.ecommerce_sku_edit': 'accounting',
    'ecommerce.ecommerce_export': 'accounting',
    'ecommerce.ecommerce_mapping_export': 'accounting',
    'ecommerce.ecommerce_mapping_import': 'accounting',
    'ecommerce.ecommerce_listings_import': 'accounting',
    'ecommerce.ecommerce_listings_mapping_export': 'accounting',
    'ecommerce.ecommerce_listings_mapping_import': 'accounting',
    'marketplace.dashboard': 'accounting',
    'marketplace.import_orders': 'accounting',
    'marketplace.unmapped': 'accounting',
    'marketplace.settlement': 'accounting',
    'marketplace.settlement_import': 'accounting',
    'marketplace.link_iv': 'accounting',
    'marketplace.api_iv_candidates': 'accounting',
    'marketplace.api_order_detail': 'accounting',
    'marketplace.reconciliation': 'accounting',
    'marketplace.review_amount': 'accounting',
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
    'bsn.mapping': 'data',
    'bsn.mapping_save': 'data',
    'bsn.mapping_suggest': 'data',
    'bsn.mapping_suggestion_approve': 'data',
    'bsn.unit_conversions': 'data',
    'bsn.unit_conversions_save': 'data',
    'bsn.unit_conversions_edit': 'data',
    'bsn.unit_conversions_dismiss': 'data',
    'supplier_catalogue.supplier_catalogue_list': 'data',
    'supplier_catalogue.supplier_catalogue_purchased': 'data',
    'supplier_catalogue.supplier_catalogue_match': 'data',
    'supplier_catalogue.supplier_catalogue_suggest': 'data',
    'supplier_catalogue.supplier_catalogue_mapping_save': 'data',
    'supplier_catalogue.supplier_catalogue_mapping_delete': 'data',
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
    'user_list': 'admin_module',
    'user_new': 'admin_module',
    'user_edit': 'admin_module',
    'toggle_db_routes': 'admin_module',
    'upload_db': 'admin_module',
    'upload_db_confirm': 'admin_module',
    'download_db': 'admin_module',
    'backups_list': 'admin_module',
    'backup_download': 'admin_module',
    'backup_restore': 'admin_module',
    'backup_delete': 'admin_module',
    'admin_simulate_role': 'admin_module',
    'admin_exit_simulate': 'admin_module',
    # call-card
    'call.call_list': 'accounting',
    'call.call_card': 'accounting',
    'call.call_mark_called': 'accounting',
    # customer contact review
    'customer_review.normalize_list':    'accounting',
    'customer_review.normalize_detail':  'accounting',
    'customer_review.normalize_confirm': 'accounting',
    'customer_review.normalize_skip':    'accounting',
}


# ── Mobile bottom-nav slots (module headers) ─────────────────────────────────
# Put's decision (2026-06-11): the mobile bottom nav shows module headers, not
# individual pages — สินค้า · การค้า · บุคลากร · บัญชี + เพิ่มเติม(drawer, rendered
# separately in the template). Each slot lands on a module's landing route.
# `manager_only` slots are hidden for staff because their landing 403s/redirects
# (hr.dashboard + accounting_summary are admin/manager only) — never show a slot
# a role can't open. ตรวจบิล is NOT here (drawer + dashboard banner instead).
_MOBILE_NAV_SLOTS = [
    {'key': 'products',   'label': 'สินค้า',  'icon': 'bi-box-seam',       'endpoint': 'products.product_list', 'manager_only': False},
    {'key': 'trade',      'label': 'การค้า',  'icon': 'bi-bar-chart-line', 'endpoint': 'sales.trade_dashboard', 'manager_only': False},
    {'key': 'hr',         'label': 'บุคลากร', 'icon': 'bi-people',         'endpoint': 'hr.dashboard',          'manager_only': True},
    {'key': 'accounting', 'label': 'บัญชี',   'icon': 'bi-calculator',     'endpoint': 'accounting_summary',    'manager_only': True},
]

# The 'accounting' module ('การค้า & บัญชี') splits across two bottom-nav slots:
# การค้า (trade) and บัญชี (finance). These endpoints belong to the บัญชี side;
# every other 'accounting'-module endpoint highlights การค้า. Keeps the two
# slots mutually exclusive so the nav never lights up both at once.
_ACCT_FINANCE_ENDPOINTS = frozenset({
    'accounting_summary', 'cashflow_dashboard', 'revenue_dashboard',
    'revenue_unmapped_drilldown', 'ar_followup', 'ar_followup_customer',
    'ar_followup_log_new', 'ar_followup_log_delete', 'ar_followup_export',
})


def _mobile_active_slot(endpoint):
    """Which bottom-nav slot key (if any) the current endpoint should highlight."""
    if endpoint in _ACCT_FINANCE_ENDPOINTS:
        return 'accounting'
    module = _ENDPOINT_MODULE.get(endpoint, 'overview')
    return {'operation': 'products', 'accounting': 'trade', 'hr': 'hr'}.get(module)


def build_mobile_nav_slots(role, endpoint=''):
    """Role-filtered bottom-nav slots with the active one flagged.

    Returns the list of visible slots (each a dict with key/label/icon/endpoint/
    active). Any role that is not admin/manager — including '' or an unexpected
    value — gets only the always-visible slots, so a staff/unknown session can
    never be shown a slot whose landing page would 403."""
    _leave_slot = {'key': 'my_leave', 'label': 'ลาของฉัน', 'icon': 'bi-calendar-x',
                   'endpoint': 'me.leave', 'active': endpoint == 'me.leave'}
    _payslip_slot = {'key': 'my_payslip', 'label': 'สลิป', 'icon': 'bi-receipt',
                     'endpoint': 'me.payslip_list',
                     'active': endpoint == 'me.payslip_list'}
    if role == 'general':
        return [
            {'key': 'stock', 'label': 'สต็อก', 'icon': 'bi-search',
             'endpoint': 'mobile.stock_search', 'active': endpoint == 'mobile.stock_search'},
            _leave_slot,
            _payslip_slot,
        ]
    is_manager = role in ('admin', 'manager', 'shareholder')  # shareholder reads HR + accounting
    active = _mobile_active_slot(endpoint)
    slots = []
    for s in _MOBILE_NAV_SLOTS:
        if s['manager_only'] and not is_manager:
            continue
        slots.append({
            'key': s['key'], 'label': s['label'], 'icon': s['icon'],
            'endpoint': s['endpoint'], 'active': s['key'] == active,
        })
    slots.append(_leave_slot)
    slots.append(_payslip_slot)
    return slots


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
        'roles': ROLES,
        'role_order': ROLE_ORDER,
    }


def _role_home(role):
    return url_for('mobile.stock_search') if role == 'general' else url_for('dashboard')


def require_login():
    endpoint = request.endpoint
    # Allow static files, login page, healthcheck, and the bootstrap DB
    # upload (which is itself token-gated) without authentication.
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
