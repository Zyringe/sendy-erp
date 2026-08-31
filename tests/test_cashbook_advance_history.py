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


# ── plan.md P2 step 2: the advance-cap fields (advisory bar) ───────────────

def test_advance_history_adds_cap_fields_keeps_existing_keys(migrated_db):
    """New fields land ALONGSIDE the pre-existing ones (net_pay etc.) — other
    callers still read those, per plan.md step 2 ('Keep every existing key')."""
    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    emp = conn.execute(
        "SELECT id FROM employees WHERE is_active=1 AND company_id IS NOT NULL LIMIT 1"
    ).fetchone()["id"]
    conn.close()

    resp = _client().get(f"/cashbook/advance-history/{emp}?month=2099-08")
    assert resp.status_code == 200
    data = resp.get_json()

    # pre-existing keys still present (regression guard)
    for key in ("employee", "month", "advances", "month_total",
               "outstanding_total", "net_pay"):
        assert key in data, f"pre-existing key {key} must survive"

    # new keys (plan.md step 2)
    for key in ("salary_rate", "base_amount", "warn_pct", "month_advance_total",
               "collectable", "target_month", "target_month_finalized"):
        assert key in data, f"new field {key} missing"
    assert data["month_advance_total"] == data["month_total"], (
        "same figure under the plan.md-named key")
    assert data["target_month"] == "2099-08"
    assert data["target_month_finalized"] is False
    assert data["collectable"] is not None and data["collectable"] > 0
    assert data["warn_pct"] == 0.5


def test_advance_history_target_month_finalized_true_when_run_closed(migrated_db):
    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    emp = conn.execute(
        "SELECT id, company_id FROM employees WHERE is_active=1 AND company_id IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.execute(
        "INSERT INTO payroll_runs (year_month, company_id, status) VALUES ('2099-09', ?, 'finalized')",
        (emp["company_id"],),
    )
    conn.commit()
    conn.close()

    resp = _client().get(f"/cashbook/advance-history/{emp['id']}?month=2099-09")
    data = resp.get_json()
    assert data["target_month_finalized"] is True


def test_advance_history_collectable_is_lower_with_carried_in(migrated_db):
    """Sanity check that collectable is computed via hr.collectable_this_month
    (shares the carry source with the payslip), not hardcoded."""
    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    emp = conn.execute(
        "SELECT id, company_id FROM employees WHERE is_active=1 AND company_id IS NOT NULL LIMIT 1"
    ).fetchone()
    run_id = conn.execute(
        "INSERT INTO payroll_runs (year_month, company_id, status) VALUES ('2099-10', ?, 'finalized')",
        (emp["company_id"],),
    ).lastrowid
    conn.execute(
        "INSERT INTO payroll_items (run_id, employee_id, carried_out) VALUES (?,?,950)",
        (run_id, emp["id"]),
    )
    conn.commit()
    conn.close()

    no_carry = _client().get(f"/cashbook/advance-history/{emp['id']}?month=2099-10").get_json()
    with_carry = _client().get(f"/cashbook/advance-history/{emp['id']}?month=2099-11").get_json()
    assert with_carry["collectable"] < no_carry["collectable"]
    assert with_carry["collectable"] == pytest.approx(no_carry["collectable"] - 950, abs=0.01)
