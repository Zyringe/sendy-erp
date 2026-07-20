"""Snapshots A & B — the de-riskers for the desktop-sidebar refactor.
See projects/pwa-nav-redesign/plan.md (Phase 1 captures, Phase 3 consumes).

base.html's desktop sidebar is about to be rebuilt to render from ONE shared
`NAV` list (nav.py) that also feeds the mobile drawer. That refactor must produce
ZERO visible change on desktop. These two snapshots are what prove it:

  A  test_sidebar_structure_unchanged — for every role × module, the sidebar's
     ordered structure (section headers + link label/href/icon, in order).
  B  test_sidebar_active_link_unchanged — for every sampled endpoint, WHICH link
     is highlighted. This is what proves the fiddly per-link active-state matchers
     (substring / prefix / prefix-with-exclusion / or-list) were ported faithfully.
     A alone cannot catch a mis-ported matcher: the link set is identical either way.

Coverage rule (plan v4): matchers are PER-LINK, so B only proves the links it
samples. `test_snapshot_b_covers_every_sidebar_link` is the meta-test that keeps
B honest — without it B is decorative.

Rendering note: these render base.html directly inside a test_request_context
rather than GETting each page. The sidebar depends only on `request.endpoint` +
session role, so this exercises the real Jinja + the real context processor
without needing 40 routes to return 200 (a /products/<id> GET would 404 on a
tmp DB and render no sidebar at all).

⚠ nav_snapshot.json was captured from base.html at commit 60d4956, BEFORE any
refactor edit — that is the entire point of it. Regenerate
(`~/.virtualenvs/erp/bin/python tests/test_nav_snapshot.py --capture`) ONLY for a
deliberate, reviewed nav change. NEVER to make a red test green.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import json
from html.parser import HTMLParser

import pytest

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), 'nav_snapshot.json')

ROLES = ['admin', 'manager', 'staff', 'shareholder', 'general']

# Extra snapshot-A cases: (role, endpoint) pairs rendered at a REAL landing page.
# MODULE_ENDPOINTS below is keyed by module, so it can only ever probe a module's
# first_endpoint — it renders `general` at `dashboard`, a page the kiosk role can
# never actually reach (it 302s to /m/stock). These pin the pages people really
# sit on, including every page whose module assignment moved on 2026-07-16.
REAL_LANDING_CASES = [
    ('general', 'mobile.stock_search'),      # บอล's actual landing
    ('general', 'me.leave'),
    ('general', 'help_install'),
    ('staff', 'me.leave'),
    ('manager', 'me.payslip_list'),
    ('admin', 'products.product_pricing'),
    ('admin', 'products.product_trade_summary'),
    ('admin', 'products.product_categorize'),
    ('admin', 'products.photos_review'),
    ('admin', 'products.promotion_new'),
    ('admin', 'partners.regions_admin'),
    ('admin', 'partners.customer_bulk_reassign'),
    ('admin', 'marketplace.returns_cancelled'),
    ('admin', 'mobile.sales_trip'),
]

# module key → an endpoint that lands in it (drives `active_module`).
MODULE_ENDPOINTS = {
    'overview':     'dashboard',
    'operation':    'products.product_list',
    'trade':        'sales.trade_dashboard',
    'finance':      'accounting.accounting_summary',
    'hr':           'hr.dashboard',
    'cashbook':     'cashbook.dashboard',
    'data':         'bsn.unified_import',
    'admin_module': 'admin.user_list',
}

# Snapshot-B samples: (role, endpoint, db_routes_enabled).
# ≥1 per sidebar link, and every multi-endpoint matcher is probed on BOTH sides of
# its boundary — that is where a port goes wrong.
B_SAMPLES = [
    # overview
    ('admin', 'dashboard', False),
    ('admin', 'inventory.alerts_view', False),
    ('admin', 'review.index', False),
    ('admin', 'review.scan', False),                      # prefix 'review.'
    # operation — 'product' in ep is a SUBSTRING match, not a prefix
    ('admin', 'products.product_list', False),
    ('admin', 'products.product_detail', False),
    ('admin', 'products.product_new', False),
    ('admin', 'inventory.transaction_history', False),
    ('admin', 'inventory.conversion_list', False),
    ('admin', 'inventory.conversion_history', False),     # prefix 'inventory.conversion_'
    ('admin', 'labels.manage', False),
    ('admin', 'labels.edit', False),                      # prefix 'labels.'
    ('admin', 'labels.print_page', False),                # ...EXCLUDED from labels.manage
    ('staff', 'labels.print_page', False),                # staff sees พิมพ์ป้าย but not จัดการป้าย
    # finance
    ('admin', 'accounting.accounting_summary', False),
    ('admin', 'accounting.cashflow_dashboard', False),
    ('admin', 'accounting.ar_dashboard', False),
    ('admin', 'accounting.ar_followup', False),           # or-list → ar_dashboard
    ('admin', 'accounting.ar_followup_customer', False),  # or-list → ar_dashboard
    ('admin', 'accounting.ap_dashboard', False),
    ('admin', 'commission.commission_dashboard', False),
    ('shareholder', 'accounting.accounting_summary', False),
    # trade
    ('admin', 'sales.trade_dashboard', False),
    ('admin', 'sales.sales_view', False),
    ('admin', 'sales.purchases_view', False),
    ('admin', 'partners.customer_list', False),
    ('admin', 'partners.customer_summary', False),        # or-list → customer_list
    ('admin', 'partners.supplier_list', False),
    ('admin', 'partners.supplier_summary', False),        # or-list → supplier_list
    ('admin', 'call.call_list', False),
    ('admin', 'call.call_card', False),                   # prefix 'call.'
    ('admin', 'ecommerce.ecommerce', False),
    ('admin', 'marketplace.dashboard', False),
    ('admin', 'marketplace.unmapped', False),             # prefix 'marketplace.'
    ('admin', 'marketplace.review', False),               # ...EXCLUDED → its own link
    # ของฉัน (admin/shareholder are leave/pay-exempt → section hidden for them)
    ('manager', 'me.leave', False),
    ('manager', 'me.payslip_list', False),
    ('manager', 'me.payslip_detail', False),              # prefix 'me.payslip'
    ('staff', 'me.leave', False),
    # hr
    ('admin', 'hr.dashboard', False),
    ('admin', 'hr.employee_list', False),
    ('admin', 'hr.employee_detail', False),               # or-list → employee_list
    ('admin', 'hr.leave_list', False),
    ('admin', 'hr.advance_list', False),
    ('admin', 'hr.payroll_list', False),
    ('admin', 'hr.payslip', False),                       # or-list → payroll_list
    # cashbook
    ('admin', 'cashbook.dashboard', False),
    # data
    ('admin', 'bsn.unified_import', False),
    ('admin', 'bsn.unified_import_confirm', False),       # prefix 'bsn.unified_import'
    ('admin', 'bsn.express_dbf_import', False),
    ('admin', 'bsn.mapping', False),
    ('admin', 'bsn.unit_conversions', False),
    ('admin', 'customer_review.normalize_list', False),
    ('admin', 'customer_review.normalize_detail', False),  # prefix 'customer_review.'
    ('admin', 'naming.index', False),
    ('staff', 'bsn.mapping', False),                       # staff: no ตั้งชื่อสินค้า link
    # admin_module (upload/download only exist with the DB-routes toggle armed)
    ('admin', 'admin.user_list', False),
    ('admin', 'admin.user_edit', False),                   # or-list → user_list
    ('admin', 'admin.cashbook_account_list', False),
    ('admin', 'admin.cashbook_account_new', False),        # prefix 'admin.cashbook_account'
    ('admin', 'admin.backups_list', False),
    ('admin', 'admin.backup_restore', False),              # prefix 'admin.backup'
    ('admin', 'admin.upload_db', True),
    ('admin', 'admin.download_db', True),                  # NO matcher → never highlights
    # Pages whose module assignment moved on 2026-07-16 (the _ENDPOINT_MODULE
    # fallback fix). Pre-fix these rendered the ภาพรวม sidebar and highlighted
    # nothing; post-fix they render their own module's sidebar.
    ('admin', 'products.product_pricing', False),
    ('admin', 'products.product_trade_summary', False),
    ('admin', 'products.product_categorize', False),
    ('admin', 'products.photos_review', False),
    ('admin', 'products.promotion_new', False),
    ('admin', 'partners.regions_admin', False),
    ('admin', 'partners.customer_bulk_reassign', False),
    ('admin', 'marketplace.returns_cancelled', False),
    ('general', 'mobile.stock_search', False),
    ('admin', 'mobile.sales_trip', False),
]


class _SidebarParser(HTMLParser):
    """Extracts the ordered contents of <nav class="sidebar-nav">.

    Badge <span>s are skipped: their text is a live DB count and their presence is
    role-gated, so folding them in would make the snapshot non-deterministic.
    Badges are covered separately (see the `badge: {key, roles}` model in nav.py).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._in_nav = False
        self._nav_depth = 0
        self._cur = None
        self._text = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get('class') or '').split()
        if not self._in_nav:
            if tag == 'nav' and 'sidebar-nav' in cls:
                self._in_nav = True
            return
        if tag == 'nav':
            self._nav_depth += 1
            return
        if self._cur is not None:
            if self._skip_depth:
                if tag == 'span':
                    self._skip_depth += 1
                return
            if tag == 'span':
                self._skip_depth = 1
            elif tag == 'i':
                icon = next((c for c in cls if c.startswith('bi-')), None)
                if icon and not self._cur.get('icon'):
                    self._cur['icon'] = icon
            return
        if tag == 'a' and 'sidebar-link' in cls:
            self._cur = {'kind': 'link', 'href': a.get('href', ''),
                         'icon': None, 'active': 'active' in cls}
            self._text = []
        elif tag == 'div' and 'sidebar-section' in cls:
            self._cur = {'kind': 'section'}
            self._text = []

    def handle_endtag(self, tag):
        if not self._in_nav:
            return
        if self._cur is not None:
            if self._skip_depth and tag == 'span':
                self._skip_depth -= 1
                return
            if tag in ('a', 'div'):
                self._cur['label'] = ' '.join(''.join(self._text).split())
                self.items.append(self._cur)
                self._cur = None
                self._text = []
            return
        if tag == 'nav':
            if self._nav_depth:
                self._nav_depth -= 1
            else:
                self._in_nav = False

    def handle_data(self, data):
        if self._cur is not None and not self._skip_depth:
            self._text.append(data)


def _path_and_method(flask_app, endpoint):
    """URL + a method the rule accepts, filling any route args with a dummy.

    The route never executes (we render base.html directly), so a non-existent id
    is fine — the path only has to MATCH the rule so request.endpoint resolves.

    The method matters: several sampled endpoints are POST-only (review.scan,
    admin.user_edit, ...). Matching them with a GET raises MethodNotAllowed, which
    leaves request.endpoint as None → every matcher misses → the sample silently
    proves nothing. render_sidebar asserts the endpoint really resolved.
    """
    from flask import url_for
    rule = next(r for r in flask_app.url_map.iter_rules() if r.endpoint == endpoint)
    with flask_app.test_request_context('/'):
        path = url_for(endpoint, **{arg: 1 for arg in rule.arguments})
    return path, ('GET' if 'GET' in rule.methods else 'POST')


def _path_for(flask_app, endpoint):
    return _path_and_method(flask_app, endpoint)[0]


def render_sidebar(flask_app, role, endpoint, db_routes_enabled=False):
    """Render base.html as `role` on `endpoint`; return the parsed sidebar items."""
    from flask import render_template, request, session
    path, method = _path_and_method(flask_app, endpoint)
    with flask_app.test_request_context(path, method=method):
        assert request.endpoint == endpoint, (
            f"{method} {path} resolved to {request.endpoint!r}, not {endpoint!r} — "
            "the sample would prove nothing")
        session['role'] = role
        session['display_name'] = 'snapshot'
        session['db_routes_enabled'] = db_routes_enabled
        html = render_template('base.html')
    p = _SidebarParser()
    p.feed(html)
    return p.items


def _structure(items):
    """Snapshot-A shape: ordered structure, active-state stripped (that is B's job)."""
    return [{k: v for k, v in it.items() if k != 'active'} for it in items]


def _active_href(items):
    """Snapshot-B shape: the href of the highlighted link (None if none)."""
    hrefs = [it['href'] for it in items if it['kind'] == 'link' and it['active']]
    assert len(hrefs) <= 1, f"more than one sidebar link is active: {hrefs}"
    return hrefs[0] if hrefs else None


def capture(flask_app):
    a = {}
    for role in ROLES:
        for module, endpoint in MODULE_ENDPOINTS.items():
            a[f'{role}|{module}'] = _structure(render_sidebar(flask_app, role, endpoint))
    for role, endpoint in REAL_LANDING_CASES:
        a[f'{role}@{endpoint}'] = _structure(render_sidebar(flask_app, role, endpoint))
    # The DB-routes toggle adds two admin links; pin the armed state too.
    a['admin|admin_module|db_routes'] = _structure(
        render_sidebar(flask_app, 'admin', 'admin.user_list', db_routes_enabled=True))
    b = {}
    for role, endpoint, db_routes in B_SAMPLES:
        b[f'{role}|{endpoint}|{int(db_routes)}'] = _active_href(
            render_sidebar(flask_app, role, endpoint, db_routes_enabled=db_routes))
    return {'A': a, 'B': b}


@pytest.fixture
def flask_app(tmp_db):
    from app import app
    app.config['TESTING'] = True
    return app


@pytest.fixture(scope='module')
def snapshot():
    with open(SNAPSHOT_PATH, encoding='utf-8') as fh:
        return json.load(fh)


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('module', sorted(MODULE_ENDPOINTS))
def test_sidebar_structure_unchanged(flask_app, snapshot, role, module):
    """Snapshot A — no link dropped, added, reordered, relabelled or re-iconed."""
    got = _structure(render_sidebar(flask_app, role, MODULE_ENDPOINTS[module]))
    assert got == snapshot['A'][f'{role}|{module}']


@pytest.mark.parametrize('role,endpoint', REAL_LANDING_CASES)
def test_sidebar_structure_unchanged_at_real_landings(flask_app, snapshot, role, endpoint):
    """Snapshot A, at the pages people actually sit on (not just module landings)."""
    got = _structure(render_sidebar(flask_app, role, endpoint))
    assert got == snapshot['A'][f'{role}@{endpoint}']


def test_sidebar_structure_unchanged_with_db_routes_armed(flask_app, snapshot):
    """Snapshot A — the conditional Upload/Download DB links."""
    got = _structure(render_sidebar(flask_app, 'admin', 'admin.user_list',
                                    db_routes_enabled=True))
    assert got == snapshot['A']['admin|admin_module|db_routes']


@pytest.mark.parametrize('role,endpoint,db_routes', B_SAMPLES)
def test_sidebar_active_link_unchanged(flask_app, snapshot, role, endpoint, db_routes):
    """Snapshot B — the highlighted link is byte-identical to pre-refactor."""
    got = _active_href(render_sidebar(flask_app, role, endpoint,
                                      db_routes_enabled=db_routes))
    assert got == snapshot['B'][f'{role}|{endpoint}|{int(db_routes)}']


def test_snapshot_b_covers_every_sidebar_link(flask_app, snapshot):
    """Meta-test — B must sample every link that can ever highlight.

    Matchers are per-link: an unsampled link can be ported wrong and B stays green.
    This asserts every link reachable in ANY role × module snapshot is the answer to
    at least one B sample, so a mis-ported matcher always has a test that fails.
    `admin.download_db` is the documented exception — it carries no active matcher
    in base.html and therefore can never be the answer to a B sample.
    """
    never_highlights = {_path_for(flask_app, 'admin.download_db')}
    all_links = {it['href'] for snap in snapshot['A'].values() for it in snap
                 if it['kind'] == 'link'}
    covered = {href for href in snapshot['B'].values() if href}
    uncovered = all_links - covered - never_highlights
    assert not uncovered, f"sidebar links never sampled by snapshot B: {sorted(uncovered)}"


if __name__ == '__main__':
    import sys

    if '--capture' not in sys.argv:
        print(__doc__)
        sys.exit("refusing to run: pass --capture to (re)generate the snapshot")
    os.environ.setdefault('SECRET_KEY', 'capture-only-secret')
    os.environ.setdefault('ADMIN_PASSWORD', 'capture-only-admin')
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'inventory_app'))
    from app import app as _app

    _app.config['TESTING'] = True
    data = capture(_app)
    with open(SNAPSHOT_PATH, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    # Re-read independently: a printed count is the writer's own echo, not evidence.
    with open(SNAPSHOT_PATH, encoding='utf-8') as fh:
        back = json.load(fh)
    assert back == data, "snapshot did not round-trip through the file"
    links = sum(1 for snap in back['A'].values() for it in snap if it['kind'] == 'link')
    print(f"captured → {SNAPSHOT_PATH}")
    print(f"  A: {len(back['A'])} role×module snapshots, {links} links total")
    print(f"  B: {len(back['B'])} sampled endpoints, "
          f"{sum(1 for v in back['B'].values() if v)} highlight a link")
