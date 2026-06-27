# tests/test_hr_phase8.py — Phase 8: monthly payroll reminder (read-only dashboard nudge)
import os
import sqlite3
from datetime import date

import pytest


# ── Task 8.1: pure helpers ────────────────────────────────────────────────────

def test_previous_ym():
    import hr
    assert hr.previous_ym(date(2026, 6, 15)) == "2026-05"
    assert hr.previous_ym(date(2026, 1, 10)) == "2025-12"   # year rollover
    assert hr.previous_ym(date(2026, 3, 1))  == "2026-02"


def test_reminder_none_when_prev_run_exists(tmp_db):
    import hr
    conn = sqlite3.connect(tmp_db)
    # company 1 already has a 2026-05 run in the live-copy DB → June's "today" → prev=2026-05 exists
    assert hr.payroll_reminder_month(date(2026, 6, 10), conn) is None


def test_reminder_returns_month_when_missing(tmp_db):
    import hr
    conn = sqlite3.connect(tmp_db)
    # "today" in a month whose previous month has no run (no 2026-06 run exists) → July → prev=2026-06 missing
    assert hr.payroll_reminder_month(date(2026, 7, 5), conn) == "2026-06"


def test_reminder_triggers_for_any_payroll_company(tmp_db):
    # Companies are DERIVED from on_payroll employees, not hardcoded: if company 2
    # gains an on_payroll employee and lacks last month's run, the nudge fires —
    # even though company 1 IS up to date. (Conversely, test_reminder_none_when_
    # prev_run_exists already proves company 2 is IGNORED while it has no employees.)
    import hr
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE employees SET company_id=2, on_payroll=1, is_active=1 WHERE emp_code='EMP006'")
    conn.commit()
    # company 1 still has its 2026-05 run; company 2 now is a payroll co. with none
    assert hr.payroll_reminder_month(date(2026, 6, 10), conn) == "2026-05"


# ── Task 8.2: dashboard wiring ────────────────────────────────────────────────

def _client(role, user_id=1):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id; s['username'] = f'test-{role}'; s['role'] = role
    return c


BANNER = "ยังไม่ได้สร้างรอบเงินเดือน"


def test_dashboard_banner_for_admin_when_missing(tmp_db):
    # delete ALL runs → whatever the real previous month is, it's missing → banner
    conn = sqlite3.connect(tmp_db); conn.execute("DELETE FROM payroll_runs"); conn.commit(); conn.close()
    html = _client('admin', 1).get('/').get_data(as_text=True)
    assert BANNER in html


def test_dashboard_no_banner_when_run_exists(tmp_db):
    import hr
    from datetime import date
    prev = hr.previous_ym(date.today())          # date-robust: seed THIS month's prev
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM payroll_runs")
    conn.execute("INSERT INTO payroll_runs(year_month,company_id,status) VALUES(?,1,'finalized')", (prev,))
    conn.commit(); conn.close()
    html = _client('admin', 1).get('/').get_data(as_text=True)
    assert BANNER not in html


def test_dashboard_no_banner_for_non_admin(tmp_db):
    conn = sqlite3.connect(tmp_db); conn.execute("DELETE FROM payroll_runs"); conn.commit(); conn.close()
    html = _client('manager', 3).get('/').get_data(as_text=True)   # manager, not general (general redirects off '/')
    assert BANNER not in html
