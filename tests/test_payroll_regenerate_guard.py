"""Regenerating an OLD payroll run must not silently delete a departed
employee's row.

`generate_run` does `DELETE FROM payroll_items WHERE run_id=?` then re-inserts
only for `is_active=1 AND on_payroll=1`. So regenerating a historical run after
someone leaves erases their payslip record for that month and (via the
orphaned-advance reconcile immediately below it) un-stamps their advances, which
then bleed into a future run.

Measured on a snapshot of prod 2026-08-05: regenerating April 2026 deleted the
row of an employee who had since left, un-stamped her ฿400 advance, and added
three people who were never on that month's payroll. The reopen button that
leads here is live in the UI for any run with no posted payment rows.

A new run, and a regenerate whose employee set has not shrunk, must both still
work — the guard is specifically about DROPPING an existing row.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database
import hr as hr_mod

YM = '2026-04'


@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _company_id(db):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id FROM companies ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row[0]


def _fresh_run(db, company_id):
    """Drop any existing run for YM, then build one from the current employee set."""
    conn = sqlite3.connect(db)
    run = conn.execute(
        "SELECT id FROM payroll_runs WHERE year_month=? AND company_id=?",
        (YM, company_id),
    ).fetchone()
    if run:
        conn.execute("DELETE FROM payroll_items WHERE run_id=?", (run[0],))
        conn.execute("UPDATE salary_advances SET deducted_in_run_id=NULL"
                     " WHERE deducted_in_run_id=?", (run[0],))
        conn.execute("DELETE FROM payroll_runs WHERE id=?", (run[0],))
    conn.commit()
    conn.close()
    return hr_mod.generate_run(YM, company_id, 'test', db_path=db)


def _item_emp_ids(db, run_id):
    conn = sqlite3.connect(db)
    ids = {r[0] for r in conn.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (run_id,))}
    conn.close()
    return ids


def test_regenerate_refuses_when_it_would_drop_a_departed_employee(migrated_db):
    company_id = _company_id(migrated_db)
    run = _fresh_run(migrated_db, company_id)
    before = _item_emp_ids(migrated_db, run["id"])
    if not before:
        pytest.skip("no payroll items generated for this month in this DB")

    victim = sorted(before)[0]
    conn = sqlite3.connect(migrated_db)
    conn.execute("UPDATE employees SET is_active=0 WHERE id=?", (victim,))
    conn.commit()
    conn.close()

    with pytest.raises(ValueError) as exc:
        hr_mod.generate_run(YM, company_id, 'test', db_path=migrated_db)

    assert str(victim) in str(exc.value) or 'ลาออก' in str(exc.value) or 'พนักงาน' in str(exc.value)
    after = _item_emp_ids(migrated_db, run["id"])
    assert after == before, "the existing rows must survive a refused regenerate"


def test_regenerate_refuses_when_employee_taken_off_payroll(migrated_db):
    """`on_payroll = 0` drops the row just as `is_active = 0` does."""
    company_id = _company_id(migrated_db)
    run = _fresh_run(migrated_db, company_id)
    before = _item_emp_ids(migrated_db, run["id"])
    if not before:
        pytest.skip("no payroll items generated for this month in this DB")

    victim = sorted(before)[0]
    conn = sqlite3.connect(migrated_db)
    conn.execute("UPDATE employees SET on_payroll=0 WHERE id=?", (victim,))
    conn.commit()
    conn.close()

    with pytest.raises(ValueError):
        hr_mod.generate_run(YM, company_id, 'test', db_path=migrated_db)

    assert _item_emp_ids(migrated_db, run["id"]) == before


def test_regenerate_with_unchanged_employee_set_still_works(migrated_db):
    """Regression guard: the normal reason to regenerate (a rate or leave fix)
    must keep working."""
    company_id = _company_id(migrated_db)
    run = _fresh_run(migrated_db, company_id)
    before = _item_emp_ids(migrated_db, run["id"])
    if not before:
        pytest.skip("no payroll items generated for this month in this DB")

    again = hr_mod.generate_run(YM, company_id, 'test', db_path=migrated_db)

    assert again["id"] == run["id"]
    assert _item_emp_ids(migrated_db, run["id"]) == before


def test_advance_stamps_survive_a_refused_regenerate(migrated_db):
    """The destructive path un-stamped a departed employee's advance. A refused
    regenerate must leave every stamp exactly as it was."""
    company_id = _company_id(migrated_db)
    run = _fresh_run(migrated_db, company_id)
    before_ids = _item_emp_ids(migrated_db, run["id"])
    if not before_ids:
        pytest.skip("no payroll items generated for this month in this DB")

    conn = sqlite3.connect(migrated_db)
    stamps_before = dict(conn.execute(
        "SELECT id, deducted_in_run_id FROM salary_advances").fetchall())
    conn.execute("UPDATE employees SET is_active=0 WHERE id=?", (sorted(before_ids)[0],))
    conn.commit()
    conn.close()

    with pytest.raises(ValueError):
        hr_mod.generate_run(YM, company_id, 'test', db_path=migrated_db)

    conn = sqlite3.connect(migrated_db)
    stamps_after = dict(conn.execute(
        "SELECT id, deducted_in_run_id FROM salary_advances").fetchall())
    conn.close()
    assert stamps_after == stamps_before
