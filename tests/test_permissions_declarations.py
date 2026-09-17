"""The gate's acceptance table: what moving permission into one module
actually changed.

`require_login` reads `permissions.py`, and so do the ten route-local
`_require_*` helpers now that they are gone. `nav.py` and the POST allowlists
are untouched and stay for the next PR. What these tests pin is that the
declaration is COMPLETE, that it is MINIMAL, and that the move denied exactly
the nine cells named in `EXPECTED_LOSSES` and granted none — `_today_may_see`
below reconstructs what the pre-module gates answered, which is what makes
this a migration record rather than a tautology.

⚠ The table was twelve cells while the oracle modelled `require_login` alone.
Three of those — `accounting.ar_followup_export`, `ar_followup_log_new`,
`ar_followup_log_delete` for `staff` — were already refused inside the route by
`_arf_require_manager` / `_arf_require_admin`, so counting them as movement was
the same over-count that put five redirect shims in the original table. The
oracle layers both gates now and they cancel.

Every sweep here asserts its COUNT before its property and carries a control,
because a role x endpoint matrix is the textbook vacuous test
(`.claude/rules/verification-discipline.md`, "a test that cannot fail").
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

ROLES = ['admin', 'manager', 'staff', 'shareholder', 'general']

# Every desk role. A refusal aimed at one of these has to say something (see
# `test_no_declaration_refuses_a_desk_role_silently`); `general` is the kiosk
# and is bounced wordlessly on purpose, which is what `permissions.SILENT` is.
DESK_ROLES = ('admin', 'manager', 'staff', 'shareholder')

# The four hardcoded prefixes `require_login` used to carry. Repeated here so
# the oracle below is an INDEPENDENT reading of the pre-switch behaviour rather
# than an import of the thing under test.
STAFF_BLOCKED_PREFIXES = ('hr.', 'cashbook.', 'naming.', 'commission.')

# The ten route-local `_require_*` helpers, transcribed from their bodies
# before they were deleted. They fired INSIDE the route, after `require_login`
# had already let the request through, so the oracle below has to layer them or
# every endpoint they guarded reads as a brand-new denial.
#
# ⚠ `hr.employee_entitlements` is deliberately absent. Its `_require_admin()`
# call sat inside the `if request.method == "POST"` branch, so manager and
# shareholder READ that page both before and after. An AST sweep that walked
# the whole function body reported it as admin-only and produced a baseline
# claiming this PR granted two cells it never touched.
_ADMIN = ('admin',)
ROUTE_GUARDS_BEFORE_PR3A = {
    # labels.py::_require_admin / _require_print_role
    'labels.manage': _ADMIN, 'labels.edit': _ADMIN,
    'labels.bulk_size': _ADMIN, 'labels.company_block': _ADMIN,
    'labels.print_page': ('admin', 'manager', 'staff'),
    'labels.search_api': ('admin', 'manager', 'staff'),
    # hr.py::_require_admin
    'hr.employee_new': _ADMIN, 'hr.employee_edit': _ADMIN,
    'hr.employee_salary_add': _ADMIN, 'hr.employee_wht_add': _ADMIN,
    'hr.leave_new': _ADMIN, 'hr.leave_edit': _ADMIN, 'hr.leave_delete': _ADMIN,
    'hr.payroll_generate': _ADMIN, 'hr.payroll_finalize': _ADMIN,
    'hr.payroll_reopen': _ADMIN, 'hr.payroll_item_edit': _ADMIN,
    # hr.py::_require_admin_or_manager
    'hr.leave_approve': ('admin', 'manager'), 'hr.leave_reject': ('admin', 'manager'),
    # hr.py::_require_pay_role — wider than the two above ON PURPOSE, and the
    # same set as the hr blueprint's own default, which is why it needs no row
    # in PAGES and why these two cells do not move.
    'hr.payroll_item_pay': ('admin', 'manager', 'shareholder'),
    'hr.payroll_item_unpay': ('admin', 'manager', 'shareholder'),
    # commission_bp.py::_require_admin — the rule CRUD, not the dashboards
    'commission.commission_overrides_list': _ADMIN,
    'commission.commission_overrides_new': _ADMIN,
    'commission.commission_overrides_edit': _ADMIN,
    'commission.commission_overrides_toggle': _ADMIN,
    'commission.commission_overrides_delete': _ADMIN,
    'commission.commission_reassign_list': _ADMIN,
    'commission.commission_reassign_new': _ADMIN,
    'commission.commission_reassign_edit': _ADMIN,
    'commission.commission_reassign_toggle': _ADMIN,
    'commission.commission_reassign_delete': _ADMIN,
    # vat_sub.py::_require_manager (the four pages a browser can open)
    'vat_sub.index': ('admin', 'manager', 'shareholder'),
    'vat_sub.product_view': ('admin', 'manager', 'shareholder'),
    'vat_sub.planning': ('admin', 'manager', 'shareholder'),
    'vat_sub.group_detail': ('admin', 'manager', 'shareholder'),
    # reconcile.py::_require_manager (all four endpoints of the blueprint)
    'reconcile.index': ('admin', 'manager', 'shareholder'),
    'reconcile.apply': ('admin', 'manager', 'shareholder'),
    'reconcile.dismiss': ('admin', 'manager', 'shareholder'),
    'reconcile.reopen': ('admin', 'manager', 'shareholder'),
    # accounting.py::_arf_require_manager / _arf_require_admin
    'accounting.ar_followup_customer': ('admin', 'manager', 'shareholder'),
    'accounting.ar_followup_export': ('admin', 'manager', 'shareholder'),
    'accounting.ar_followup_log_new': _ADMIN,
    'accounting.ar_followup_log_delete': _ADMIN,
}

EXPECTED_GAINS = set()

# The declaration denies these nine cells that no pre-module gate did. The
# gate is method-agnostic, exactly as the six checks it replaces were, so this
# table is swept over EVERY endpoint in the URL map and not just the GET ones.
EXPECTED_LOSSES = {
    # Refused today by an accounting guard: flash
    # 'ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager' then redirect to '/'. The gate
    # now says the same words from one place.
    ('staff', 'accounting.accounting_summary'),
    ('staff', 'accounting.cashflow_dashboard'),
    ('staff', 'accounting.financial_health'),
    ('staff', 'accounting.revenue_dashboard'),
    ('staff', 'accounting.revenue_unmapped_drilldown'),
    # Refused today by an inline abort(403) in the route. Still 403.
    ('staff', 'products.product_cost_history'),
    # `admin.user_delete` is absent from `_ENDPOINT_MODULE`, which is why the
    # old admin_module check missed it and the POST allowlist was what refused
    # it. The `admin` blueprint declaration catches it. POST-only, so the
    # refusal moves EARLIER, from the POST gate to the access gate, and the
    # visible text changes from 'ไม่มีสิทธิ์ดำเนินการนี้'.
    ('manager', 'admin.user_delete'),
    ('shareholder', 'admin.user_delete'),
    ('staff', 'admin.user_delete'),
}

# The exact refusal each GET-reachable loss must produce: (status, flash) with
# flash None meaning "no message expected". These six are the ones a browser
# can open; the other three are POST-only.
#
# ⚠ Recorded HERE as literals, deliberately. An earlier version read the
# expected shape back from `permissions.refusal` — the very thing under test —
# so the expectation moved with the declaration and pinned nothing: flipping
# `product_cost_history` from FORBID to a bounce left all 15 tests GREEN
# (break-it-once, 2026-09-17). `test_recorded_refusals_match_the_declaration`
# below is what keeps these literals and the module honest about each other.
ACCOUNTING_GUARD_MSG = 'ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager'

EXPECTED_REFUSAL = {
    ('staff', 'accounting.accounting_summary'):         (302, ACCOUNTING_GUARD_MSG),
    ('staff', 'accounting.cashflow_dashboard'):         (302, ACCOUNTING_GUARD_MSG),
    ('staff', 'accounting.financial_health'):           (302, ACCOUNTING_GUARD_MSG),
    ('staff', 'accounting.revenue_dashboard'):          (302, ACCOUNTING_GUARD_MSG),
    ('staff', 'accounting.revenue_unmapped_drilldown'): (302, ACCOUNTING_GUARD_MSG),
    # The route aborts 403 today, with no message, and keeps doing so.
    ('staff', 'products.product_cost_history'):         (403, None),
}

# 302s to the same place for every role including staff, so they are neither
# pages nor refusals. Five of PR 1's twelve were these, admitted by a
# `status_code != 200` assertion a redirect satisfies vacuously — hence
# `test_redirect_shims_still_redirect_for_staff` below.
REDIRECT_SHIMS = {
    'accounting.ar_followup':          '/ar?tab=customers',
    'accounting.express_ar_dashboard': '/ar?tab=overview',
    'accounting.express_ap_dashboard': '/ap?tab=overview',
    'sales.payment_customers':         '/ar?tab=customers',
    'sales.payment_status':            '/ar?tab=invoices',
}


def _app():
    from app import app as a
    a.config['TESTING'] = True
    return a


def _all_endpoints(a):
    """Every endpoint in the URL map. The gate is method-agnostic, so a
    GET-only sweep would be the wrong table."""
    return sorted({r.endpoint for r in a.url_map.iter_rules()
                   if r.endpoint != 'static'})


def _today_may_see(role, endpoint):
    """The verdict every gate that predates `permissions.py` would give.

    Two layers, in the order a request met them: `require_login`'s six
    hardcoded checks, then whichever route-local `_require_*` helper the
    blueprint carried. Modelling only the first would read all 43 guarded
    endpoints as fresh denials; modelling neither would read them as fresh
    grants. Both layers are transcribed independently of the module under test.
    """
    import access_control as ac
    import permissions as P
    if endpoint in P.PUBLIC:
        return True
    if ac._ENDPOINT_MODULE.get(endpoint) == 'admin_module' and role != 'admin':
        return False
    if role == 'general' and endpoint not in ac._GENERAL_ALLOWED:
        return False
    if endpoint.startswith(STAFF_BLOCKED_PREFIXES) and role == 'staff':
        return False
    guard = ROUTE_GUARDS_BEFORE_PR3A.get(endpoint)
    return guard is None or role in guard


# ── completeness ─────────────────────────────────────────────────────────────

def test_every_blueprint_is_declared(tmp_db):
    import permissions as P
    assert P.undeclared_blueprints(_app()) == []


def test_undeclared_blueprint_is_actually_detected(tmp_db):
    """Control for the test above, which would pass on a broken detector.

    Built on a throwaway Flask app, never on Sendy's. `app` is a module-level
    singleton shared by every test in the run, so registering a probe blueprint
    on it leaks into the matrix sweep below.
    """
    import permissions as P
    from flask import Blueprint, Flask
    throwaway = Flask(__name__)
    probe = Blueprint('zzz_undeclared_probe', __name__)

    @probe.route('/zzz-undeclared-probe')
    def _view():
        return 'x'

    throwaway.register_blueprint(probe)
    assert P.undeclared_blueprints(throwaway) == [
        ('zzz_undeclared_probe', 'zzz_undeclared_probe._view')]
    with pytest.raises(P.UndeclaredBlueprint):
        P.init_permissions(throwaway)

    # And the real app is untouched by any of that.
    assert P.undeclared_blueprints(_app()) == []


def test_every_endpoint_resolves_to_a_non_empty_declaration(tmp_db):
    import permissions as P
    a = _app()
    endpoints = _all_endpoints(a)
    assert len(endpoints) >= 200, 'url map looks empty, the sweep would be vacuous'
    empty = [e for e in endpoints if not P.roles_for(e)]
    assert empty == [], empty


# ── minimality ───────────────────────────────────────────────────────────────

def test_no_page_row_restates_its_blueprint_default(tmp_db):
    import permissions as P
    assert len(P.PAGES) >= 5, 'PAGES is empty, the sweep would be vacuous'
    redundant = [e for e, page in P.PAGES.items()
                 if P.AREAS[P.area_of(e)].see == page.see]
    assert redundant == [], redundant


def test_every_page_row_names_a_real_endpoint(tmp_db):
    import permissions as P
    real = {r.endpoint for r in _app().url_map.iter_rules()}
    assert len(real) >= 200
    ghosts = sorted(set(P.PAGES) - real)
    assert ghosts == [], ghosts


def test_every_declaration_carries_a_reason(tmp_db):
    import permissions as P
    blank = ([k for k, v in P.AREAS.items() if not v.why.strip()]
             + [k for k, v in P.PAGES.items() if not v.why.strip()])
    assert len(P.AREAS) + len(P.PAGES) >= 25
    assert blank == [], blank


def test_no_declaration_refuses_a_desk_role_silently(tmp_db):
    """A desk role that is turned away must be told something, or told 403.

    The Thai strings were on screen before the module existed and a refusal
    that dropped them would be a silent UI change dressed up as a refactor.
    The kiosk is the deliberate exception (`permissions.SILENT`), so only desk
    roles are swept.

    Swept through `refusal()` rather than by reading rows, because the shape a
    role actually gets depends on WHICH row answers: the area's, when the role
    is outside the area altogether, and the page's when it is inside. Reading
    `PAGES[e].deny` alone would report a shape nobody receives.
    """
    import permissions as P
    a = _app()
    refused = [(role, e) for e in _all_endpoints(a) for role in DESK_ROLES
               if not P.may_see(role, e)]
    # Count first, or the property below is vacuous.
    assert len(refused) > 100, len(refused)
    mute = [(role, e) for role, e in refused
            if P.refusal(role, e) == (P.BOUNCE, '')]
    assert mute == [], sorted(mute)
    # Control: both shapes really occur across the sweep, so this is not
    # passing because everything happens to be a 403 (or everything a message).
    shapes = {P.refusal(role, e)[0] for role, e in refused}
    assert shapes == {P.FORBID, P.BOUNCE}, shapes


# ── the matrix ───────────────────────────────────────────────────────────────

def test_declared_matrix_moves_exactly_the_expected_cells(tmp_db):
    import permissions as P
    a = _app()
    endpoints = _all_endpoints(a)
    assert len(endpoints) >= 200, 'too few endpoints, the sweep is vacuous'

    gains, losses, agreed = set(), set(), 0
    for endpoint in endpoints:
        for role in ROLES:
            old = _today_may_see(role, endpoint)
            new = P.may_see(role, endpoint)
            if old == new:
                agreed += 1
            elif new:
                gains.add((role, endpoint))
            else:
                losses.add((role, endpoint))

    # Control: the sweep really compared something, and the two sides really
    # can disagree. Without this a broken oracle reads as perfect agreement.
    assert agreed > 500, agreed
    assert agreed + len(gains) + len(losses) == len(endpoints) * len(ROLES)

    assert gains == EXPECTED_GAINS, sorted(gains)
    assert losses == EXPECTED_LOSSES, {
        'unexpected': sorted(losses - EXPECTED_LOSSES),
        'no longer happening': sorted(EXPECTED_LOSSES - losses),
    }


def test_general_sees_only_the_kiosk(tmp_db):
    """`general` is a stock-lookup kiosk. The declaration must not widen it.

    All-methods, like the gate: a POST-only endpoint the kiosk may reach is
    exactly as much of a widening as a page it may open.
    """
    import access_control as ac
    import permissions as P
    a = _app()
    endpoints = _all_endpoints(a)
    assert len(endpoints) >= 200
    declared = {e for e in endpoints if P.may_see('general', e)}
    allowed_today = {e for e in endpoints
                     if e in ac._GENERAL_ALLOWED or e in P.PUBLIC}
    assert len(declared) >= 5, declared
    assert declared == allowed_today, {
        'declaration adds': sorted(declared - allowed_today),
        'declaration drops': sorted(allowed_today - declared),
    }


# ── the gate reads the module, rather than re-typing it ──────────────────────

def _require_login_body():
    """`require_login`'s source with its docstring AND comments removed.

    Species #7 of `.claude/rules/verification-discipline.md`'s "a test that
    cannot fail": an `inspect.getsource` assertion is satisfied by PROSE
    unless the prose goes first. Comments are the live hazard here — the new
    gate's own comment names `admin_module` and the hr. / cashbook. prefixes it
    deleted, so a naive body would "find" every literal this PR removed.
    """
    import inspect
    import access_control as ac
    src = inspect.getsource(ac.require_login)
    doc = ac.require_login.__doc__
    if doc:
        src = src.replace(doc, '')
    # No '#' occurs inside a string literal in this function; if one ever does,
    # this helper needs a real tokenizer and its control below will say so.
    return '\n'.join(line.split('#', 1)[0] for line in src.splitlines())


def test_require_login_reads_the_public_set_by_name(tmp_db):
    """PUBLIC has ONE spelling, so drift between gate and module is unrepresentable.

    This replaces `test_public_set_matches_require_logins_own_tuple`, which
    asserted the literal 6-tuple that this PR deletes.
    """
    import permissions as P
    body = _require_login_body()
    # Control: the strip left the code behind. A helper that returned '' would
    # otherwise satisfy every negative below.
    assert '_role_home(' in body
    assert 'permissions.may_see(' in body

    assert 'permissions.PUBLIC' in body
    # NEGATIVE: the hand-typed tuple is gone. 'login' is excluded on purpose —
    # `url_for('login', ...)` legitimately names it twice.
    for name in ("'healthz'", "'static'", "'bootstrap_upload_db'",
                 "'serve_sw'", "'help_install'"):
        assert name not in body, name
    assert len(P.PUBLIC) == 6, sorted(P.PUBLIC)


def test_a_public_endpoint_really_opens_without_a_session(tmp_db):
    """Behavioural control for the test above, which reads source only.

    Source cannot see whether the gate RUNS. `/healthz` is in PUBLIC and must
    answer without any session at all; a broken PUBLIC lookup would redirect
    it to the login page instead.
    """
    c = _app().test_client()
    resp = c.get('/healthz')
    assert resp.status_code == 200, resp.status_code
    # And the control's control: a NON-public page does get bounced to login,
    # so the 200 above is about PUBLIC and not about the gate being off.
    bounced = c.get('/products', follow_redirects=False)
    assert bounced.status_code == 302, bounced.status_code
    assert '/login' in bounced.headers['Location']


def test_require_login_reads_the_impersonation_escape_by_name(tmp_db):
    """ADR 0003. Replaces `test_impersonation_escape_is_carved_out_by_name`,
    which asserted the literal pair this PR deletes."""
    import permissions as P
    assert P.IMPERSONATION_ESCAPE == {'admin_exit_simulate', 'admin_simulate_role'}
    body = _require_login_body()
    assert '_role_home(' in body                    # control: code survived
    assert 'permissions.IMPERSONATION_ESCAPE' in body
    assert "'admin_exit_simulate'" not in body      # NEGATIVE
    assert "'admin_simulate_role'" not in body      # NEGATIVE


def test_a_simulating_admin_can_still_get_back_out(tmp_db):
    """ADR 0003's invariant, now that this pair declares ADMIN_ONLY + FORBID.

    An admin simulating `general` fails `may_see` on `admin_exit_simulate`, so
    without a carve-out they would get a 403 and be trapped in the kiosk. This
    asserts the OUTCOME, not which carve-out delivers it: the gate has two that
    both cover this endpoint (`IMPERSONATION_ESCAPE`, and the `admin_only`
    exemption), and break-it-once confirmed deleting either one alone leaves
    this green. Deleting both is what turns it red.

    Asserts the session STATE the route changed, not the 302 — a Sendy route
    302s on refusal too.
    """
    import permissions as P
    assert not P.may_see('general', 'admin_exit_simulate')      # precondition
    c = _app().test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'], s['role'] = 7, 'sim', 'general'
        s['_real_role'], s['_real_user_id'], s['_real_username'] = 'admin', 1, 'putty'
    resp = c.post('/admin/exit-simulate')
    assert resp.status_code == 302, resp.status_code
    with c.session_transaction() as s:
        assert s['role'] == 'admin', dict(s)
        assert '_real_role' not in s, dict(s)


# ⚠ The gate's `admin_only` exemption (ADR 0003) is NOT tested, and that is
# deliberate. Measured 2026-09-17 on this branch AND on ed80ca4: all five
# GET-reachable admin endpoints answer 403 to an admin simulating `staff` and
# to plain staff alike, because each route carries its own
# `if session.get('role') != 'admin': abort(403)` reading the SIMULATED role.
# So the exemption is behaviourally inert today — deleting the line leaves
# this file and the role suite green — and a test asserting today's 403 would
# go red exactly when PR 3 moves those route-local guards, which is the
# "guard must survive its own success" trap. It earns a test then, not now.
#
# ⚠ "Inert" is about the exemption LINE, not about impersonation as a whole.
# One refusal shape really does move, in the one direction the acceptance
# matrix cannot see, and the next test pins it.


def test_a_simulating_admin_is_refused_an_admin_page_like_a_plain_user(tmp_db):
    """The one refusal SHAPE this PR moves that `EXPECTED_LOSSES` cannot see.

    That matrix is keyed on `(role, endpoint)` and has no impersonation
    dimension, so this was invisible to it. Measured against ed80ca4: an admin
    simulating `general` who opened an admin URL got a 302 to /m/stock, while a
    plain `general` user got 403 on the same URL. The 302 was an artefact of
    check ORDER rather than a decision — the base exempted an impersonator from
    the admin_module abort, and the kiosk redirect on the very next line then
    caught them. One gate cannot keep both, and writing the ordering artefact
    down as policy is the worse of the two options, so the two now agree.

    Nothing is granted in either direction, and the way out of a simulation is
    unaffected (`test_a_simulating_admin_can_still_get_back_out`).
    """
    a = _app()
    plain = _client(a, 'general').get('/users', follow_redirects=False)
    assert plain.status_code == 403, plain.status_code

    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'], s['role'] = 7, 'sim', 'general'
        s['_real_role'], s['_real_user_id'], s['_real_username'] = 'admin', 1, 'putty'
    simulating = c.get('/users', follow_redirects=False)
    assert simulating.status_code == 403, simulating.status_code


def test_the_kiosk_is_refused_without_a_message(tmp_db):
    """`permissions.SILENT` exists so the kiosk is bounced wordlessly, as before.

    Nothing else pinned this. The matrix compares `may_see` only, so dropping
    the SILENT clause would start flashing desk-role wording at a stock-lookup
    kiosk with every other test in this file still green (break-it-once,
    2026-09-17).
    """
    import permissions as P
    a = _app()
    path = '/hr/advances'                     # a MANAGEMENT area that carries a msg
    assert P.AREAS['hr'].msg, 'precondition: there must BE a message to suppress'

    kiosk = _client(a, 'general')
    bounced = kiosk.get(path, follow_redirects=False)
    assert bounced.status_code == 302, bounced.status_code
    assert bounced.headers['Location'].endswith('/m/stock'), bounced.headers['Location']
    assert _flashes(kiosk) == [], _flashes(kiosk)

    # Control: the message really is reachable on this very path, so this is
    # not passing because the harness never sees a flash at all.
    desk = _client(a, 'staff')
    assert desk.get(path, follow_redirects=False).status_code == 302
    assert P.AREAS['hr'].msg in _flashes(desk), _flashes(desk)


def test_a_404_is_a_404_for_a_desk_role_and_home_for_the_kiosk(tmp_db):
    """A 404 carries no endpoint, so there is nothing for the gate to look up.

    `roles_for(None)` would raise on `None.split('.')`, so the branch is not
    cosmetic: without it every 404 becomes a 500. `general` has no chrome to
    render a 404 into and goes home, which is what it did before this PR.
    """
    a = _app()
    missing = '/zzz-no-such-route-exists'
    assert _client(a, 'staff').get(missing).status_code == 404

    bounced = _client(a, 'general').get(missing, follow_redirects=False)
    assert bounced.status_code == 302, bounced.status_code
    assert bounced.headers['Location'].endswith('/m/stock'), bounced.headers['Location']


# ── the live proof behind EXPECTED_LOSSES ────────────────────────────────────

def _url_for(a, endpoint, conn):
    rule = next((r for r in a.url_map.iter_rules() if r.endpoint == endpoint), None)
    assert rule is not None, endpoint
    if not rule.arguments:
        return rule.rule
    if endpoint == 'products.product_cost_history':
        row = conn.execute('SELECT id FROM products LIMIT 1').fetchone()
        assert row is not None, 'no products in the test DB'
        from flask import url_for
        with a.test_request_context():
            return url_for(endpoint, product_id=row[0])
    pytest.fail('no argument recipe for %s (%s)' % (endpoint, rule.rule))


def _client(a, role):
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'], s['role'] = 1, 'probe', role
    return c


def _flashes(c):
    with c.session_transaction() as s:
        return [m for _cat, m in s.get('_flashes', [])]


def test_every_expected_loss_is_a_real_page_admin_can_open(tmp_db, tmp_db_conn):
    """The twelve are not a behaviour change, and this is the evidence.

    PR 1 asserted only `status_code != 200` for the refused role, which a
    redirect shim satisfies VACUOUSLY — that is how five shims got into the
    table. So the load-bearing assertion here is the other one: an ALLOWED
    role must get a real 200, which no shim can do. The refused role is then
    checked in its RECORDED shape, not merely "not 200".
    """
    import permissions as P
    a = _app()

    # Counts before the loops, so a row silently leaving the table cannot make
    # this pass by iterating over less.
    assert len(EXPECTED_LOSSES) == 9
    assert len(EXPECTED_REFUSAL) == 6
    assert set(EXPECTED_REFUSAL) < EXPECTED_LOSSES
    post_only = EXPECTED_LOSSES - set(EXPECTED_REFUSAL)
    assert len(post_only) == 3, sorted(post_only)
    assert all('GET' not in (r.methods or set())
               for r in a.url_map.iter_rules()
               if r.endpoint in {e for _role, e in post_only}), sorted(post_only)

    # Positive control: the harness is not simply denying everything, and /ar
    # is precisely the page the declaration KEEPS open to staff.
    assert _client(a, 'staff').get('/ar').status_code == 200, (
        'control failed: staff cannot reach /ar, so every refusal below is meaningless')

    not_a_page, wrong_shape = [], []
    for (role, endpoint), (want_status, want_msg) in sorted(EXPECTED_REFUSAL.items()):
        path = _url_for(a, endpoint, tmp_db_conn)

        # The assertion PR 1 was missing: a shim cannot answer 200.
        allowed = _client(a, 'admin').get(path, follow_redirects=False)
        if allowed.status_code != 200:
            not_a_page.append((endpoint, allowed.status_code))
            continue

        c = _client(a, role)
        refused = c.get(path, follow_redirects=False)
        if refused.status_code != want_status:
            wrong_shape.append((endpoint, 'want %d' % want_status, refused.status_code))
        elif want_msg is not None and want_msg not in _flashes(c):
            # The words, not just the redirect: these strings were on screen
            # before this PR and carrying them is half its point.
            wrong_shape.append((endpoint, 'flash missing', want_msg, _flashes(c)))
        elif want_msg is None and _flashes(c):
            wrong_shape.append((endpoint, 'unexpected flash', _flashes(c)))
    assert not_a_page == [], not_a_page
    assert wrong_shape == [], wrong_shape


def test_recorded_refusals_match_the_declaration(tmp_db):
    """Ties the literals above to `permissions.refusal`, in both directions.

    The live test asserts the literals so it stays independent of the module.
    That independence is only safe if somebody notices when the two disagree,
    which is this test: it goes red if a declaration's `deny`/`msg` is edited
    without the recorded shape following (break-it-once M2).
    """
    import permissions as P
    shape = {P.FORBID: 403, P.BOUNCE: 302}
    assert len(EXPECTED_REFUSAL) == 6
    drift = []
    for (role, endpoint), (want_status, want_msg) in sorted(EXPECTED_REFUSAL.items()):
        deny, msg = P.refusal(role, endpoint)
        if (shape[deny], msg or None) != (want_status, want_msg):
            drift.append((endpoint, (shape[deny], msg or None), (want_status, want_msg)))
    assert drift == [], drift
    # Control: both shapes really occur in this table, so neither branch of the
    # comparison above is untaken.
    assert {s for s, _m in EXPECTED_REFUSAL.values()} == {302, 403}


def test_redirect_shims_still_redirect_for_staff(tmp_db):
    """The regression this PR would otherwise have shipped.

    Each of these 302s to the same consolidated page for every role. Left on
    their blueprint's MANAGEMENT default, staff would have got a permission
    error where a working navigation path used to be — so the test is that
    staff's Location EQUALS admin's, not merely that staff is not refused.
    """
    a = _app()
    assert len(REDIRECT_SHIMS) == 5
    admin, staff = _client(a, 'admin'), _client(a, 'staff')
    diverged = []
    for endpoint, want_target in sorted(REDIRECT_SHIMS.items()):
        path = next(r.rule for r in a.url_map.iter_rules() if r.endpoint == endpoint)
        ar, sr = admin.get(path), staff.get(path)
        if (ar.status_code, sr.status_code) != (302, 302):
            diverged.append((endpoint, ar.status_code, sr.status_code))
        elif ar.headers['Location'] != sr.headers['Location']:
            diverged.append((endpoint, ar.headers['Location'], sr.headers['Location']))
        elif not ar.headers['Location'].endswith(want_target):
            diverged.append((endpoint, 'target', ar.headers['Location']))
    assert diverged == [], diverged


# ── the ten route-local guards, after the move ───────────────────────────────

def test_no_route_local_permission_helper_survives(tmp_db):
    """The ten helpers are gone, and no eleventh grew back.

    Source-level, so it catches a helper that exists but is never called — the
    shape that would quietly become the source of truth again.
    """
    import pathlib
    import re
    bp = pathlib.Path(__file__).resolve().parents[1] / 'inventory_app' / 'blueprints'
    files = sorted(bp.glob('*.py'))
    assert len(files) >= 15, files          # control: the sweep found the tree
    pattern = re.compile(r'^def _[a-z_]*require[a-z_]*\(', re.M)
    offenders = [(f.name, pattern.findall(f.read_text())) for f in files]
    assert [(n, hits) for n, hits in offenders if hits] == []
    # Control: the pattern can match. It is the one the plan prescribes after
    # a plain `^def _require_` sweep missed `_arf_require_manager` entirely.
    assert pattern.findall('def _arf_require_manager():\n')


# (role, url, status, flash) for the refusals the ten helpers used to deliver.
# The pairs matter more than the rows: each page appears twice, once for a role
# INSIDE the blueprint (which gets the page row's 403) and once for a role
# OUTSIDE it (which gets the area's own words).
MOVED_REFUSALS = [
    # labels — area OFFICE, four admin pages, two OPERATORS pages
    ('manager',     '/labels/manage',        403, None),
    ('staff',       '/labels/manage',        403, None),
    ('shareholder', '/labels/print',         403, None),
    ('general',     '/labels/manage',        302, None),
    # hr — area MANAGEMENT with its own words, eleven admin pages
    ('manager',     '/hr/employees/new',     403, None),
    ('shareholder', '/hr/employees/new',     403, None),
    ('staff',       '/hr/employees/new',     302, 'ไม่มีสิทธิ์เข้าถึงระบบบุคลากร'),
    # commission — same shape
    ('manager',     '/commission/overrides', 403, None),
    ('staff',       '/commission/overrides', 302, 'ไม่มีสิทธิ์เข้าถึงระบบคอมมิชชั่น'),
    # vat_sub — area OFFICE, so staff meets the PAGE row and its words
    ('staff',       '/vat-sub',              302, 'ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager'),
    # reconcile — the whole blueprint is MANAGEMENT, so staff meets the AREA
    ('staff',       '/reconcile',            302, 'ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager'),
]


def test_the_moved_guards_refuse_exactly_as_they_did_in_the_route(tmp_db):
    """Every refusal `_require_*` used to raise, now raised by the gate.

    Recorded as literals for the same reason `EXPECTED_REFUSAL` is: reading
    the shape back out of `permissions.refusal` would pin nothing.
    """
    a = _app()
    assert len(MOVED_REFUSALS) == 11
    # Positive control: an allowed role gets a real page on the same URLs, so
    # a blanket failure cannot read as a pass.
    admin = _client(a, 'admin')
    for url in sorted({u for _r, u, _s, _m in MOVED_REFUSALS}):
        assert admin.get(url).status_code == 200, url

    wrong = []
    for role, url, want_status, want_msg in MOVED_REFUSALS:
        c = _client(a, role)
        resp = c.get(url, follow_redirects=False)
        flashes = _flashes(c)
        if resp.status_code != want_status:
            wrong.append((role, url, 'want %d' % want_status, resp.status_code))
        elif want_msg is not None and want_msg not in flashes:
            wrong.append((role, url, 'flash missing', want_msg, flashes))
        elif want_msg is None and flashes:
            wrong.append((role, url, 'unexpected flash', flashes))
    assert wrong == [], wrong


def test_the_kiosk_is_bounced_home_by_a_403_area_too(tmp_db):
    """`general` never meets a 403 raised on a page INSIDE an area it may enter.

    The area answers first for a role the area excludes, which is what keeps
    the kiosk — which has no chrome to render an error page into — on the
    bounce it has always had. Reading the page row first turned 37 of these
    into 403s (measured 2026-09-17).
    """
    kiosk = _client(_app(), 'general')
    resp = kiosk.get('/labels/manage', follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    assert resp.headers['Location'].endswith('/m/stock'), resp.headers['Location']
    assert _flashes(kiosk) == [], _flashes(kiosk)


def test_employee_entitlements_reads_for_manager_and_writes_for_admin_only(tmp_db, tmp_db_conn):
    """The one guard that sat inside a POST branch, so the page has two answers.

    `hr.employee_entitlements` gets NO declaration: an admin-only see-set would
    take the page away from manager and shareholder, who read it today. Its
    POST stays admin-only by omission from the POST allowlists, which is the
    mechanism 55 other write endpoints already rely on, and this is what says
    so out loud.
    """
    import access_control as ac
    a = _app()
    row = tmp_db_conn.execute('SELECT id FROM employees ORDER BY id LIMIT 1').fetchone()
    assert row is not None, 'no employees in the test DB, the sweep would be vacuous'
    url = '/hr/employees/%d/entitlements' % row[0]

    for role in ('admin', 'manager', 'shareholder'):
        assert _client(a, role).get(url).status_code == 200, role
    assert _client(a, 'staff').get(url, follow_redirects=False).status_code == 302

    assert 'hr.employee_entitlements' not in ac._MANAGER_POST_OK
    for role in ('manager', 'shareholder'):
        c = _client(a, role)
        posted = c.post(url, data={'year': '2026'}, follow_redirects=False)
        assert posted.status_code == 302, (role, posted.status_code)
        assert 'ไม่มีสิทธิ์ดำเนินการนี้' in _flashes(c), (role, _flashes(c))
