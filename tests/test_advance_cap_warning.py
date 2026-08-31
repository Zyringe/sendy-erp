"""TDD — advance cap warning (P2, plan.md "P2 — advance cap warning").

Risky money math + a race + a schema (hr_config) seed → written FIRST, run
RED, then `hr.py` / `blueprints/cashbook.py` / migration 179 made to pass.

Three blockers already found in review (plan.md) — these tests exist
specifically to pin them, not to rediscover them:
  A. check-then-write race — the guard must recompute inside ONE
     BEGIN IMMEDIATE transaction spanning the read and the insert.
  B. the ceiling must NOT be sourced from a payroll_items row — most real
     advances are keyed before that month's run ever exists.
  C. the cap is keyed off advance_date's month, not "this month" — a
     backdated advance lands in the run for ITS month.

Sections:
  1. `_load_config` / migration 179 — advance_warn_pct default matches seed.
  2. `hr.advance_is_warn` — the yellow-bar boundary (pure function).
  3. `hr.collectable_this_month` — the red-line ceiling, computed from
     SOURCE (Blocker B), leave-adjusted (Major), no payroll_items involved.
  4. `hr.check_advance_cap` — the actual guard (Blocker C: keyed off the
     advance's own month; distinct warning for an already-finalized month).
  5. Route-level (`/cashbook/new`): refuse-without-confirm /
     confirm-then-write / ordinary-advance-unaffected / bulk grouping.
  6. Concurrency — the seam is BEFORE the first write (Blocker A).

Every test seeds its own state — `tmp_db_conn_hr_clean` clones the LIVE dev
DB WITH its data (`.claude/rules/erp-engineering-discipline.md`), so this
file deletes payroll/leave/advance rows before asserting on them, same as
tests/test_hr_payroll.py and tests/test_payroll_carry_forward.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import hr
import database


# ── shared helpers (mirror tests/test_hr_payroll.py / test_payroll_carry_forward.py) ──

def _mk_employee(conn, emp_code, full_name, start_date,
                 monthly_salary=15000.0, sso_enrolled=1, company_id=1):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, 'M', ?, ?, 90, ?, 0, 1)""",
        (emp_code, full_name, company_id, start_date, sso_enrolled),
    )
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, ?, ?, 'initial')""",
        (eid, start_date, monthly_salary),
    )
    conn.commit()
    return eid


def _add_advance(conn, employee_id, advance_date, amount, deducted_in_run_id=None):
    conn.execute(
        """INSERT INTO salary_advances
             (employee_id, advance_date, amount, raw_name, deducted_in_run_id)
           VALUES (?, ?, ?, 'test', ?)""",
        (employee_id, advance_date, amount, deducted_in_run_id),
    )
    conn.commit()


def _leave_type_id(conn, code):
    return conn.execute(
        "SELECT id FROM leave_types WHERE code=?", (code,)
    ).fetchone()[0]


def _add_leave(conn, employee_id, code, start, end, days, status='approved'):
    conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (employee_id, _leave_type_id(conn, code), start, end, days, status),
    )
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
# 1. hr_config seed / _load_config default parity (migration 179)
# ══════════════════════════════════════════════════════════════════════════

def test_advance_warn_pct_code_default_matches_migration(tmp_db_conn):
    """hr.py:203's fallback default MUST equal the value migration 179
    seeds, or a deploy where the code lands before the migration (or a DB
    that predates 179) silently applies a different threshold than a fully
    migrated one — the exact drift already live for sso_max_base (code
    default 15000, prod holds 17500; plan.md "Nit")."""
    c = tmp_db_conn
    seeded = c.execute(
        "SELECT value FROM hr_config WHERE key='advance_warn_pct'"
    ).fetchone()
    assert seeded is not None, "migration 179 must have seeded this row"
    seeded_value = float(seeded[0])

    # Code default: read _load_config on a DB with the row DELETED, so only
    # the code fallback can answer.
    c.execute("DELETE FROM hr_config WHERE key='advance_warn_pct'")
    cfg = hr._load_config(c)
    assert cfg["advance_warn_pct"] == seeded_value, (
        f"code default {cfg['advance_warn_pct']} != migrated value {seeded_value}")


def test_load_config_reads_advance_warn_pct_from_db_when_present(tmp_db_conn):
    c = tmp_db_conn
    c.execute("UPDATE hr_config SET value='0.75' WHERE key='advance_warn_pct'")
    cfg = hr._load_config(c)
    assert cfg["advance_warn_pct"] == 0.75


# ══════════════════════════════════════════════════════════════════════════
# 2. hr.advance_is_warn — pure boundary
# ══════════════════════════════════════════════════════════════════════════

def test_advance_warn_boundary_exactly_50pct_does_not_warn():
    # base_amount 10000, warn_pct 0.5 -> threshold 5000.00 exactly
    assert hr.advance_is_warn(5000.00, 10000.0, 0.5) is False


def test_advance_warn_boundary_one_satang_over_warns():
    assert hr.advance_is_warn(5000.01, 10000.0, 0.5) is True


def test_advance_warn_well_under_threshold_no_warn():
    assert hr.advance_is_warn(1000.0, 10000.0, 0.5) is False


# ══════════════════════════════════════════════════════════════════════════
# 3. hr.collectable_this_month — the red-line ceiling
# ══════════════════════════════════════════════════════════════════════════

def test_collectable_with_zero_payroll_runs_still_produces_a_ceiling(tmp_db_conn_hr_clean):
    """Blocker B regression: advance_history's OLD net_pay=None came from
    reading a payroll_items row that usually doesn't exist yet at advance-
    entry time. collectable_this_month must answer from SOURCE — measured
    2026-08-31, ฿8,400 of หลุย's August advances were keyed before run 8
    existed. Seed ZERO payroll_runs for the target month; the ceiling must
    still be a real positive number, not None/0 from a missing join."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CAPB', 'cap-blocker-b', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=1)
    assert c.execute(
        "SELECT COUNT(*) FROM payroll_runs WHERE year_month='2027-02'"
    ).fetchone()[0] == 0, "control: no run exists for the target month"

    status = hr.collectable_this_month(c, eid, '2027-02')
    assert status is not None
    # base 15000, sso = 15000*0.05 = 750, no carry, no unpaid leave
    assert status["salary_rate"] == 15000.0
    assert status["base_amount"] == 15000.0
    assert status["collectable"] == pytest.approx(15000.0 - 750.0, abs=0.01)


def test_collectable_unpaid_leave_lowers_the_ceiling_below_salary_rate(tmp_db_conn_hr_clean):
    """Major regression: an employee on unpaid leave earns less than
    salary_rate, so a salary_rate-based ceiling over-estimates what is
    collectable — exactly when the person is most at risk."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CAPLV', 'cap-leave', '2020-01-01',
                       monthly_salary=15000.0, sso_enrolled=0)
    # UNPAID leave type is unconditionally unpaid (see hr.py _compute_unpaid_days)
    _add_leave(c, eid, 'UNPAID', '2027-03-05', '2027-03-09', 5)

    with_leave = hr.collectable_this_month(c, eid, '2027-03')
    # control: same employee, a month with no leave at all
    no_leave = hr.collectable_this_month(c, eid, '2027-04')

    assert with_leave["collectable"] < no_leave["collectable"], (
        "unpaid leave must lower the ceiling, not leave it at salary_rate")
    assert with_leave["collectable"] == pytest.approx(
        15000.0 - round(15000.0 / 30 * 5, 2), abs=0.01)


def test_collectable_subtracts_carried_in(tmp_db_conn_hr_clean):
    """The ceiling must reuse the SAME carried_in source as _build_item
    (plan.md: 'extract the existing lines into a shared helper... or the two
    definitions of what this month can pay will drift')."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CAPCARRY', 'cap-carry', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=0)
    run = hr.generate_run('2027-05', 1, created_by=1, conn=c)
    c.execute("UPDATE payroll_items SET carried_out=950.0 WHERE run_id=? AND employee_id=?",
             (run['id'], eid))
    c.execute("UPDATE payroll_runs SET status='finalized' WHERE id=?", (run['id'],))
    c.commit()

    status = hr.collectable_this_month(c, eid, '2027-06')
    assert status["collectable"] == pytest.approx(15000.0 - 950.0, abs=0.01)


def test_collectable_unknown_employee_returns_none(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    assert hr.collectable_this_month(c, 999999, '2027-01') is None


# ══════════════════════════════════════════════════════════════════════════
# 4. hr.check_advance_cap — the actual guard (Blocker C)
# ══════════════════════════════════════════════════════════════════════════

def test_check_advance_cap_within_ceiling_does_not_raise(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_OK', 'cap-ok', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=1)
    # collectable ~= 14250; a 1000 advance is well within it
    hr.check_advance_cap(c, eid, '2027-02', 1000.0)  # must not raise


def test_check_advance_cap_over_ceiling_raises(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_OVER', 'cap-over', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=1)
    with pytest.raises(hr.AdvanceCapWarning):
        hr.check_advance_cap(c, eid, '2027-02', 20000.0)


def test_check_advance_cap_keyed_off_advance_own_month_not_current(tmp_db_conn_hr_clean):
    """Blocker C: a backdated advance is judged against ITS OWN month's
    ceiling, not whatever 'this month' happens to be. This test only proves
    the function accepts an explicit target_month distinct from any notion
    of 'today' — the route-level test below proves the ROUTE derives it
    from advance_date, not date.today()."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_BACKDATE', 'cap-backdate', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=1)
    # July ceiling ~14250; a 3100 backdated advance is within July's ceiling
    hr.check_advance_cap(c, eid, '2027-07', 3100.0)  # must not raise
    # but the SAME 3100 pushed against a month that already carries a full
    # ceiling-sized advance DOES raise
    _add_advance(c, eid, '2027-08-02', 13000.0)
    with pytest.raises(hr.AdvanceCapWarning):
        hr.check_advance_cap(c, eid, '2027-08', 3100.0)


def test_check_advance_cap_sums_existing_month_advances(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_SUM', 'cap-sum', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=1)
    _add_advance(c, eid, '2027-09-01', 10000.0)
    # ceiling ~14250; existing 10000 + new 5000 = 15000 > 14250 -> raises
    with pytest.raises(hr.AdvanceCapWarning):
        hr.check_advance_cap(c, eid, '2027-09', 5000.0)
    # control: the same new amount alone (no existing advance) does NOT raise
    eid2 = _mk_employee(c, 'T_SUM2', 'cap-sum-control', '2027-01-01',
                        monthly_salary=15000.0, sso_enrolled=1)
    hr.check_advance_cap(c, eid2, '2027-09', 5000.0)


def test_check_advance_cap_distinct_warning_for_finalized_target_month(tmp_db_conn_hr_clean):
    """Blocker C: an advance dated into an ALREADY-FINALIZED month raises —
    even a tiny amount well within the ceiling — because that money cannot
    be collected in its own month at all and will surface in a later run."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_FIN', 'cap-finalized', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=0)
    run = hr.generate_run('2027-07', 1, created_by=1, conn=c)
    hr.finalize_run(run['id'], conn=c)

    with pytest.raises(hr.AdvanceCapWarning, match='ปิดไปแล้ว'):
        hr.check_advance_cap(c, eid, '2027-07', 10.0)  # tiny, well within ceiling


def test_check_advance_cap_no_warning_control(tmp_db_conn_hr_clean):
    """Control: an ordinary small advance into an open (non-finalized,
    unfinalized-month) target with room to spare raises nothing at all."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CTRL', 'cap-control', '2027-01-01',
                       monthly_salary=15000.0, sso_enrolled=1)
    hr.check_advance_cap(c, eid, '2027-10', 500.0)  # must not raise


# ══════════════════════════════════════════════════════════════════════════
# 5. Route-level — /cashbook/new
# ══════════════════════════════════════════════════════════════════════════

ADVANCE_CATEGORY = 'เงินเดือน (เบิกล่วงหน้า)'


@pytest.fixture
def clean_migrated_db(tmp_db):
    """tmp_db with migration 179 confirmed applied (it already is — this
    worktree's live DB had it applied during this phase), AND payroll/leave/
    advance state wiped so route tests can assert on committed rows without
    colliding with the live-DB clone's real data (same shape as
    tmp_db_conn_hr_clean, but for a route test that needs its OWN fresh
    connection per request rather than one held open across the test)."""
    database.init_db()
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        DELETE FROM cashbook_transactions
         WHERE payroll_item_id IS NOT NULL
            OR payroll_run_id  IS NOT NULL
            OR salary_advance_id IS NOT NULL;
        DELETE FROM payroll_items;
        DELETE FROM salary_advances;
        DELETE FROM payroll_runs;
        DELETE FROM leave_requests;
    """)
    conn.commit()
    conn.close()
    return tmp_db


def _client_as_user(user_id, role):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _active_account(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0 ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("no active non-transfer cashbook account")
    return row["id"]


def _mk_route_employee(db, emp_code, full_name, start_date, monthly_salary=15000.0):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    eid = _mk_employee(conn, emp_code, full_name, start_date, monthly_salary=monthly_salary)
    conn.close()
    return eid


def test_over_cap_advance_refused_without_confirm_zero_rows_inserted(clean_migrated_db):
    account_id = _active_account(clean_migrated_db)
    eid = _mk_route_employee(clean_migrated_db, 'T_R_OVER', 'route-over', '2027-01-01', 15000.0)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2027-11-05",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(eid),
        "rows-0-amount": "20000",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 200, "over-cap must re-render the form (never redirect on refusal)"
    body = resp.get_data(as_text=True)
    assert "confirm_advance_cap" in body, "the confirm control must be offered"

    conn = sqlite3.connect(clean_migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE employee_id=?", (eid,)
    ).fetchone()[0]
    conn.close()
    assert n == 0, "no row may be written until confirmed"


def test_over_cap_advance_with_confirm_writes_exactly_one_row(clean_migrated_db):
    account_id = _active_account(clean_migrated_db)
    eid = _mk_route_employee(clean_migrated_db, 'T_R_CONF', 'route-confirm', '2027-01-01', 15000.0)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2027-11-05",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(eid),
        "rows-0-amount": "20000",
        "confirm_advance_cap": "1",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:800]

    conn = sqlite3.connect(clean_migrated_db)
    conn.row_factory = sqlite3.Row
    advs = conn.execute(
        "SELECT * FROM salary_advances WHERE employee_id=?", (eid,)
    ).fetchall()
    assert len(advs) == 1, "exactly one row"
    assert advs[0]["amount"] == 20000
    cb = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE salary_advance_id=?", (advs[0]["id"],)
    ).fetchone()
    conn.close()
    assert cb is not None, "linked cashbook row must exist"


def test_ordinary_small_advance_unaffected_exactly_one_row_inserted(clean_migrated_db):
    """Control (plan.md): several render/redirect exits precede the insert
    (blueprints/cashbook.py `_upsert_category`/duplicate-check paths) so a
    weaker assertion could pass with nothing actually written."""
    account_id = _active_account(clean_migrated_db)
    eid = _mk_route_employee(clean_migrated_db, 'T_R_SMALL', 'route-small', '2027-01-01', 15000.0)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2027-11-05",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(eid),
        "rows-0-amount": "500",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:800]

    conn = sqlite3.connect(clean_migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE employee_id=?", (eid,)
    ).fetchone()[0]
    conn.close()
    assert n == 1, "exactly one row must have actually been inserted"


def test_backdated_advance_into_already_finalized_month_refused(clean_migrated_db):
    """Blocker C route-level regression: the route must key the cap off the
    SUBMITTED advance_date's month, not the request's own date.today()."""
    account_id = _active_account(clean_migrated_db)
    eid = _mk_route_employee(clean_migrated_db, 'T_R_BACK', 'route-backdate', '2027-01-01', 15000.0)

    conn = sqlite3.connect(clean_migrated_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    import hr as hr_mod
    run = hr_mod.generate_run('2027-06', 1, created_by=1, conn=conn)
    hr_mod.finalize_run(run['id'], conn=conn)
    conn.close()

    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2027-07-15",  # today's entry date — NOT the target month
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(eid),
        "rows-0-amount": "10",
        "rows-0-txn_date": "2027-06-05",  # backdated INTO the finalized month
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 200, "must refuse (re-render), not silently insert"
    body = resp.get_data(as_text=True)
    assert "ปิดไปแล้ว" in body, "the distinct finalized-month message must reach the page"

    conn = sqlite3.connect(clean_migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE employee_id=?", (eid,)
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_bulk_rows_same_employee_same_month_evaluated_as_sum(clean_migrated_db):
    """plan.md step 5: bulk submissions evaluate the SUM per (employee,
    target month) in one request, not per row — two rows individually
    under the ceiling can together exceed it."""
    account_id = _active_account(clean_migrated_db)
    eid = _mk_route_employee(clean_migrated_db, 'T_R_BULK', 'route-bulk', '2027-01-01', 15000.0)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2027-11-05",
        "account_id": str(account_id),
        "bulk_mode": "1",
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(eid),
        "rows-0-amount": "8000",
        "rows-1-direction": "expense",
        "rows-1-category": ADVANCE_CATEGORY,
        "rows-1-employee_id": str(eid),
        # deliberately NOT 8000 again: an identical second row would collide
        # with the PRE-EXISTING in-batch duplicate guard
        # (_find_duplicate_indices keys on effective_date/direction/category/
        # user_category/amount) and return 200 for THAT reason, making this
        # test pass vacuously without ever exercising the cap sum at all.
        "rows-1-amount": "8100",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 200, "the SUM (16100) exceeds the ~14250 ceiling"
    body = resp.get_data(as_text=True)
    assert "confirm_advance_cap" in body, (
        "must be refused by the CAP guard specifically, not the unrelated "
        "duplicate-row guard (which also renders 200)")

    conn = sqlite3.connect(clean_migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE employee_id=?", (eid,)
    ).fetchone()[0]
    conn.close()
    assert n == 0, "neither row may be written until confirmed"


# ══════════════════════════════════════════════════════════════════════════
# 6. Concurrency — the seam is BEFORE the first write (Blocker A)
# ══════════════════════════════════════════════════════════════════════════

def _concurrent_advance_insert_blocked(db_path, employee_id, advance_date, amount):
    other = sqlite3.connect(db_path, timeout=0.1)
    try:
        other.execute(
            "INSERT INTO salary_advances (employee_id, advance_date, amount)"
            " VALUES (?,?,?)", (employee_id, advance_date, amount))
        other.commit()
        return False
    except sqlite3.OperationalError as e:
        return "locked" in str(e).lower()
    finally:
        other.close()


def test_advance_post_holds_the_write_lock_across_the_cap_check(clean_migrated_db, monkeypatch):
    """The cap read and the salary_advances insert must be ONE BEGIN
    IMMEDIATE transaction. Under gunicorn -w 2 a second worker could
    otherwise insert a competing advance between the read and the write.

    The seam is patched at hr_mod._begin_immediate, BEFORE the first write —
    a probe placed after it would be excluded either way and pass with the
    fix removed (same shape as tests/test_hr_payroll.py's finalize test)."""
    import hr as hr_mod
    account_id = _active_account(clean_migrated_db)
    eid = _mk_route_employee(clean_migrated_db, 'T_R_RACE', 'route-race', '2027-01-01', 15000.0)

    seen = {}
    real = hr_mod._begin_immediate

    def probe(c):
        seen['blocked'] = _concurrent_advance_insert_blocked(
            clean_migrated_db, eid, '2027-12-10', 500.0)
        return real(c)

    monkeypatch.setattr(hr_mod, '_begin_immediate', probe)

    cl = _client_as_user(1, "admin")
    form = {
        "txn_date": "2027-12-05",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(eid),
        "rows-0-amount": "500",
    }
    resp = cl.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:800]

    assert seen, "the probe never ran — the lock seam never fired for an advance row"
    assert seen['blocked'] is True, (
        "a concurrent writer got in between the cap check and the insert")
