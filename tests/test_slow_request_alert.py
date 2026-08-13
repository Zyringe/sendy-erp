"""Early warning for a request drifting toward gunicorn's 60s kill.

WHY THIS EXISTS
    2026-08-11: the marketplace settlement import 500'd on prod. Not a crash —
    gunicorn `--timeout 60` SIGABRT'd the worker mid-request, so the route's own
    try/except never ran and nothing was flashed or logged by the app.

    The damning part is the month before it. The July matcher rebuild doubled
    the cost of that import (measured: 15.2s → 33.0s on identical data), and
    every import since had been taking 40-55s and SUCCEEDING. "A bit slow" is
    indistinguishable from "about to break" if nobody measures it, so the first
    signal anyone got was the team hitting an Internal Server Error.

    A request that actually times out cannot report itself — the process is
    gone. So the warning has to come from the slow-but-still-succeeding runs,
    which is exactly the window that was wasted. At 33s this would have fired
    weeks early, on /alerts, where Put sees it (see system_alerts.py's
    docstring: a flash reaches whoever ran the import, which is not Put).

TWO LEVELS ON PURPOSE
    One threshold tells you once and then goes quiet — it cannot express "this
    is getting worse", which is the shape of the actual failure. Crossing into
    critical is a SEPARATE incident, so it alerts even while the warning is
    still open.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models
from models import system_alerts as sa


@pytest.fixture
def client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app.test_client()


def _open_alerts(conn, kind=sa.KIND_SLOW_REQUEST):
    return conn.execute(
        "SELECT id, kind, severity, message, dedupe_key, context_json"
        "  FROM system_alerts WHERE resolved_at IS NULL AND kind = ?"
        " ORDER BY id", (kind,)).fetchall()


def test_a_normal_fast_request_records_nothing(empty_db_conn):
    """Control included: the same call ABOVE the threshold must alert, so a
    green result here can't just mean 'the test never reached the code'."""
    assert models.record_slow_request_alert('marketplace.upload', 'POST', 0.4) is None
    assert models.record_slow_request_alert('marketplace.upload', 'POST',
                                            sa.SLOW_REQUEST_WARN_SECONDS - 0.01) is None
    assert _open_alerts(empty_db_conn) == []

    # Control: the recorder CAN write, so the empty result above is meaningful.
    assert models.record_slow_request_alert('marketplace.upload', 'POST',
                                            sa.SLOW_REQUEST_WARN_SECONDS) is not None
    assert len(_open_alerts(empty_db_conn)) == 1


def test_a_slow_request_records_one_warning(empty_db_conn):
    models.record_slow_request_alert('marketplace.upload', 'POST', 33.0)

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row['severity'] == 'warning'
    assert 'marketplace.upload' in row['message']
    assert '33' in row['message']
    assert str(sa.REQUEST_TIMEOUT_SECONDS) in row['message']


def test_a_very_slow_request_records_an_error(empty_db_conn):
    models.record_slow_request_alert('marketplace.upload', 'POST',
                                     sa.SLOW_REQUEST_CRITICAL_SECONDS + 1)

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1
    assert rows[0]['severity'] == 'error'


def test_the_same_slow_endpoint_does_not_stack_alerts(empty_db_conn):
    """A weekly import that is slow every week must hold ONE open alert, not
    52. The partial unique index on (kind, dedupe_key) does the work."""
    for _ in range(5):
        models.record_slow_request_alert('marketplace.upload', 'POST', 33.0)

    assert len(_open_alerts(empty_db_conn)) == 1


def test_crossing_into_critical_alerts_even_while_the_warning_is_open(empty_db_conn):
    """Degradation is the thing being watched, so getting worse is news."""
    models.record_slow_request_alert('marketplace.upload', 'POST', 25.0)
    models.record_slow_request_alert('marketplace.upload', 'POST', 50.0)

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 2
    assert sorted(r['severity'] for r in rows) == ['error', 'warning']


def test_two_different_endpoints_alert_separately(empty_db_conn):
    models.record_slow_request_alert('marketplace.upload', 'POST', 33.0)
    models.record_slow_request_alert('bsn.import_weekly', 'POST', 33.0)

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 2
    assert len({r['dedupe_key'] for r in rows}) == 2


def test_the_alert_carries_the_numbers_needed_to_act(empty_db_conn):
    models.record_slow_request_alert('marketplace.upload', 'POST', 33.25)

    import json
    ctx = json.loads(_open_alerts(empty_db_conn)[0]['context_json'])
    assert ctx['endpoint'] == 'marketplace.upload'
    assert ctx['method'] == 'POST'
    assert ctx['seconds'] == pytest.approx(33.25, abs=0.01)
    assert ctx['timeout_seconds'] == sa.REQUEST_TIMEOUT_SECONDS


def test_the_hook_is_wired_and_measures_a_real_request(client, monkeypatch):
    """The recorder above is useless if nothing calls it. Exercises the real
    before/after_request pair through a real request rather than asserting the
    hooks exist — a registration check would pass with the body deleted."""
    calls = []
    monkeypatch.setattr(models, 'record_slow_request_alert',
                        lambda ep, m, s: calls.append((ep, m, s)))

    resp = client.get('/healthz')

    assert resp.status_code == 200
    # Count FIRST: if the hook never ran, the field assertions below would be
    # vacuous rather than red.
    assert len(calls) == 1, calls
    endpoint, method, seconds = calls[0]
    assert endpoint == 'healthz'
    assert method == 'GET'
    assert isinstance(seconds, float) and seconds >= 0


def test_a_broken_alert_hook_never_breaks_the_page(client, monkeypatch):
    """Monitoring must not be able to take the app down. If this regresses, a
    bug in alerting becomes a 500 on every route at once."""
    def boom(*a, **k):
        raise RuntimeError('alerting is broken')

    monkeypatch.setattr(models, 'record_slow_request_alert', boom)

    assert client.get('/healthz').status_code == 200


def test_a_slow_request_actually_reaches_put_on_the_alerts_page(
        empty_db_conn, client, monkeypatch):
    """The end the whole feature exists for: recorded is not the same as SEEN.

    Drives a real request with the threshold dropped to ~0 so an ordinary fast
    request qualifies, then renders /alerts the way Put would open it and
    asserts the message is on the page.
    """
    monkeypatch.setattr(sa, 'SLOW_REQUEST_WARN_SECONDS', 0.0)
    monkeypatch.setattr(sa, 'SLOW_REQUEST_CRITICAL_SECONDS', 999.0)

    assert client.get('/healthz').status_code == 200
    assert len(_open_alerts(empty_db_conn)) == 1, 'the request did not alert'

    with client.session_transaction() as s:
        s['role'] = 'admin'; s['username'] = 'admin'; s['user_id'] = 1
    page = client.get('/alerts')

    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'healthz' in html
    assert 'Internal Server Error' in html      # the actionable warning text


def test_a_locked_db_makes_the_alert_give_up_fast_instead_of_stalling(empty_db):
    """The self-defeating failure: an already-slow request must not be pushed
    OVER the 60s line by the act of filing its own warning.

    ⚠ This pins the OUTCOME (fast + quiet), not the mechanism. A review claimed
    get_connection()'s pre-`busy_timeout` statements could add ~10s here;
    measuring said otherwise — `sqlite3.connect` takes no lock and
    `PRAGMA journal_mode=WAL` under a held lock raises in 0.00s rather than
    waiting — so this test deliberately passes on both connection shapes. Do
    not read a green here as proof of the connect-time ordering; it proves the
    recorder cannot stall or throw, which is the property that matters.
    """
    import time
    blocker = sqlite3.connect(empty_db, timeout=10)
    try:
        blocker.execute("BEGIN IMMEDIATE")            # hold the write lock
        blocker.execute(
            "INSERT INTO system_alerts (kind, severity, message, dedupe_key)"
            " VALUES ('x', 'warning', 'holding the lock', 'x')")

        t0 = time.monotonic()
        result = models.record_slow_request_alert('marketplace.upload', 'POST', 33.0)
        elapsed = time.monotonic() - t0

        assert result is None                          # best-effort: no crash
        # Wide margin on purpose: the bug waits ~10s, the fix ~2s. Anything
        # under 5s can only be the short timeout.
        assert elapsed < 5.0, f'alert path stalled {elapsed:.1f}s on a locked DB'
    finally:
        blocker.rollback()
        blocker.close()


def test_the_timeout_constant_matches_the_real_gunicorn_timeout():
    """`REQUEST_TIMEOUT_SECONDS` is QUOTED TO PUT in the alert text ("เพดานของ
    เซิร์ฟเวอร์คือ 60 วินาที"), but the real limit lives in the Procfile /
    railway.toml `--timeout`. Nothing else keeps the two in step, so changing
    the deploy without this constant would make every alert state a number
    that is not true — worse than no alert, because it is confidently wrong.

    Same class of reason as tests/test_gunicorn_preload.py pinning --preload.
    """
    import re

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    checked = 0
    for name in ('Procfile', 'railway.toml'):
        text = open(os.path.join(repo, name), encoding='utf-8').read()
        # Ignore comments so a rationale note can't satisfy this by accident.
        live = '\n'.join(l for l in text.splitlines() if not l.strip().startswith('#'))
        m = re.search(r'--timeout\s+(\d+)', live)
        assert m, f'{name}: no --timeout found in the gunicorn start command'
        assert int(m.group(1)) == sa.REQUEST_TIMEOUT_SECONDS, (
            f'{name} says --timeout {m.group(1)} but REQUEST_TIMEOUT_SECONDS is '
            f'{sa.REQUEST_TIMEOUT_SECONDS}; the alert text would lie to Put')
        checked += 1
    assert checked == 2, 'both deploy files must be checked'

    # The whole point is warning BEFORE the kill, so the order must hold.
    assert 0 < sa.SLOW_REQUEST_WARN_SECONDS < sa.SLOW_REQUEST_CRITICAL_SECONDS \
        < sa.REQUEST_TIMEOUT_SECONDS


def test_recording_is_best_effort_and_never_raises(empty_db_conn, monkeypatch):
    """An alerting problem must never become the user's problem. Same stance as
    record_wacc_identity_alert."""
    def boom(*a, **k):
        raise RuntimeError('table is gone')

    monkeypatch.setattr(sa, 'create_system_alert', boom)

    assert models.record_slow_request_alert('marketplace.upload', 'POST', 33.0) is None
