"""#590 PR 1: the one channel that names who is writing.

Design: docs/specs/2026-09-19-590-cost-audit-actor-design.md §A1 (branch
feat/590-cost-audit-actor). PR 1 adds no triggers and refuses nothing; it only
makes `sendy_actor(field)` answer correctly on every connection the app opens.

Every assertion resolves THROUGH THE SQL FUNCTION on a real connection, because
that is the path PR 2's triggers will take. Reading `actor.current()` in Python
would test a second resolver that no trigger ever calls.
"""
import os
import sqlite3
import sys
import time

import pytest

import actor
import database


def _resolved(conn):
    return tuple(conn.execute(
        "SELECT sendy_actor('who'), sendy_actor('source'), sendy_actor('reason')"
    ).fetchone())


@pytest.fixture
def no_default():
    """Switch the test default off, so 'nothing declared' is observable."""
    actor.set_fallback(None)
    yield


# ── the test default ─────────────────────────────────────────────────────────

def test_a_plain_test_resolves_the_lowest_precedence_default(tmp_db, request):
    conn = database.get_connection()
    try:
        assert _resolved(conn) == ('pytest', 'test', f'test:{request.node.nodeid}')
    finally:
        conn.close()


def test_with_nothing_declared_every_field_is_null(tmp_db, no_default):
    conn = database.get_connection()
    try:
        assert _resolved(conn) == (None, None, None)
    finally:
        conn.close()


def test_the_default_refuses_to_be_set_outside_pytest(monkeypatch):
    monkeypatch.delitem(sys.modules, 'pytest')
    with pytest.raises(RuntimeError):
        actor.set_fallback(actor.Actor(who='someone'))


# ── nested scopes ────────────────────────────────────────────────────────────

def test_a_root_scope_replaces_the_default_and_a_partial_scope_composes(tmp_db):
    conn = database.get_connection()
    try:
        with actor.acting_as(kind='script', who='put', source='manual', detail='x.py: fix pid 1'):
            assert _resolved(conn) == ('put', 'manual', 'script:x.py: fix pid 1')
            with actor.acting_as(detail='wacc:recalculate'):
                # who comes from the outer scope, what comes from the engine
                assert _resolved(conn) == (
                    'put', 'manual', 'script:x.py: fix pid 1 > wacc:recalculate')
            assert _resolved(conn) == ('put', 'manual', 'script:x.py: fix pid 1')
        assert _resolved(conn)[0] == 'pytest'
    finally:
        conn.close()


def test_a_scope_is_reset_even_when_its_body_raises(tmp_db):
    conn = database.get_connection()
    try:
        with pytest.raises(ZeroDivisionError):
            with actor.acting_as(kind='script', who='put', source='manual', detail='x'):
                1 / 0
        assert _resolved(conn)[0] == 'pytest'
    finally:
        conn.close()


def test_a_partial_scope_alone_names_nobody(tmp_db, no_default):
    conn = database.get_connection()
    try:
        with actor.acting_as(detail='wacc:recalculate'):
            assert _resolved(conn)[0] is None
    finally:
        conn.close()


def test_a_root_scope_must_name_who(tmp_db):
    with pytest.raises(ValueError):
        with actor.acting_as(kind='script', who='  ', source='manual', detail='x'):
            pass


# ── requests ─────────────────────────────────────────────────────────────────

@pytest.fixture
def capture(tmp_db, monkeypatch):
    """Drive a REAL request through the app and record what `sendy_actor`
    resolves inside it, on a real `get_connection()`.

    `/products/<id>/cost-history` calls models.get_cost_history; the stand-in
    records the actor and returns no history, so the page still answers.
    """
    import models
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    seen = []

    def _record(product_id):
        c = database.get_connection()
        try:
            seen.append(_resolved(c))
        finally:
            c.close()
        return []

    monkeypatch.setattr(models, 'get_cost_history', _record)

    def _get(**sess):
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s.update(sess)
        resp = client.get('/products/1/cost-history')
        assert resp.status_code == 200, resp.status_code
        return seen[-1]
    return _get


def test_a_request_resolves_the_session_user_and_the_endpoint(capture):
    got = capture(user_id=1, username='admin', role='admin')
    assert got == ('admin', 'manual', 'ui:products.product_cost_history')


def test_while_impersonating_both_people_are_named(capture):
    got = capture(user_id=5, username='ballwtp1', role='manager',
                  _real_role='admin', _real_username='admin', _real_user_id=1)
    assert got[0] == 'ballwtp1 via admin'


def test_teardown_resets_so_nothing_leaks_across_sequential_requests(capture):
    assert capture(user_id=1, username='alice', role='admin')[0] == 'alice'
    # Between requests, on the same thread: the request frame must be gone.
    between = database.get_connection()
    try:
        assert _resolved(between)[0] == 'pytest'
    finally:
        between.close()
    assert capture(user_id=2, username='bob', role='admin')[0] == 'bob'
    after = database.get_connection()
    try:
        assert _resolved(after)[0] == 'pytest'
    finally:
        after.close()


# ── scripts ──────────────────────────────────────────────────────────────────

def test_a_script_identity_is_bound_to_its_own_connection_only(tmp_db, no_default):
    sc = database.script_connection('scripts/fix_things.py', operator='put',
                                    reason='re-cost pid 1')
    other = database.get_connection()
    try:
        assert _resolved(sc) == ('put', 'manual', 'script:fix_things.py: re-cost pid 1')
        assert _resolved(other)[0] is None
        with actor.acting_as(detail='wacc:recalculate'):
            assert _resolved(sc)[2] == 'script:fix_things.py: re-cost pid 1 > wacc:recalculate'
    finally:
        sc.close()
        other.close()


@pytest.mark.parametrize('operator,reason', [('', 'why'), ('put', ''), ('  ', 'why')])
def test_a_script_must_name_its_operator_and_reason(tmp_db, operator, reason):
    with pytest.raises(ValueError):
        database.script_connection('x.py', operator=operator, reason=reason)


def test_a_script_connection_is_refused_inside_the_app(tmp_db):
    from app import app as flask_app
    with flask_app.app_context():
        with pytest.raises(RuntimeError):
            database.script_connection('x.py', operator='put', reason='why')


@pytest.fixture
def utc_process(monkeypatch):
    """A `railway ssh` shell has no TZ; emulate that as UTC, then restore."""
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_script_stamps_bangkok_time_like_the_app(tmp_db, utc_process):
    sc = database.script_connection('x.py', operator='put', reason='why')
    try:
        hours = sc.execute(
            "SELECT (julianday(datetime('now','localtime')) - julianday('now')) * 24"
        ).fetchone()[0]
        assert round(hours) == 7
    finally:
        sc.close()


# ── the connection itself ────────────────────────────────────────────────────

def _off_by_default(monkeypatch):
    """Emulate a SQLite build whose compile-time default is trusted_schema=OFF."""
    real = sqlite3.connect

    def _connect(*a, **kw):
        c = real(*a, **kw)
        c.execute('PRAGMA trusted_schema = OFF')
        return c
    monkeypatch.setattr(database.sqlite3, 'connect', _connect)


def _trigger_can_call_it(conn):
    conn.execute('CREATE TABLE _probe_t (x)')
    conn.execute('CREATE TABLE _probe_log (who)')
    conn.execute("CREATE TRIGGER _probe_tr AFTER INSERT ON _probe_t "
                 "BEGIN INSERT INTO _probe_log VALUES (sendy_actor('who')); END")
    conn.execute('INSERT INTO _probe_t VALUES (1)')
    return conn.execute('SELECT who FROM _probe_log').fetchone()[0]


def test_get_connection_pins_trusted_schema_on(tmp_db, monkeypatch):
    _off_by_default(monkeypatch)
    conn = database.get_connection()
    try:
        assert conn.execute('PRAGMA trusted_schema').fetchone()[0] == 1
        assert _trigger_can_call_it(conn) == 'pytest'
    finally:
        conn.rollback()
        conn.close()


def test_script_connection_pins_trusted_schema_on(tmp_db, monkeypatch):
    _off_by_default(monkeypatch)
    sc = database.script_connection('x.py', operator='put', reason='why')
    try:
        assert sc.execute('PRAGMA trusted_schema').fetchone()[0] == 1
        assert _trigger_can_call_it(sc) == 'put'
    finally:
        sc.rollback()
        sc.close()


def test_sendy_actor_never_raises(tmp_db, monkeypatch):
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT sendy_actor('no-such-field')").fetchone()[0] is None

        def _boom(*a, **kw):
            raise RuntimeError('resolver bug')
        monkeypatch.setattr(actor, 'current', _boom)
        assert conn.execute("SELECT sendy_actor('who')").fetchone()[0] is None
    finally:
        conn.close()


# ── the four fixtures other tests write through ──────────────────────────────

def test_tmp_db_conn_carries_the_function(tmp_db_conn):
    assert _resolved(tmp_db_conn)[0] == 'pytest'


def test_tmp_db_conn_hr_clean_carries_the_function(tmp_db_conn_hr_clean):
    assert _resolved(tmp_db_conn_hr_clean)[0] == 'pytest'


def test_empty_db_conn_carries_the_function(empty_db_conn):
    assert _resolved(empty_db_conn)[0] == 'pytest'


def test_patch_models_conn_carries_the_function(tmp_db, patch_models_conn):
    import models
    patch_models_conn(lambda: sqlite3.connect(tmp_db))
    conn = models.get_connection()
    try:
        assert _resolved(conn)[0] == 'pytest'
    finally:
        conn.close()
