"""Migration 155 — ประกันสังคม ceiling 15,000 → 17,500 (effective 1 ม.ค. 2569).

The maximum wage base for a มาตรา 33 contribution is set by กฎกระทรวง, not by the
Act, so it moves without the statute changing. It was raised from 15,000 to
17,500 by a regulation published in ราชกิจจานุเบกษา on 12 ธ.ค. 2568, in force
1 ม.ค. 2569 — max contribution 750 → 875 at 5%. Two further steps are already
scheduled: 20,000 in 2572 and 23,000 in 2575.

Sendy seeded `hr_config.sso_max_base = 15000` in mig 054 and never revisited it,
so from Jan 2026 it under-deducts and under-remits for anyone earning above
15,000. See ~/FlawlessOS/wiki/legal/thai-social-security-contributions.md.

Blast radius on today's data is ZERO — only two employees are sso_enrolled
(หลุย 15,000 and บอล 13,000) and neither is above the old ceiling, so no current
payslip figure moves. That is asserted below so the claim is checked, not
assumed.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG = os.path.join(REPO, "data", "migrations", "155_sso_ceiling_2569.sql")
ROLLBACK = os.path.join(REPO, "data", "migrations", "155_sso_ceiling_2569.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


# ── the config value itself ──────────────────────────────────────────────────

def test_migration_raises_the_ceiling_to_17500(tmp_db_conn):
    tmp_db_conn.execute("UPDATE hr_config SET value='15000' WHERE key='sso_max_base'")
    tmp_db_conn.commit()

    _apply(tmp_db_conn, MIG)

    assert tmp_db_conn.execute(
        "SELECT value FROM hr_config WHERE key='sso_max_base'"
    ).fetchone()[0] == "17500"


def test_rate_and_floor_are_untouched(tmp_db_conn):
    """Only the ceiling moved. The floor and the 5% rate are separate knobs and
    this regulation did not change them."""
    before = dict(tmp_db_conn.execute(
        "SELECT key, value FROM hr_config WHERE key IN ('sso_rate','sso_min_base','day_divisor')"
    ).fetchall())

    _apply(tmp_db_conn, MIG)

    after = dict(tmp_db_conn.execute(
        "SELECT key, value FROM hr_config WHERE key IN ('sso_rate','sso_min_base','day_divisor')"
    ).fetchall())
    assert after == before


def test_rollback_restores_15000(tmp_db_conn):
    _apply(tmp_db_conn, MIG)
    _apply(tmp_db_conn, ROLLBACK)
    assert tmp_db_conn.execute(
        "SELECT value FROM hr_config WHERE key='sso_max_base'"
    ).fetchone()[0] == "15000"


def test_migration_is_rerunnable(tmp_db_conn):
    """A plain UPDATE keyed on `key` — applying twice must not drift."""
    _apply(tmp_db_conn, MIG)
    _apply(tmp_db_conn, MIG)
    rows = tmp_db_conn.execute(
        "SELECT value FROM hr_config WHERE key='sso_max_base'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "17500"


# ── what it means for the engine ─────────────────────────────────────────────

def test_engine_charges_875_at_the_new_ceiling(tmp_db_conn_hr_clean):
    """The point of the change: someone at or above 17,500 now contributes 875,
    not 750. This fails against the old ceiling."""
    import hr

    conn = tmp_db_conn_hr_clean
    _apply(conn, MIG)
    eid = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date, probation_days,
              sso_enrolled, diligence_allowance, is_active)
           VALUES ('T_SSO_CAP','sso cap','M',1,'2024-01-01',90,1,0,1)"""
    ).lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2024-01-01', 30000, 'initial')""", (eid,))
    conn.commit()

    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    item = conn.execute(
        "SELECT sso_employee, sso_employer FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid)).fetchone()

    assert item['sso_employee'] == 875.00, "17,500 × 5% — the 2569 ceiling"
    assert item['sso_employer'] == 875.00


def test_below_the_old_ceiling_is_unchanged(tmp_db_conn_hr_clean):
    """Regression guard: raising the ceiling must not touch anyone under it —
    which is every currently-enrolled employee."""
    import hr

    conn = tmp_db_conn_hr_clean
    _apply(conn, MIG)
    eid = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date, probation_days,
              sso_enrolled, diligence_allowance, is_active)
           VALUES ('T_SSO_LOW','under cap','F',1,'2024-01-01',90,1,0,1)"""
    ).lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2024-01-01', 13000, 'initial')""", (eid,))
    conn.commit()

    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    assert conn.execute(
        "SELECT sso_employee FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid)).fetchone()['sso_employee'] == 650.00   # 13,000 × 5%


def test_no_currently_enrolled_employee_is_affected(tmp_db_conn):
    """The blast-radius claim, checked rather than asserted in prose: every
    sso_enrolled employee sits at or below the OLD 15,000 ceiling, so raising it
    moves nobody. If a raise ever pushes someone past 15,000 this goes red and
    the reviewer has to think about back-contributions."""
    rows = tmp_db_conn.execute(
        """SELECT e.emp_code, s.monthly_salary
             FROM employees e
             JOIN employee_salary_history s ON s.id = (
                  SELECT id FROM employee_salary_history
                   WHERE employee_id = e.id ORDER BY effective_date DESC, id DESC LIMIT 1)
            WHERE e.is_active = 1 AND e.sso_enrolled = 1"""
    ).fetchall()
    assert rows, "expected at least one enrolled employee in the snapshot"
    over = [(r['emp_code'], r['monthly_salary']) for r in rows if r['monthly_salary'] > 15000]
    assert not over, (
        f"enrolled employees now above the old ceiling: {over} — raising it CHANGES "
        f"their contribution, so check whether back-contributions are owed from Jan 2026")
