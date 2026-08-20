"""The daily-import staleness signal: a working-day rule, and an alert.

TWO problems, both measured (plan F5, projects/express-integration/
import-flow-plan-2026-08-17.md):

1. The old rule was a flat 26-hour threshold. The team works Mon-Sat, so
   EVERY MONDAY read ~38h and showed red with nothing wrong -- measured
   2026-08-17 at exactly that. A signal that is wrong one working day in six
   is a signal nobody trusts, which is why nothing caught AR/AP going 73 days
   without an import.
2. Staleness was a passive badge on two pages. An absence raises no event and
   there is no scheduler on this app (Procfile is gunicorn and nothing else),
   so the alert has to be raised BY a request -- the same shape
   record_slow_request_alert already uses.

The rule: stale iff there has been no import since the last COMPLETED working
day, where a working day is any day that is not a Sunday and not in
company_holidays. That table is empty on prod (0 rows), so today this is
exactly the Sunday rule -- and it starts honouring holidays the day someone
fills it in, with no code change.

An alert that stays open after the import lands is the same wolf-crying one
layer up, so a successful import RESOLVES it (resolved_by records that it was
automatic; the row stays for history).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import datetime

import pytest

import models
from models import imports as imports_mod
from models import system_alerts as sa


# 2026-08-17 is a Monday, 08-15 a Saturday, 08-16 a Sunday.
MON = '2026-08-17'
SUN = '2026-08-16'
SAT = '2026-08-15'
FRI = '2026-08-14'
TUE = '2026-08-18'


def _log_import(conn, when_iso, source='express_dbf'):
    conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename,"
        " record_count, line_count, status, imported_at)"
        " VALUES ('sales', ?, 1, 1, 'imported', ?)", (source, when_iso + ' 17:00:00'))
    conn.commit()


def _holiday(conn, day_iso):
    # Force the state: empty_db has no companies row, and company_holidays
    # carries an FK to it. Never inherit fixture state -- create it.
    conn.execute("INSERT OR IGNORE INTO companies (id, code, name_th, is_active)"
                 " VALUES (1, 'TST', 'ทดสอบ', 1)")
    conn.execute(
        "INSERT INTO company_holidays (company_id, holiday_date, name_th, year)"
        " VALUES (1, ?, 'ทดสอบ', ?)", (day_iso, int(day_iso[:4])))
    conn.commit()


def _fresh(conn, today):
    return models.get_express_dbf_freshness(today=today, conn=conn)


# -- the working-day rule ----------------------------------------------------

def test_monday_after_a_saturday_import_is_NOT_stale(empty_db_conn):
    """THE case this rule exists for. The old 26h threshold called this stale
    every single week."""
    _log_import(empty_db_conn, SAT)
    f = _fresh(empty_db_conn, MON)
    assert f['is_stale'] is False, f
    assert f['expected_since'] == SAT          # Sunday skipped, Saturday is it


def test_monday_after_only_a_friday_import_IS_stale(empty_db_conn):
    """Saturday was a working day and no import happened -- that is the real
    miss the badge is supposed to catch."""
    _log_import(empty_db_conn, FRI)
    f = _fresh(empty_db_conn, MON)
    assert f['is_stale'] is True, f
    assert f['expected_since'] == SAT


def test_sunday_after_a_saturday_import_is_not_stale(empty_db_conn):
    _log_import(empty_db_conn, SAT)
    assert _fresh(empty_db_conn, SUN)['is_stale'] is False


def test_ordinary_weekday_after_yesterdays_import_is_not_stale(empty_db_conn):
    _log_import(empty_db_conn, MON)
    f = _fresh(empty_db_conn, TUE)
    assert f['is_stale'] is False
    assert f['expected_since'] == MON


def test_todays_own_import_counts(empty_db_conn):
    _log_import(empty_db_conn, TUE)
    assert _fresh(empty_db_conn, TUE)['is_stale'] is False


def test_no_import_at_all_is_stale(empty_db_conn):
    f = _fresh(empty_db_conn, MON)
    assert f['is_stale'] is True
    assert f['last_at'] is None


def test_a_non_dbf_import_does_not_count(empty_db_conn):
    """Scope guard: only the DBF flow marks this fresh, matching the docstring
    contract the dashboard copy relies on."""
    _log_import(empty_db_conn, SAT, source='something_else')
    assert _fresh(empty_db_conn, MON)['is_stale'] is True


def test_company_holidays_are_skipped_once_the_table_is_filled(empty_db_conn):
    """Option (b) with no code change: the rule reads holidays if any exist."""
    _holiday(empty_db_conn, SAT)
    _log_import(empty_db_conn, FRI)
    f = _fresh(empty_db_conn, MON)
    assert f['is_stale'] is False, f
    assert f['expected_since'] == FRI          # Sunday AND the holiday skipped
    assert f['rule'] == 'sunday+holidays'


def test_rule_reports_sunday_only_when_no_holidays_are_configured(empty_db_conn):
    _log_import(empty_db_conn, SAT)
    assert _fresh(empty_db_conn, MON)['rule'] == 'sunday'


def test_a_holiday_run_cannot_loop_forever(empty_db_conn):
    """Defensive: every day of a month marked a holiday must still terminate
    and must not silently report 'fresh'."""
    d = datetime.date(2026, 7, 20)
    while d < datetime.date(2026, 8, 18):
        _holiday(empty_db_conn, d.isoformat())
        d += datetime.timedelta(days=1)
    f = _fresh(empty_db_conn, TUE)
    assert f['expected_since'] is not None
    assert f['is_stale'] is True               # no import ever logged


# -- the alert ---------------------------------------------------------------

def _open_staleness_alerts(conn):
    return conn.execute(
        "SELECT id, message FROM system_alerts"
        " WHERE kind = ? AND resolved_at IS NULL", (sa.KIND_IMPORT_STALE,)
    ).fetchall()


def test_stale_import_raises_exactly_one_alert(empty_db_conn):
    _log_import(empty_db_conn, FRI)
    for _ in range(5):                          # five requests, one incident
        models.record_import_staleness_alert(
            models.get_express_dbf_freshness(today=MON, conn=empty_db_conn),
            conn=empty_db_conn)
    empty_db_conn.commit()
    rows = _open_staleness_alerts(empty_db_conn)
    assert len(rows) == 1, [dict(r) for r in rows]   # count, not "at least one"


def test_a_fresh_import_raises_nothing(empty_db_conn):
    _log_import(empty_db_conn, SAT)
    models.record_import_staleness_alert(
        models.get_express_dbf_freshness(today=MON, conn=empty_db_conn),
        conn=empty_db_conn)
    empty_db_conn.commit()
    assert _open_staleness_alerts(empty_db_conn) == []


def test_a_successful_import_resolves_the_open_alert(empty_db_conn):
    """Otherwise the list never clears itself and becomes the thing people
    learn to ignore -- the exact failure this phase exists to fix."""
    _log_import(empty_db_conn, FRI)
    models.record_import_staleness_alert(
        models.get_express_dbf_freshness(today=MON, conn=empty_db_conn),
        conn=empty_db_conn)
    empty_db_conn.commit()
    assert len(_open_staleness_alerts(empty_db_conn)) == 1   # control

    n = models.clear_import_staleness_alert(conn=empty_db_conn)
    empty_db_conn.commit()
    assert n == 1
    assert _open_staleness_alerts(empty_db_conn) == []
    row = empty_db_conn.execute(
        "SELECT resolved_by FROM system_alerts WHERE kind = ?",
        (sa.KIND_IMPORT_STALE,)).fetchone()
    assert 'auto' in (row['resolved_by'] or '')

    # And a still-stale state may alert AGAIN afterwards -- a recurrence is news.
    models.record_import_staleness_alert(
        models.get_express_dbf_freshness(today=MON, conn=empty_db_conn),
        conn=empty_db_conn)
    empty_db_conn.commit()
    assert len(_open_staleness_alerts(empty_db_conn)) == 1


def test_clear_is_a_no_op_when_nothing_is_open(empty_db_conn):
    assert models.clear_import_staleness_alert(conn=empty_db_conn) == 0


# -- the request hook must never be able to break a page ---------------------

def test_after_request_hook_swallows_its_own_failure(tmp_db, monkeypatch):
    """Monitoring must not become the outage. Same stance as the slow-request
    hook it sits beside: whole body in try/except, response returned regardless.

    Break-it-once: removing that try/except turns this into a 500."""
    from app import app as flask_app
    import models as models_mod

    def _boom(*a, **k):
        raise RuntimeError('alert subsystem down')
    monkeypatch.setattr(models_mod, 'get_express_dbf_freshness', _boom)

    flask_app.config['TESTING'] = False        # let the hook's except actually run
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'u'
        s['role'] = 'admin'
    try:
        r = c.get('/alerts')
        assert r.status_code == 200, r.status_code
    finally:
        flask_app.config['TESTING'] = True


def test_repeat_calls_do_not_write_once_an_alert_is_open(empty_db_conn):
    """The hook runs on EVERY html page view and a stale stretch lasts days, so
    the steady state must not take a write lock for an INSERT the dedupe index
    will discard anyway. Counts actual statements via sqlite3's own trace
    callback -- a real signal, not elapsed time."""
    _log_import(empty_db_conn, FRI)
    fresh = models.get_express_dbf_freshness(today=MON, conn=empty_db_conn)

    models.record_import_staleness_alert(fresh, conn=empty_db_conn)
    empty_db_conn.commit()
    assert len(_open_staleness_alerts(empty_db_conn)) == 1      # control

    seen = []
    empty_db_conn.set_trace_callback(seen.append)
    try:
        for _ in range(10):
            models.record_import_staleness_alert(fresh, conn=empty_db_conn)
    finally:
        empty_db_conn.set_trace_callback(None)

    # Control: the calls really did run and really did hit the DB.
    assert len(seen) >= 10, seen
    writes = [q for q in seen
              if q.lstrip()[:6].upper() in ('INSERT', 'UPDATE', 'DELETE')]
    assert writes == [], writes
    assert len(_open_staleness_alerts(empty_db_conn)) == 1


def test_a_real_page_request_actually_persists_the_alert(tmp_db):
    """END-TO-END, through the real hook, with NO hand-managed connection.

    This exists because every unit test above passes its own `conn` and commits
    it itself -- so all sixteen stayed green while the hook was, in the real
    app, inserting into a connection nobody ever committed and closing it. The
    feature was completely dead and pytest could not see it. Assert the ROW,
    from a separate connection, after a real request.
    """
    import sqlite3
    import config
    from app import app as flask_app

    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM express_import_log")          # force: no import
    conn.execute("DELETE FROM system_alerts WHERE kind = 'import_stale'")
    conn.commit()
    conn.close()

    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'u'
        s['role'] = 'admin'
    r = c.get('/alerts')
    assert r.status_code == 200                              # control

    check = sqlite3.connect(config.DATABASE_PATH)
    n = check.execute(
        "SELECT COUNT(*) FROM system_alerts"
        " WHERE kind = 'import_stale' AND resolved_at IS NULL").fetchone()[0]
    check.close()
    assert n == 1, f'the request did not persist an alert (got {n})'
