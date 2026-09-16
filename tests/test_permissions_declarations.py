"""Guards for `permissions.py` — the declaration layer, before anything reads it.

`permissions.py` ships inert: `require_login`, `nav.py` and the templates are
untouched, so nothing here can change what a user sees. What these tests pin is
that the declaration is COMPLETE, that it is MINIMAL, and that switching the app
over to it would move exactly the twelve cells named in `EXPECTED_LOSSES` and no
others. That list is the next PR's acceptance test, written down before the next
PR exists.

Every sweep here asserts its COUNT before its property and carries a control,
because a role x endpoint matrix is the textbook vacuous test
(`.claude/rules/verification-discipline.md`, "a test that cannot fail").
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

ROLES = ['admin', 'manager', 'staff', 'shareholder', 'general']

# The four hardcoded prefixes in `require_login` that this project exists to
# delete. Repeated here so the oracle below is an INDEPENDENT reading of today's
# behaviour rather than an import of the thing under test.
STAFF_BLOCKED_PREFIXES = ('hr.', 'cashbook.', 'naming.', 'commission.')

# Switching the app to the declaration denies `staff` these twelve GET
# endpoints. Each is ALREADY denied to staff today by a route-local guard, an
# inline 403, or a redirect shim, which
# `test_every_expected_loss_is_already_denied_to_staff` proves over live HTTP.
# So the declaration moves WHERE the refusal is written, not WHETHER it happens.
EXPECTED_LOSSES = {
    ('staff', 'accounting.accounting_summary'),
    ('staff', 'accounting.ar_followup'),
    ('staff', 'accounting.ar_followup_export'),
    ('staff', 'accounting.cashflow_dashboard'),
    ('staff', 'accounting.express_ap_dashboard'),
    ('staff', 'accounting.express_ar_dashboard'),
    ('staff', 'accounting.financial_health'),
    ('staff', 'accounting.revenue_dashboard'),
    ('staff', 'accounting.revenue_unmapped_drilldown'),
    ('staff', 'products.product_cost_history'),
    ('staff', 'sales.payment_customers'),
    ('staff', 'sales.payment_status'),
}


def _app():
    from app import app as a
    a.config['TESTING'] = True
    return a


def _get_endpoints(a):
    return sorted({r.endpoint for r in a.url_map.iter_rules()
                   if r.endpoint != 'static' and 'GET' in (r.methods or set())})


def _today_may_see(role, endpoint):
    """Today's GET verdict from `require_login`, read independently.

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
    endpoints = sorted({r.endpoint for r in a.url_map.iter_rules()
                        if r.endpoint != 'static'})
    assert len(endpoints) >= 200, 'url map looks empty, the sweep would be vacuous'
    empty = [e for e in endpoints if not P.roles_for(e)]
    assert empty == [], empty


def test_public_set_matches_require_logins_own_tuple(tmp_db):
    """`require_login` opens on a literal 6-tuple. Drift there is a real hole."""
    import inspect
    import access_control as ac
    import permissions as P
    src = inspect.getsource(ac.require_login)
    for endpoint in P.PUBLIC:
        assert "'%s'" % endpoint in src, endpoint
    assert "'static'" in src
    assert len(P.PUBLIC) == 6, sorted(P.PUBLIC)


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


# ── the matrix ───────────────────────────────────────────────────────────────

def test_declared_matrix_moves_exactly_the_expected_cells(tmp_db):
    import permissions as P
    a = _app()
    endpoints = _get_endpoints(a)
    assert len(endpoints) >= 120, 'too few GET endpoints, the sweep is vacuous'

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

    assert gains == set(), sorted(gains)
    assert losses == EXPECTED_LOSSES, {
        'unexpected': sorted(losses - EXPECTED_LOSSES),
        'no longer happening': sorted(EXPECTED_LOSSES - losses),
    }


def test_general_sees_only_the_kiosk(tmp_db):
    """`general` is a stock-lookup kiosk. The declaration must not widen it."""
    import access_control as ac
    import permissions as P
    a = _app()
    declared = {e for e in _get_endpoints(a) if P.may_see('general', e)}
    allowed_today = {e for e in _get_endpoints(a)
                     if e in ac._GENERAL_ALLOWED or e in P.PUBLIC}
    assert len(declared) >= 5, declared
    assert declared == allowed_today, {
        'declaration adds': sorted(declared - allowed_today),
        'declaration drops': sorted(allowed_today - declared),
    }


def test_impersonation_escape_is_carved_out_by_name(tmp_db):
    """ADR 0003. These two must stay reachable while impersonating."""
    import inspect
    import access_control as ac
    import permissions as P
    assert P.IMPERSONATION_ESCAPE == {'admin_exit_simulate', 'admin_simulate_role'}
    src = inspect.getsource(ac.require_login)
    for endpoint in P.IMPERSONATION_ESCAPE:
        assert "'%s'" % endpoint in src, endpoint


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


def test_every_expected_loss_is_already_denied_to_staff(tmp_db, tmp_db_conn):
    """The twelve are not a behaviour change, and this is the evidence.

    A status code cannot see a template gate, but it can see a refusal, and a
    refusal is all that is claimed here.
    """
    a = _app()

    def as_staff(path):
        c = a.test_client()
        with c.session_transaction() as s:
            s['user_id'], s['username'], s['role'] = 1, 'probe', 'staff'
        return c.get(path, follow_redirects=False)

    # Positive control: the harness is not simply denying everything, and /ar
    # is precisely the page the declaration KEEPS open to staff.
    assert as_staff('/ar').status_code == 200, (
        'control failed: staff cannot reach /ar, so every refusal below is meaningless')

    assert len(EXPECTED_LOSSES) == 12
    still_open = []
    for role, endpoint in sorted(EXPECTED_LOSSES):
        assert role == 'staff'
        resp = as_staff(_url_for(a, endpoint, tmp_db_conn))
        if resp.status_code == 200:
            still_open.append((endpoint, resp.status_code))
    assert still_open == [], still_open
