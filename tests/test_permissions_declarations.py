"""The gate's acceptance table: what switching `require_login` to the
declaration actually moved.

`require_login` reads `permissions.py` now. `nav.py`, the ten route-local
`_require_*` helpers and the POST allowlists are untouched and stay for the
next PR. What these tests pin is that the declaration is COMPLETE, that it is
MINIMAL, and that the switch moved exactly the twelve cells named in
`EXPECTED_LOSSES` and no others — the list `_today_may_see` below models the
pre-switch gate to produce, which is what makes this a migration record rather
than a tautology.

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

EXPECTED_GAINS = set()

# Switching the gate to the declaration denies these twelve cells. The gate is
# method-agnostic, exactly as the six checks it replaces were, so this table is
# swept over EVERY endpoint in the URL map and not just the GET ones.
EXPECTED_LOSSES = {
    # Refused today by a route-local guard: flash
    # 'ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager' then redirect to '/'. The gate
    # now says the same words from one place.
    ('staff', 'accounting.accounting_summary'),
    ('staff', 'accounting.ar_followup_export'),
    ('staff', 'accounting.cashflow_dashboard'),
    ('staff', 'accounting.financial_health'),
    ('staff', 'accounting.revenue_dashboard'),
    ('staff', 'accounting.revenue_unmapped_drilldown'),
    # Refused today by an inline abort(403) in the route. Still 403.
    ('staff', 'products.product_cost_history'),
    # POST-only, and refused today by the POST allowlist: nobody but admin
    # holds them, so the refusal moves EARLIER, from the POST gate to the
    # access gate. The visible text changes from 'ไม่มีสิทธิ์ดำเนินการนี้'.
    ('staff', 'accounting.ar_followup_log_new'),
    ('staff', 'accounting.ar_followup_log_delete'),
    # `admin.user_delete` is absent from `_ENDPOINT_MODULE`, which is why the
    # old admin_module check missed it and the POST allowlist was what refused
    # it. The `admin` blueprint declaration catches it.
    ('manager', 'admin.user_delete'),
    ('shareholder', 'admin.user_delete'),
    ('staff', 'admin.user_delete'),
}

# The exact refusal each GET-reachable loss must produce: (status, flash) with
# flash None meaning "no message expected". These seven are the ones a browser
# can open; the other five are POST-only.
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
    ('staff', 'accounting.ar_followup_export'):         (302, ACCOUNTING_GUARD_MSG),
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
    """The pre-switch verdict from `require_login`, read independently.

    Route-local guards are deliberately NOT modelled: this is the gate the
    declaration replaces, and the guards layer on top of it either way.
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
    return True


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

    The four Thai strings were on screen before this PR and a refusal that
    dropped them would be a silent UI change dressed up as a refactor. The
    kiosk is the deliberate exception (`permissions.SILENT`), so only rows
    that exclude a DESK role are in scope.
    """
    import permissions as P
    rows = [(name, row) for name, row in
            list(P.AREAS.items()) + list(P.PAGES.items())
            if any(r not in row.see for r in DESK_ROLES)]
    # Count first, or the property below is vacuous. Six AREAS (accounting,
    # admin, cashbook, commission, hr, naming) + three PAGES
    # (products.product_cost_history, admin_exit_simulate, admin_simulate_role).
    assert len(rows) == 9, sorted(n for n, _ in rows)
    mute = [n for n, row in rows if row.deny != P.FORBID and not row.msg]
    assert mute == [], mute
    # Control: the two shapes both really occur, so this is not passing because
    # every row happens to be a 403 (or every row happens to carry a message).
    assert [n for n, row in rows if row.deny == P.FORBID]
    assert [n for n, row in rows if row.msg]


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
    assert len(EXPECTED_LOSSES) == 12
    assert len(EXPECTED_REFUSAL) == 7
    assert set(EXPECTED_REFUSAL) < EXPECTED_LOSSES
    post_only = EXPECTED_LOSSES - set(EXPECTED_REFUSAL)
    assert len(post_only) == 5, sorted(post_only)
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
    assert len(EXPECTED_REFUSAL) == 7
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
