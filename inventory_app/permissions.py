"""Who may open which page, declared once.

Sendy answers "may this role use this page" in seven places that share no code:
`_MODULE_DEFS[*].roles` (sidebar only), `nav.py` link roles (sidebar only), a
hardcoded prefix list in `require_login` (URL access, `staff` only), the
`_ROLE_POST_OK` allowlists (POST), ten route-local `_require_*` helpers, dozens
of inline `abort(403)` sites, and inline template flags. Two of them already
contradict each other on the same feature, and nothing notices, because nothing
knows the two are about the same question.

This module is the one place that answers it. It is a pure function over
`(role, endpoint)`: it never reads `session` or `request`. That purity is what
makes the guard affordable, because the whole role x endpoint matrix can be
swept in one test with no application context.

Read the plan in the workspace brain repo at
`projects/sendy-permission-module/sendy-permission-module-plan.md` for the
measurements and the decisions behind every set below.

Two gates, never one (see `CONTEXT.md`, "render gate vs access gate"):

    access gate   may_see()      denies on an unknown endpoint
    render gate   the sidebar    falls back to 'overview', never raises

`require_login` reads this module: it IS the access gate now, and the hardcoded
prefix list it used to carry is gone. `nav.py` and the Jinja context still hold
their own role lists, and migrating those two is what is left.
"""

from typing import NamedTuple

ADMIN, MANAGER, STAFF, SHAREHOLDER, GENERAL = (
    'admin', 'manager', 'staff', 'shareholder', 'general')

# Role sets are named for WHO is in them, never for a module. `naming.*` and
# `hr.*` share a set without sharing a domain, and calling it FINANCE would
# quietly assert otherwise.
ALL_ROLES  = frozenset({ADMIN, MANAGER, STAFF, SHAREHOLDER, GENERAL})
OFFICE     = frozenset({ADMIN, MANAGER, STAFF, SHAREHOLDER})   # every desk role
MANAGEMENT = frozenset({ADMIN, MANAGER, SHAREHOLDER})          # no staff
ADMIN_ONLY = frozenset({ADMIN})
NOBODY     = frozenset()

# Checked before any role logic, so these need no session at all.
PUBLIC = frozenset({
    'login', 'static', 'healthz', 'bootstrap_upload_db', 'serve_sw',
    'help_install',
})

# ADR 0003. While impersonating, the controls that END the impersonation must
# stay reachable whatever the impersonated role's gates say, or an admin who
# simulates `general` is stuck in the stock-search kiosk. `require_login`
# already exempts this pair; the exemption is named here so the invariant
# sweep below can carve it out by name instead of going red on day one.
IMPERSONATION_ESCAPE = frozenset({'admin_exit_simulate', 'admin_simulate_role'})

# How a refusal is delivered. Two shapes, because Sendy already has two and
# collapsing them would be a visible change: a page outside the role's app at
# all answers 403, and a page the role merely may not open sends them home.
FORBID = '403'      # abort(403). The page is not part of this role's app at all.
BOUNCE = 'bounce'   # flash `msg` (when there is one) and redirect to the role's home.


class Access(NamedTuple):
    """Who may see something, and why it was decided that way.

    `why` is not decoration. Every row below was a judgement call somebody has
    to be able to re-litigate, and the seven notations this module replaces
    carried their reasons in commit messages nobody reads at the call site.

    `msg` is the Thai string flashed on refusal, and it is not decoration
    either: these words are already on screen today, written at four different
    call sites, and moving the refusal without carrying them would be a silent
    UI change dressed up as a refactor.
    """
    see: frozenset
    why: str
    deny: str = BOUNCE
    msg: str = ''


# ── The declaration ──────────────────────────────────────────────────────────
# Keyed by blueprint name. App-level endpoints (no dot in the endpoint name)
# live under ROOT. An endpoint whose blueprint is absent from this table is a
# declaration bug, not a permission decision: `undeclared_blueprints()` finds it
# and `init_permissions()` refuses to boot.
ROOT = '(app)'

AREAS = {
    ROOT:              Access(ALL_ROLES,  'login, healthcheck, service worker, the dashboard'),
    'accounting':      Access(MANAGEMENT, 'revenue, P&L, cash flow; the AR pages are excepted below',
                              msg='ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager'),
    'admin':           Access(ADMIN_ONLY, 'user accounts, DB upload/download, backups',
                              deny=FORBID),
    'bsn':             Access(OFFICE,     'Express import and code mapping; staff does the importing'),
    'call':            Access(OFFICE,     'the call card and call log, sales-desk work'),
    'cashbook':        Access(MANAGEMENT, 'the household and shop cash books',
                              msg='ไม่มีสิทธิ์เข้าถึงระบบบัญชีรับ-จ่าย'),
    'commission':      Access(MANAGEMENT, 'salesperson commission; staff must not see what reps earn',
                              msg='ไม่มีสิทธิ์เข้าถึงระบบคอมมิชชั่น'),
    'customer_review': Access(OFFICE,     'the customer-name review queue'),
    'ecommerce':       Access(OFFICE,     'marketplace listing files and platform SKUs'),
    'hr':              Access(MANAGEMENT, 'payroll, leave, advances, employee records',
                              msg='ไม่มีสิทธิ์เข้าถึงระบบบุคลากร'),
    'inventory':       Access(OFFICE,     'stock adjustments, unit conversions, alerts'),
    'labels':          Access(OFFICE,     'label printing; the admin-only management pages keep their own guard'),
    'marketplace':     Access(OFFICE,     'order reconciliation and IV matching'),
    'me':              Access(ALL_ROLES,  'self-service: own leave, own payslip, own password'),
    'mobile':          Access(ALL_ROLES,  'the PWA; general is a stock-lookup kiosk and reaches only the two search pages'),
    'naming':          Access(MANAGEMENT, 'bulk product-name cascades are manager and admin work',
                              msg='ไม่มีสิทธิ์เข้าถึงระบบตั้งชื่อสินค้า'),
    'partners':        Access(OFFICE,     'customers and suppliers'),
    'products':        Access(OFFICE,     'the catalog; cost history is excepted below'),
    'reconcile':       Access(OFFICE,     'the Express-to-Sendy reconciler'),
    'review':          Access(OFFICE,     'the bill-checking queue'),
    'sales':           Access(OFFICE,     'sales and purchase documents; the payment pages are excepted below'),
    'vat_sub':         Access(OFFICE,     'the VAT sub-book; its write routes keep their own manager guard'),
}

PAGES = {
    # ── general is a kiosk, not a desk role ──────────────────────────────────
    # It lands on /m/stock and everything else redirects there. These three sit
    # under an all-roles blueprint but are not part of the kiosk.
    'dashboard':
        Access(OFFICE, 'general is redirected to stock search, it has no desktop dashboard'),
    'mobile.customer_detail':
        Access(OFFICE, 'sales-trip data, outside the kiosk'),
    'mobile.sales_trip':
        Access(OFFICE, 'sales-trip data, outside the kiosk'),

    # ── staff chases debt (Q3, Q9) ───────────────────────────────────────────
    # The only finance pages staff may open. Measured 2026-09-16: staff already
    # reaches the first three today, so declaring them OFFICE keeps a working
    # page working rather than granting anything new.
    'accounting.ar_dashboard':
        Access(OFFICE, 'staff chases receivables; /ar is the workspace it does that in'),
    'accounting.ap_dashboard':
        Access(OFFICE, 'open to staff today and intended to stay open'),
    'accounting.express_ar_customer':
        Access(OFFICE, 'the per-customer AR drill-down staff needs before phoning'),
    'accounting.ar_followup_customer':
        Access(OFFICE, 'the follow-up workspace; staff may log a call, not delete one'),

    # ── cost and margin stay off the staff desk ──────────────────────────────
    'products.product_cost_history':
        Access(MANAGEMENT, 'cost price, guarded today by an inline 403 in the route',
               deny=FORBID),

    # ── app-level endpoints, where the ROOT default is too wide ──────────────
    # No dot in the name, so these resolve to ROOT/ALL_ROLES unless declared —
    # and ALL_ROLES here would be WIDER than the gate they replace.
    'admin_exit_simulate':
        Access(ADMIN_ONLY,
               'ends an impersonation; admin_module (403) today. IMPERSONATION_ESCAPE is what '
               'keeps it reachable mid-simulation, NOT this see-set, so declaring it admin-only '
               'does not trap a simulating admin. It sits in the manager and staff POST sets, so '
               'ROOT here would turn a 403 into actually running the route for a manager who is '
               'not impersonating at all',
               deny=FORBID),
    'admin_simulate_role':
        Access(ADMIN_ONLY,
               'starts or switches an impersonation; admin_module (403) today. Same as above: '
               'IMPERSONATION_ESCAPE is what makes it reachable mid-simulation, not the see-set',
               deny=FORBID),
    'toggle_book':
        Access(OFFICE,
               'the book switch is desk chrome and the kiosk has no book to switch; it is in the '
               'manager, shareholder and staff POST sets but not general\'s, and the kiosk '
               'redirect is what refuses it today'),

    # ── redirect shims, not pages ────────────────────────────────────────────
    # 302 to the consolidated page for EVERY role including admin, so they are
    # neither pages nor refusals. Declared so the completeness sweep has an
    # answer, not because anything renders. Leaving them on their blueprint's
    # MANAGEMENT default would take a working navigation path away from staff
    # and land it on a permission error instead of the page it redirects to.
    'accounting.express_import':
        Access(OFFICE, 'redirect shim to /import-data, which staff uses'),
    'accounting.ar_followup':
        Access(OFFICE, 'redirect shim to /ar?tab=customers, which staff uses'),
    'accounting.express_ar_dashboard':
        Access(OFFICE, 'redirect shim to /ar?tab=overview, which staff uses'),
    'accounting.express_ap_dashboard':
        Access(OFFICE, 'redirect shim to /ap?tab=overview, which staff uses'),
    # `sales.payment_customers` (-> /ar?tab=customers) and `sales.payment_status`
    # (-> /ar?tab=invoices) are the other two shims of the same set. They carry
    # NO row on purpose: `sales` is already OFFICE, so a row would only restate
    # its blueprint default, which is exactly what
    # `test_no_page_row_restates_its_blueprint_default` forbids. Same verdict,
    # one fewer line to drift. `test_redirect_shims_still_redirect_for_staff`
    # covers all six by name.
}

# The kiosk role has no application chrome to read a message in, so it is
# bounced back to the stock search without one, exactly as today.
SILENT = frozenset({GENERAL})


def area_of(endpoint):
    """The blueprint an endpoint belongs to. App-level endpoints are ROOT."""
    return endpoint.split('.')[0] if '.' in endpoint else ROOT


def roles_for(endpoint):
    """The declared see-set for `endpoint`.

    Deny-by-default: an endpoint under an undeclared blueprint resolves to
    NOBODY. This is the access gate, so an unknown answer is a refusal. The
    render gate is the opposite and falls back to 'overview' rather than
    blanking the nav, which is why the two can never share one lookup.
    """
    if endpoint in PUBLIC:
        return ALL_ROLES
    page = PAGES.get(endpoint)
    if page is not None:
        return page.see
    area = AREAS.get(area_of(endpoint))
    return area.see if area is not None else NOBODY


def may_see(role, endpoint):
    """Whether `role` may open `endpoint` with a GET."""
    return role in roles_for(endpoint)


def refusal(role, endpoint):
    """How to refuse `(role, endpoint)`: `(deny-shape, message)`.

    Pure, like `may_see`. It DESCRIBES the refusal; `access_control` is what
    turns it into an abort or a redirect. That split is why this module still
    needs no request context and the whole table stays sweepable in one test.
    """
    row = PAGES.get(endpoint)
    if row is None:
        row = AREAS.get(area_of(endpoint))
    if row is None:
        # An undeclared blueprint. A declaration bug rather than a permission
        # decision, and `init_permissions` refuses to boot on one, so this is
        # only reachable for an app built without that check.
        return (FORBID, '')
    return (row.deny, '' if role in SILENT else row.msg)


def admin_only(endpoint):
    """Whether `endpoint` is declared for admin and nobody else.

    ADR 0003: an admin who is simulating another role keeps the admin-only
    pages, so they can still administer and, above all, get back out. The
    simulated role's OTHER limits do apply, or the simulation would just show
    the admin their own app.
    """
    return roles_for(endpoint) == ADMIN_ONLY


class UndeclaredBlueprint(RuntimeError):
    """A blueprint reached the URL map without a row in AREAS."""


def undeclared_blueprints(app):
    """Blueprints present in `app`'s URL map with no AREAS row.

    Returns a sorted list of (blueprint, example_endpoint). Empty is the only
    healthy answer.
    """
    seen = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC:
            continue
        area = area_of(rule.endpoint)
        if area not in AREAS:
            seen.setdefault(area, rule.endpoint)
    return sorted(seen.items())


def init_permissions(app):
    """Refuse to boot an app carrying a blueprint nobody declared.

    A new page under a declared blueprint costs nothing, which is the point. A
    new BLUEPRINT costs one row, and this is what makes somebody write it. A
    boot failure is a harder gate than CI because it cannot be merged past.
    """
    missing = undeclared_blueprints(app)
    if missing:
        raise UndeclaredBlueprint(
            'blueprint(s) with no row in permissions.AREAS: '
            + ', '.join('%s (e.g. %s)' % (a, e) for a, e in missing))
