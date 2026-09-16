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

Nothing reads this module yet. Wiring it into `require_login`, `nav.py` and the
Jinja context is the next PR, which is where the behaviour actually changes.
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


class Access(NamedTuple):
    """Who may see something, and why it was decided that way.

    `why` is not decoration. Every row below was a judgement call somebody has
    to be able to re-litigate, and the seven notations this module replaces
    carried their reasons in commit messages nobody reads at the call site.
    """
    see: frozenset
    why: str


# ── The declaration ──────────────────────────────────────────────────────────
# Keyed by blueprint name. App-level endpoints (no dot in the endpoint name)
# live under ROOT. An endpoint whose blueprint is absent from this table is a
# declaration bug, not a permission decision: `undeclared_blueprints()` finds it
# and `init_permissions()` refuses to boot.
ROOT = '(app)'

AREAS = {
    ROOT:              Access(ALL_ROLES,  'login, healthcheck, service worker, the dashboard'),
    'accounting':      Access(MANAGEMENT, 'revenue, P&L, cash flow; the AR pages are excepted below'),
    'admin':           Access(ADMIN_ONLY, 'user accounts, DB upload/download, backups'),
    'bsn':             Access(OFFICE,     'Express import and code mapping; staff does the importing'),
    'call':            Access(OFFICE,     'the call card and call log, sales-desk work'),
    'cashbook':        Access(MANAGEMENT, 'the household and shop cash books'),
    'commission':      Access(MANAGEMENT, 'salesperson commission; staff must not see what reps earn'),
    'customer_review': Access(OFFICE,     'the customer-name review queue'),
    'ecommerce':       Access(OFFICE,     'marketplace listing files and platform SKUs'),
    'hr':              Access(MANAGEMENT, 'payroll, leave, advances, employee records'),
    'inventory':       Access(OFFICE,     'stock adjustments, unit conversions, alerts'),
    'labels':          Access(OFFICE,     'label printing; the admin-only management pages keep their own guard'),
    'marketplace':     Access(OFFICE,     'order reconciliation and IV matching'),
    'me':              Access(ALL_ROLES,  'self-service: own leave, own payslip, own password'),
    'mobile':          Access(ALL_ROLES,  'the PWA; general is a stock-lookup kiosk and reaches only the two search pages'),
    'naming':          Access(MANAGEMENT, 'bulk product-name cascades are manager and admin work'),
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
        Access(MANAGEMENT, 'cost price, guarded today by an inline 403 in the route'),
    'sales.payment_customers':
        Access(MANAGEMENT, 'who owes what; module finance, not trade'),
    'sales.payment_status':
        Access(MANAGEMENT, 'who owes what; module finance, not trade'),

    # ── redirect shims, not pages ────────────────────────────────────────────
    # 302s to /import-data for every role including admin. Declared so the
    # completeness sweep has an answer, not because anything renders.
    'accounting.express_import':
        Access(OFFICE, 'redirect shim to /import-data, which staff uses'),
}


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
