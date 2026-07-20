"""ONE shared NAV list feeding the mobile drawer (Phase 3) and, in a later
phase not yet wired up, the desktop sidebar (base.html still hand-renders its
own <nav class="sidebar-nav"> for now). See projects/pwa-nav-redesign/plan.md.

Every link here was ported 1:1 from base.html's sidebar-nav block (verified by
tests/test_nav.py::test_nav_covers_current_sidebar, and cross-checked against
the pre-refactor snapshots in tests/test_nav_snapshot.py). Do not add, remove,
or relabel a link here without checking base.html first — until the desktop
sidebar is switched over to render from NAV, the two must describe the SAME
real links independently.

Role model
----------
A section's `roles=None` means "every role except 'general'". `general` is the
mobile-only kiosk role (access_control.py::_GENERAL_ALLOWED / `require_login`'s
redirect-everything-else gate) and can only ever reach a handful of endpoints,
so a section must opt it in EXPLICITLY — 'ของฉัน' and 'แอป' do. This is what
keeps `nav_sections('general')` down to just ของฉัน + แอป's help_install
(the plan's explicit requirement — the OLD drawer showed the whole nav to
general, all of it dead-linking back to stock search) without a role check
bolted onto every other section.

A LINK's own `roles` (if given) narrows the section's filter further. `roles_exclude`
subtracts specific roles from an otherwise-open link — used once, for
mobile.sales_trip, which bounces for general even though the แอป section it
lives in is open to general (for help_install).

`badge` is a dict `{'key': <context var name>, 'roles': <optional set>, 'css':
<optional CSS class>}`, never a bare string — one badge (bsn.mapping's
pending_suggestions_count) is role-gated in base.html (`is_manager` there =
admin/manager, excluding shareholder), so a bare-string model would silently
leak that count to a role that shouldn't see it. `css` exists because the
sidebar uses TWO different badge styles (`badge-count` for alert_count/
suspicious_count, `badge bg-danger ms-auto` for pending_suggestions_count) —
the desktop refactor must reproduce both exactly, even though neither
tests/test_nav_snapshot.py's snapshots capture badges at all (their counts are
live DB values, so including them would make the snapshot non-deterministic —
see that file's `_SidebarParser` docstring). Defaults to `'badge-count'` if
omitted.

Active-link matching (`active_link()`, no live consumer yet) mirrors base.html's
existing heterogeneous matchers 1:1:
  - default: exact match on the link's own endpoint (`ep`)
  - `match`: overrides the default with an explicit endpoint set (encodes the
    template's or-lists, e.g. hr.payroll_list's `startswith(...) or == 'hr.payslip'`)
  - `match_prefix`: prefix match(es), OR'd with the above
  - `match_substring`: substring match (only products.product_list uses this —
    base.html's own `'product' in request.endpoint`, deliberately not a prefix)
  - `match_exclude`: endpoints that must NEVER count as a match even though a
    prefix/substring above would otherwise catch them (e.g. marketplace.dashboard
    excludes marketplace.review, which has its own sibling link)
  - `match=[]` (explicit empty list): this link never highlights at all
    (admin.download_db — base.html gives it no active-state clause whatsoever)
"""

ALL_ROLES = frozenset({'admin', 'manager', 'staff', 'shareholder', 'general'})

NAV = [
    {
        'module': 'overview', 'section': 'ภาพรวม', 'roles': None,
        'links': [
            {'ep': 'dashboard', 'label': 'Dashboard', 'icon': 'bi-speedometer2'},
            {'ep': 'inventory.alerts_view', 'label': 'แจ้งเตือน', 'icon': 'bi-exclamation-triangle',
             'badge': {'key': 'alert_count', 'css': 'badge-count'}},
            {'ep': 'review.index', 'label': 'ตรวจบิล', 'icon': 'bi-clipboard-check',
             'badge': {'key': 'suspicious_count', 'css': 'badge-count'}, 'match_prefix': ['review.']},
        ],
    },
    {
        'module': 'operation', 'section': 'คลังสินค้า', 'roles': None,
        'links': [
            {'ep': 'products.product_list', 'label': 'สินค้า', 'icon': 'bi-grid-3x3-gap',
             'match_substring': 'product'},
            {'ep': 'inventory.transaction_history', 'label': 'ประวัติสต็อก', 'icon': 'bi-arrow-left-right'},
            {'ep': 'inventory.conversion_list', 'label': 'แปลงสินค้า', 'icon': 'bi-scissors',
             'match_prefix': ['inventory.conversion_']},
            {'ep': 'labels.manage', 'label': 'จัดการป้ายสินค้า', 'icon': 'bi-tags',
             'roles': {'admin'}, 'match_prefix': ['labels.'], 'match_exclude': ['labels.print_page']},
            {'ep': 'labels.print_page', 'label': 'พิมพ์ป้ายสินค้า', 'icon': 'bi-printer',
             'roles': {'admin', 'manager', 'staff'}},
        ],
    },
    {
        'module': 'trade', 'section': 'การค้า', 'roles': None,
        'links': [
            {'ep': 'sales.trade_dashboard', 'label': 'ภาพรวมการค้า', 'icon': 'bi-bar-chart-line'},
            {'ep': 'sales.sales_view', 'label': 'ยอดขาย', 'icon': 'bi-graph-up-arrow'},
            {'ep': 'sales.purchases_view', 'label': 'ยอดซื้อ', 'icon': 'bi-truck'},
            {'ep': 'partners.customer_list', 'label': 'ลูกค้า', 'icon': 'bi-people',
             'match': ['partners.customer_list', 'partners.customer_summary']},
            {'ep': 'partners.supplier_list', 'label': 'ผู้จำหน่าย', 'icon': 'bi-truck',
             'match': ['partners.supplier_list', 'partners.supplier_summary']},
            {'ep': 'call.call_list', 'label': 'โทรหาลูกค้า', 'icon': 'bi-telephone-outbound',
             'match_prefix': ['call.']},
        ],
    },
    {
        'module': 'trade', 'section': 'ขายออนไลน์', 'roles': None,
        'links': [
            {'ep': 'ecommerce.ecommerce', 'label': 'รายการขายออนไลน์', 'icon': 'bi-bag-heart'},
            {'ep': 'marketplace.dashboard', 'label': 'คำสั่งซื้อ Marketplace', 'icon': 'bi-receipt',
             'match_prefix': ['marketplace.'], 'match_exclude': ['marketplace.review']},
            {'ep': 'marketplace.review', 'label': 'ต้องตรวจการจับคู่ใบกำกับ', 'icon': 'bi-clipboard-check'},
        ],
    },
    {
        'module': 'finance', 'section': 'การเงิน', 'roles': {'admin', 'manager', 'shareholder'},
        'links': [
            {'ep': 'accounting.financial_health', 'label': 'สุขภาพการเงิน', 'icon': 'bi-heart-pulse'},
            {'ep': 'accounting.accounting_summary', 'label': 'สรุปกำไร-ขาดทุน', 'icon': 'bi-calculator'},
            {'ep': 'accounting.cashflow_dashboard', 'label': 'กระแสเงินสด', 'icon': 'bi-cash-stack'},
            {'ep': 'accounting.ar_dashboard', 'label': 'ลูกหนี้ (AR)', 'icon': 'bi-receipt-cutoff',
             'match': ['accounting.ar_dashboard', 'accounting.ar_followup', 'accounting.ar_followup_customer']},
            {'ep': 'accounting.ap_dashboard', 'label': 'เจ้าหนี้ (AP)', 'icon': 'bi-bank'},
            {'ep': 'commission.commission_dashboard', 'label': 'ค่าคอมพนักงานขาย', 'icon': 'bi-cash-coin'},
        ],
    },
    {
        # NOT module-scoped — shows regardless of active_module (role-gated only,
        # same as base.html's own comment above this section). module=None +
        # always=True so a future desktop module='x' render still includes it.
        'module': None, 'section': 'ของฉัน', 'roles': {'staff', 'manager', 'general'}, 'always': True,
        'links': [
            {'ep': 'me.leave', 'label': 'ลาของฉัน', 'icon': 'bi-calendar-x'},
            {'ep': 'me.payslip_list', 'label': 'สลิปเงินเดือน', 'icon': 'bi-cash-stack',
             'match_prefix': ['me.payslip']},
        ],
    },
    {
        # ⚠ roles={'admin','manager'} — NOT the _MODULE_DEFS ('admin','manager',
        # 'shareholder') set. base.html's HR section gates on session.role, not
        # active_module, and EXCLUDES shareholder (the `is_manager` landmine,
        # access_control.py :391 vs :420 — the module switcher lets her INTO 'hr',
        # base.html's section then renders nothing). Reproduced faithfully, not
        # fixed here (Put's call, out of scope — see plan.md).
        'module': 'hr', 'section': 'บุคลากร (HR)', 'roles': {'admin', 'manager'},
        'links': [
            {'ep': 'hr.dashboard', 'label': 'HR Dashboard', 'icon': 'bi-person-badge'},
            {'ep': 'hr.employee_list', 'label': 'พนักงาน', 'icon': 'bi-people',
             'match': ['hr.employee_list', 'hr.employee_new', 'hr.employee_detail', 'hr.employee_entitlements']},
            {'ep': 'hr.leave_list', 'label': 'การลา', 'icon': 'bi-calendar-x',
             'match': ['hr.leave_list', 'hr.leave_new']},
            {'ep': 'hr.advance_list', 'label': 'เบิกล่วงหน้า', 'icon': 'bi-cash'},
            {'ep': 'hr.payroll_list', 'label': 'Payroll', 'icon': 'bi-cash-coin',
             'match': ['hr.payroll_list', 'hr.payslip'], 'match_prefix': ['hr.payroll']},
        ],
    },
    {
        'module': 'cashbook', 'section': 'บัญชีรับ-จ่าย', 'roles': {'admin', 'manager', 'shareholder'},
        'links': [
            {'ep': 'cashbook.dashboard', 'label': 'Dashboard', 'icon': 'bi-speedometer2',
             'url_kwargs': {'vat': 'novat'}},
        ],
    },
    {
        'module': 'data', 'section': 'นำเข้าข้อมูล', 'roles': None,
        'links': [
            {'ep': 'bsn.unified_import', 'label': 'นำเข้าข้อมูล', 'icon': 'bi-box-arrow-in-down',
             'match_prefix': ['bsn.unified_import']},
            {'ep': 'bsn.express_dbf_import', 'label': 'นำเข้า Express (DBF)', 'icon': 'bi-file-earmark-arrow-up'},
            {'ep': 'bsn.mapping', 'label': 'ผูกรหัส BSN', 'icon': 'bi-tags',
             'badge': {'key': 'pending_suggestions_count', 'roles': {'admin', 'manager'},
              'css': 'badge bg-danger ms-auto'}},
            {'ep': 'bsn.unit_conversions', 'label': 'แปลงหน่วย', 'icon': 'bi-arrow-left-right'},
            {'ep': 'customer_review.normalize_list', 'label': 'ตรวจข้อมูลลูกค้า', 'icon': 'bi-person-check',
             'match_prefix': ['customer_review.']},
            {'ep': 'naming.index', 'label': 'ตั้งชื่อสินค้า', 'icon': 'bi-pencil-square',
             'roles': {'admin', 'manager'}, 'match_prefix': ['naming.']},
        ],
    },
    {
        'module': 'admin_module', 'section': 'ระบบ', 'roles': {'admin'},
        'links': [
            {'ep': 'admin.user_list', 'label': 'จัดการผู้ใช้', 'icon': 'bi-people',
             'match': ['admin.user_list', 'admin.user_new', 'admin.user_edit']},
            {'ep': 'admin.cashbook_account_list', 'label': 'จัดการบัญชีเงินสด/ธนาคาร', 'icon': 'bi-bank',
             'match_prefix': ['admin.cashbook_account']},
            {'ep': 'admin.backups_list', 'label': 'สำรอง/กู้คืนข้อมูล', 'icon': 'bi-clock-history',
             'match_prefix': ['admin.backup']},
            # Documented exception: the DB-routes ON/OFF toggle itself is a POST
            # form (admin.toggle_db_routes), not a link — it stays hand-coded in
            # base.html, desktop-only, deliberately NOT in NAV. These two links
            # are conditional on that toggle (session['db_routes_enabled']).
            {'ep': 'admin.upload_db', 'label': 'Upload Database', 'icon': 'bi-cloud-upload',
             'condition': 'db_routes_enabled'},
            {'ep': 'admin.download_db', 'label': 'Download Database', 'icon': 'bi-cloud-download',
             'condition': 'db_routes_enabled', 'match': []},  # base.html: no active-state clause at all
        ],
    },
    {
        # Mobile-only tail — never appears on desktop (desktop:False), only in
        # the drawer. Open to every role including general at the SECTION level;
        # mobile.sales_trip is excluded per-LINK because it bounces for general
        # (not in access_control._GENERAL_ALLOWED); help_install is exempt from
        # require_login, so general genuinely can open it.
        'module': None, 'section': 'แอป', 'roles': ALL_ROLES, 'desktop': False,
        'links': [
            {'ep': 'mobile.sales_trip', 'label': 'แผนทริปขาย (เขต)', 'icon': 'bi-pin-map',
             'roles_exclude': {'general'}},
            {'ep': 'help_install', 'label': 'ติดตั้งแอปบนมือถือ', 'icon': 'bi-phone-fill'},
        ],
    },
]


def _link_visible(link, role):
    roles = link.get('roles')
    if roles is not None and role not in roles:
        return False
    if role in link.get('roles_exclude', ()):
        return False
    return True


def _section_visible(section, role, flat):
    """`flat=True` (the drawer, module=None): a section's `roles=None` means
    "every role EXCEPT 'general'" — the mobile kiosk role opts in explicitly
    (see module docstring). A section that really is for every role including
    general (แอป) lists ALL_ROLES explicitly instead of None.

    `flat=False` (the desktop, module='x'): `roles=None` means literally every
    role, general included. base.html's module-gated sections (ภาพรวม/
    คลังสินค้า/การค้า/ขายออนไลน์/นำเข้าข้อมูล) have NEVER had a role gate — only
    `active_module` gates them. 'general' only ever avoids seeing them in
    practice because `require_login` redirects it away before the page can
    render, not because the sidebar itself hides the section. Verified against
    tests/nav_snapshot.json's `general|operation`/`general|trade`/`general|data`/
    `general|overview` entries, captured pre-refactor: general renders the FULL
    module content there (labels.manage/naming.index etc. still excluded, via
    their own per-LINK `roles`). Reproduced faithfully, not fixed — same
    "pre-existing quirk, out of scope" treatment as the shareholder/HR gap.
    This is a genuine, deliberate asymmetry: the drawer is a NEW, tighter fix
    for general (this project's whole point); the desktop sidebar is a frozen
    port of code that never had this restriction to begin with."""
    roles = section.get('roles')
    if roles is None:
        return (role != 'general') if flat else True
    return role in roles


def nav_sections(role, module=None):
    """Role-filtered NAV sections, links role-filtered too.

    module=None  -> ALL sections, flat, in NAV order (the mobile drawer: one
                    scroll, no active state). Includes desktop:False sections.
    module='x'   -> only sections whose module == 'x', plus any 'always' section
                    (e.g. ของฉัน) — the desktop's module-scoped view. Drops
                    desktop:False sections (they never render on desktop).

    A section left with zero links after role-filtering is dropped entirely
    (e.g. 'ระบบ' vanishes outright for non-admin, rather than showing an empty
    header — matches base.html, which wraps the whole section in the role `if`).

    See `_section_visible`'s docstring for the one deliberate flat-vs-scoped
    asymmetry (general's section-level visibility for `roles=None` sections).
    """
    flat = module is None
    out = []
    for section in NAV:
        if not _section_visible(section, role, flat):
            continue
        if not flat:
            if section.get('desktop') is False:
                continue
            if section.get('module') != module and not section.get('always'):
                continue
        links = [link for link in section['links'] if _link_visible(link, role)]
        if not links:
            continue
        out.append({**section, 'links': links})
    return out


def _link_matches(link, endpoint):
    if endpoint in link.get('match_exclude', ()):
        return False
    match = link['match'] if 'match' in link else (link['ep'],)
    if endpoint in match:
        return True
    for prefix in link.get('match_prefix', ()):
        if endpoint.startswith(prefix):
            return True
    substring = link.get('match_substring')
    if substring and substring in endpoint:
        return True
    return False


def active_link(endpoint, module):
    """(module, ep) of the NAV link that highlights for `endpoint`, or None.

    `module` is REQUIRED and is the request's `active_module`. base.html only ever
    evaluates its matchers INSIDE the active module's section (every section is
    wrapped in `{% if active_module == 'x' %}`), so matching must be scoped the
    same way or it silently crosses modules:

        active_link('naming.product_save', 'data')  -> ('data', 'naming.index')   ✔
        # unscoped, this returned ('operation', 'products.product_list') — because
        # products.product_list matches on the SUBSTRING 'product', and
        # 'naming.product_save' contains it. On a naming.* page (module='data')
        # base.html never renders the คลังสินค้า section at all, so สินค้า cannot
        # be the highlighted link there.

    Same bug family as PR #291 (a parent link lighting up on a sibling child that
    owns its own link) — hence `module` is a required arg, not an optional one: a
    caller cannot accidentally get the global-scan behaviour.

    No live consumer yet: base.html still hand-renders its own active-state checks.
    This exists so the desktop refactor (a later phase) switches to a matcher that
    is already pinned by tests + snapshot B, instead of re-deriving it from scratch.
    """
    if not endpoint:
        return None
    for section in NAV:
        if section.get('desktop') is False:
            continue
        if section.get('module') != module and not section.get('always'):
            continue
        for link in section['links']:
            if _link_matches(link, endpoint):
                return (section.get('module'), link['ep'])
    return None
