"""Phase 2 — the "ดูประวัติ" advance-history endpoint (plan.md decision C6).
When entering an advance you can peek at that employee's advances this month,
their TOTAL still-outstanding (not-yet-deducted) advances, and this month's net
salary — so you don't over-advance. Read-only JSON; the modal (front-end) calls
it.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database


@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _client(role='admin', user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def test_advance_history_month_outstanding_and_netpay(migrated_db):
    conn = sqlite3.connect(migrated_db)
    emp = conn.execute("SELECT id FROM employees WHERE is_active=1 LIMIT 1").fetchone()[0]
    # a finalized payroll run + item for 2099-07 (net 12,000) — future month so it
    # can't collide with real data in the live clone
    run_id = conn.execute(
        "INSERT INTO payroll_runs (year_month, company_id, status) VALUES ('2099-07', NULL, 'finalized')"
    ).lastrowid
    conn.execute(
        "INSERT INTO payroll_items (run_id, employee_id, net_pay) VALUES (?,?,12000)", (run_id, emp)
    )
    # advances: this-month outstanding 1,000 + this-month deducted 500 + prev-month outstanding 300
    conn.execute("INSERT INTO salary_advances (employee_id, advance_date, amount) VALUES (?, '2099-07-05', 1000)", (emp,))
    conn.execute("INSERT INTO salary_advances (employee_id, advance_date, amount, deducted_in_run_id) VALUES (?, '2099-07-20', 500, ?)", (emp, run_id))
    conn.execute("INSERT INTO salary_advances (employee_id, advance_date, amount) VALUES (?, '2099-06-10', 300)", (emp,))
    conn.commit()
    conn.close()

    resp = _client().get(f"/cashbook/advance-history/{emp}?month=2099-07")
    assert resp.status_code == 200
    data = resp.get_json()

    assert len(data["advances"]) == 2, "only 2099-07 advances listed"
    assert data["month_total"] == 1500
    assert data["outstanding_total"] == 1300, "all not-yet-deducted advances (across months)"
    assert data["net_pay"] == 12000


def test_advance_history_unknown_employee_404(migrated_db):
    assert _client().get("/cashbook/advance-history/999999?month=2099-07").status_code == 404
