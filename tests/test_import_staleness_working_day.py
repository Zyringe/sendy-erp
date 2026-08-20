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


def _log_import(conn, when_iso, filename='express-dbf-upload'):
    """A COMPLETED run, as blueprints/bsn.py records it after the import
    returns. This is the freshness marker -- see the P1 block at the bottom of
    this file for why a sub-import row is not."""
    import json
    conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes,"
        " imported_at) VALUES (?, 0, 0, ?, ?)",
        (filename, json.dumps({'bsn': {'ok': True, 'summary': 'x'}}),
         when_iso + ' 17:00:00'))
    conn.commit()


def _sub_import_marker(conn, when_iso):
    """The express_import_log row payments_out commits partway through the
    import -- deliberately NOT a completion signal."""
    conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename,"
        " record_count, line_count, status, imported_at)"
        " VALUES ('payments_out', 'express_dbf', 1, 1, 'imported', ?)",
        (when_iso + ' 17:00:00',))
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
    """Scope guard: only the DBF upload flow marks this fresh. import_log is a
    shared table -- other importers write their own rows into it."""
    _log_import(empty_db_conn, SAT, filename='weekly-sales.csv')
    assert _fresh(empty_db_conn, MON)['is_stale'] is True


def test_a_sub_import_row_alone_does_not_count(empty_db_conn):
    """Scope guard for P1 from the other side: the express_import_log row that
    payments_out commits mid-import is not, on its own, evidence of anything."""
    _sub_import_marker(empty_db_conn, SAT)
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

def test_the_dashboard_raises_it_and_cannot_be_broken_by_it(tmp_db, monkeypatch):
    """The alert is raised from the two pages that ALREADY compute freshness
    (dashboard + import page), not from an after_request hook charging every
    page in the app for a once-a-day signal (Codex round 7).

    Monitoring must still never become the outage, so the failure case is
    asserted here too: break the helper, the page must still render."""
    import sqlite3
    import config
    from app import app as flask_app
    import models as models_mod

    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM import_log WHERE filename = 'express-dbf-upload'")
    conn.execute("DELETE FROM system_alerts WHERE kind = 'import_stale'")
    conn.commit()
    conn.close()

    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'u'
        s['role'] = 'admin'

    assert c.get('/').status_code == 200                       # control
    check = sqlite3.connect(config.DATABASE_PATH)
    n = check.execute(
        "SELECT COUNT(*) FROM system_alerts"
        " WHERE kind = 'import_stale' AND resolved_at IS NULL").fetchone()[0]
    check.close()
    assert n == 1, f'the dashboard did not persist an alert (got {n})'

    def _boom(*a, **k):
        raise RuntimeError('alert subsystem down')
    monkeypatch.setattr(models_mod, 'record_import_staleness_alert', _boom)
    assert c.get('/').status_code == 200, 'a broken alert must not break the page'


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


# -- the marker must mean "the import COMPLETED", not "something committed" ---

def _completed_run(conn, when_iso, bsn_ok=True, vat_only=False):
    """A row exactly as blueprints/bsn.py writes it after an upload."""
    import json
    notes = {'upload': {'filename': 'daily.zip'}}
    if vat_only:
        notes['vat'] = {'ok': None, 'status': 'building'}
    else:
        notes['bsn'] = ({'ok': True, 'summary': 'x'} if bsn_ok
                        else {'ok': False, 'error': 'parse died'})
    conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes,"
        " imported_at) VALUES ('express-dbf-upload', 0, 0, ?, ?)",
        (json.dumps(notes, ensure_ascii=False), when_iso + ' 17:00:00'))
    conn.commit()


def test_a_failed_import_is_not_reported_fresh(empty_db_conn):
    """THE regression (Codex round 7, P1).

    commit_express_dbf runs payments_out -- which commits its own
    express_import_log row with source_filename='express_dbf' -- BEFORE
    credit_notes_ap and the six isolated registers. If a later step raises, the
    route reports the BSN import as FAILED while that sub-import marker is
    already committed under today's timestamp. Reading MAX(imported_at) over
    those rows therefore says "fresh" about an import that failed, and would
    even let the staleness alert clear.

    So the fixture writes BOTH: the sub-import marker (as payments_out leaves
    it) and the run record saying the run failed. Fresh-by-sub-import must lose
    to failed-by-run."""
    _sub_import_marker(empty_db_conn, MON)       # payments_out got that far
    _completed_run(empty_db_conn, MON, bsn_ok=False)
    f = _fresh(empty_db_conn, TUE)
    assert f['is_stale'] is True, f


def test_a_completed_run_is_reported_fresh(empty_db_conn):
    """Control for the test above: the same shape, only the outcome differs.
    Without this the one above could pass by never reading anything."""
    _sub_import_marker(empty_db_conn, MON)
    _completed_run(empty_db_conn, MON, bsn_ok=True)
    f = _fresh(empty_db_conn, TUE)
    assert f['is_stale'] is False, f
    assert f['last_date'] == MON


def test_a_vat_only_upload_does_not_refresh_the_main_book(empty_db_conn):
    """The VAT half rebuilds vat_book.db and writes nothing to this book's
    ledger, so it must not make the main book look freshly imported."""
    _completed_run(empty_db_conn, MON, vat_only=True)
    assert _fresh(empty_db_conn, TUE)['is_stale'] is True
