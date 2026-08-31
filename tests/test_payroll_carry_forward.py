"""TDD — payroll carry-forward (P1a: migration 178 + `inventory_app/hr.py`).

Risky money math + a schema migration → written FIRST, run RED, then
`hr.py`/the migration made to pass. See
projects/payroll-carry-forward/plan.md for the full design.

Trigger case: prod payroll_runs id 8 (2026-08, draft), employee EMP004 หลุย —
net_pay computed to -฿950 (a ฿15,200 advance against a ฿15,000 salary, SSO
฿750). Finalizing as-is would stamp the FULL ฿15,200 of advances "collected",
pay ฿0, and silently write off the ฿950 the moment September starts clean.

Design (plan.md "Data model"): `payroll_items.carried_in` / `carried_out` are
DERIVED, never stamped — `generate_run` DELETEs and re-INSERTs every item on
every (re)generate (hr.py:1245, "preserving nothing"), so anything keyed by
hand into a draft dies on the next regenerate. `carried_in` for a run is read
from the `carried_out` of that employee's most recent FINALIZED prior run —
ordered by `year_month`, NOT by `id` (prod's real shape: run 3 = 2026-05, run
4 = 2026-04 — ids are not chronological).

Two sections:
  1. Migration mechanics — `pre178_conn` fixture (same shape as test_mig157's
     `pre157_conn`): detects whether 178 is already live on the cloned DB
     (it is, on THIS worktree, applied for real during P1a) and rolls it back
     first, so the tests exercise the raw forward/rollback SQL either way.
  2. Engine tests — plain `tmp_db_conn` (178 is already live on the DB it
     clones), matching tests/test_hr_payroll.py's own style.

Every test seeds its own state — `tmp_db_conn` clones the LIVE dev DB WITH
its data (`.claude/rules/erp-engineering-discipline.md`).
"""
import os
import sqlite3

import pytest

import hr

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_178 = os.path.join(REPO, "data", "migrations", "178_payroll_carry_forward.sql")
ROLLBACK_178 = os.path.join(
    REPO, "data", "migrations", "178_payroll_carry_forward.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


# ── shared helpers (mirror tests/test_hr_payroll.py) ────────────────────────

def _mk_employee(conn, emp_code, full_name, start_date,
                 monthly_salary=15000.0, sso_enrolled=0, company_id=1):
    """sso_enrolled=0 by default (unlike test_hr_payroll.py's 1) — these
    tests are about carry arithmetic, not SSO; keeping SSO at 0 means
    gross == base_amount == monthly_salary with no other moving parts."""
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


def _add_advance(conn, employee_id, advance_date, amount):
    conn.execute(
        """INSERT INTO salary_advances
             (employee_id, advance_date, amount, raw_name)
           VALUES (?, ?, ?, 'test')""",
        (employee_id, advance_date, amount),
    )
    conn.commit()


def _item(conn, run_id, employee_id):
    r = conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run_id, employee_id),
    ).fetchone()
    assert r is not None, "payroll_items row missing"
    return r


def _plant_finalized_run(conn, employee_id, year_month, carried_out,
                         company_id=1):
    """Directly plant a FINALIZED prior run with one payroll_items row
    carrying `carried_out`, bypassing generate_run/finalize_run entirely —
    this is the SOURCE row _build_item's carried_in query reads, and must be
    constructible independently of the code under test."""
    cur = conn.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, finalized_at)
           VALUES (?, ?, 'finalized', date('now'), datetime('now'))""",
        (year_month, company_id),
    )
    run_id = cur.lastrowid
    conn.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              carried_out)
           VALUES (?, ?, 0, 0, 0, 0, ?)""",
        (run_id, employee_id, carried_out),
    )
    conn.commit()
    return run_id


def _plant_draft_run(conn, employee_id, year_month, carried_out,
                     company_id=1):
    cur = conn.execute(
        """INSERT INTO payroll_runs (year_month, company_id, status, run_date)
           VALUES (?, ?, 'draft', date('now'))""",
        (year_month, company_id),
    )
    run_id = cur.lastrowid
    conn.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              carried_out)
           VALUES (?, ?, 0, 0, 0, 0, ?)""",
        (run_id, employee_id, carried_out),
    )
    conn.commit()
    return run_id


# ═══════════════════════════════════════════════════════════════════════════
# Section 1 — migration mechanics
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def pre178_conn(tmp_db_conn):
    """tmp_db_conn is a fresh copy of the live LOCAL dev DB. This worktree's
    live DB already has 178 applied for real (done during P1a), so every
    fresh copy carries carried_in/carried_out — re-applying MIG_178 would
    raise 'duplicate column'. Detect and roll back first so this fixture (and
    the tests built on it) work whether or not 178 is already live."""
    cols = {r["name"] for r in tmp_db_conn.execute("PRAGMA table_info(payroll_items)")}
    if "carried_in" in cols:
        # Normalise to all-zero BEFORE invoking the rollback — the rollback
        # correctly REFUSES on non-zero carry data (there is nothing to fold
        # it into, unlike 157's wht_amount merge), and a real dev-DB row
        # carrying a nonzero value would abort fixture construction before
        # any test runs.
        tmp_db_conn.execute(
            "UPDATE payroll_items SET carried_in = 0, carried_out = 0 "
            "WHERE carried_in <> 0 OR carried_out <> 0")
        tmp_db_conn.commit()
        _apply(tmp_db_conn, ROLLBACK_178)
    return tmp_db_conn


def test_migration_adds_columns_with_zero_default(pre178_conn):
    _apply(pre178_conn, MIG_178)
    row = pre178_conn.execute("SELECT id FROM payroll_items LIMIT 1").fetchone()
    assert row is not None, "fixture DB has no payroll_items rows to check"
    r = pre178_conn.execute(
        "SELECT carried_in, carried_out FROM payroll_items WHERE id = ?",
        (row["id"],)).fetchone()
    assert (r["carried_in"], r["carried_out"]) == (0, 0)


def test_migration_check_constraint_refuses_negative(pre178_conn):
    """The column itself must refuse it, not only hr.py's Python guard —
    direct SQL, a future migration, or a new write path would otherwise slip
    past. Mirrors test_negative_payroll_item_wht_is_refused_by_the_schema."""
    _apply(pre178_conn, MIG_178)
    iid = pre178_conn.execute("SELECT id FROM payroll_items LIMIT 1").fetchone()["id"]

    with pytest.raises(sqlite3.IntegrityError):
        pre178_conn.execute(
            "UPDATE payroll_items SET carried_in = -1 WHERE id = ?", (iid,))
    with pytest.raises(sqlite3.IntegrityError):
        pre178_conn.execute(
            "UPDATE payroll_items SET carried_out = -1 WHERE id = ?", (iid,))

    # 0 and positive both fine
    pre178_conn.execute(
        "UPDATE payroll_items SET carried_in = 0, carried_out = 0 WHERE id = ?",
        (iid,))
    pre178_conn.execute(
        "UPDATE payroll_items SET carried_in = 5, carried_out = 3 WHERE id = ?",
        (iid,))
    r = pre178_conn.execute(
        "SELECT carried_in, carried_out FROM payroll_items WHERE id = ?",
        (iid,)).fetchone()
    assert (r["carried_in"], r["carried_out"]) == (5, 3)


def test_migration_audit_triggers_capture_carry_columns(pre178_conn):
    """⚠ Break-it-once target: delete the carried_in/carried_out lines from
    ONE of the three trigger recreations in the migration file and this test
    must go red for the specific column that trigger no longer captures."""
    _apply(pre178_conn, MIG_178)
    before = pre178_conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    iid = pre178_conn.execute("SELECT id FROM payroll_items LIMIT 1").fetchone()["id"]

    pre178_conn.execute(
        "UPDATE payroll_items SET carried_in = 12.5, carried_out = 3.25 WHERE id = ?",
        (iid,))
    pre178_conn.commit()

    after = pre178_conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before + 1, "the UPDATE trigger did not fire for a carry-only change"

    body = pre178_conn.execute(
        "SELECT changed_fields FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()["changed_fields"]
    assert '"carried_in"' in body and '"carried_out"' in body, (
        f"audit body missing carry columns: {body}")


def test_rollback_is_byte_identical_and_drops_columns(pre178_conn):
    before = {
        r["name"]: r["sql"] for r in pre178_conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='payroll_items'")
    }
    assert before, "fixture has no payroll_items-related sqlite_master rows"

    _apply(pre178_conn, MIG_178)
    _apply(pre178_conn, ROLLBACK_178)

    after = {
        r["name"]: r["sql"] for r in pre178_conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='payroll_items'")
    }
    assert after == before, "payroll_items schema not byte-identical after rollback"

    cols = {r["name"] for r in pre178_conn.execute("PRAGMA table_info(payroll_items)")}
    assert "carried_in" not in cols and "carried_out" not in cols


def test_rollback_refuses_when_carry_data_present(pre178_conn):
    _apply(pre178_conn, MIG_178)
    iid = pre178_conn.execute("SELECT id FROM payroll_items LIMIT 1").fetchone()["id"]
    pre178_conn.execute(
        "UPDATE payroll_items SET carried_out = 950 WHERE id = ?", (iid,))
    pre178_conn.commit()

    with pytest.raises(sqlite3.IntegrityError) as e:
        _apply(pre178_conn, ROLLBACK_178)
    assert "rollback refused" in str(e.value)

    # data untouched
    r = pre178_conn.execute(
        "SELECT carried_out FROM payroll_items WHERE id = ?", (iid,)).fetchone()
    assert r["carried_out"] == 950

    # connection left clean (RAISE(ROLLBACK), not ABORT — see rollback header)
    assert not pre178_conn.in_transaction, "refusal left a transaction open"
    assert [r[0] for r in pre178_conn.execute(
        "SELECT name FROM sqlite_temp_master")] == [], "refusal stranded TEMP objects"

    # retry on the SAME connection gets the same refusal, not a stale-state error
    with pytest.raises(sqlite3.IntegrityError) as e2:
        _apply(pre178_conn, ROLLBACK_178)
    assert "rollback refused" in str(e2.value)


def test_forward_rollback_forward_round_trips(pre178_conn):
    _apply(pre178_conn, MIG_178)
    _apply(pre178_conn, ROLLBACK_178)
    _apply(pre178_conn, MIG_178)  # must not raise "duplicate column"
    cols = {r["name"] for r in pre178_conn.execute("PRAGMA table_info(payroll_items)")}
    assert {"carried_in", "carried_out"} <= cols


def test_migration_self_records_in_applied_migrations(pre178_conn):
    pre178_conn.execute(
        "DELETE FROM applied_migrations WHERE filename = '178_payroll_carry_forward.sql'")
    pre178_conn.commit()
    _apply(pre178_conn, MIG_178)
    assert pre178_conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename = '178_payroll_carry_forward.sql'").fetchone()[0] == 1
    _apply(pre178_conn, ROLLBACK_178)
    assert pre178_conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename = '178_payroll_carry_forward.sql'").fetchone()[0] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — engine (hr.py). 178 is already live on the DB tmp_db_conn
# clones (applied for real to this worktree during P1a), so no _apply() here.
# ═══════════════════════════════════════════════════════════════════════════

def test_no_prior_finalized_run_carried_in_zero(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_CF1', 'no prior run', '2025-01-01')
    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    items = tmp_db_conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid)).fetchall()
    # CONTROL first: the fixture actually generated an item for this employee
    # — the active-employee filter could exclude it and this test would pass
    # on nothing.
    assert len(items) == 1, f"expected exactly 1 item, got {len(items)}"
    it = items[0]
    assert it['carried_in'] == 0
    assert it['carried_out'] == 0
    assert it['net_pay'] == it['gross']


def test_prior_finalized_carry_out_becomes_this_run_carried_in(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_CF2', 'carries in', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 950.0)

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    assert it['carried_in'] == 950.0
    assert it['gross'] == 15000.0
    # net drops by EXACTLY the carried_in amount (no SSO/advances/leave here)
    assert it['net_pay'] == 15000.0 - 950.0


def test_negative_net_clamps_to_zero_and_sets_carried_out(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_CF3', 'goes negative', '2025-01-01',
                       monthly_salary=15000.0)
    # carried_in (15200) exceeds gross (15000) -> net_before_carry = -200
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 15200.0)

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    assert it['carried_in'] == 15200.0
    assert it['net_pay'] == 0.0
    assert it['carried_out'] == 200.0


def test_non_negative_net_carried_out_zero(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_CF4', 'stays positive', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 100.0)  # small, won't go negative

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    items = tmp_db_conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid)).fetchall()
    assert len(items) == 1, "control: item must actually be generated"
    it = items[0]
    assert it['carried_out'] == 0.0
    assert it['net_pay'] == 15000.0 - 100.0


def test_regenerate_stability_carried_in_does_not_drift(tmp_db_conn):
    """Codex: must seed a NON-ZERO carried_out, else this passes at 0==0
    both times and pins nothing."""
    eid = _mk_employee(tmp_db_conn, 'T_CF5', 'regen stable', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 950.0)

    run1 = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it1 = _item(tmp_db_conn, run1['id'], eid)
    assert it1['carried_in'] == 950.0

    run2 = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    assert run2['id'] == run1['id'], "regenerate must reuse the same draft run"
    it2 = _item(tmp_db_conn, run2['id'], eid)
    assert it2['carried_in'] == 950.0


def test_draft_prior_run_does_not_count(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_CF6', 'draft prior', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_draft_run(tmp_db_conn, eid, '2025-12', 500.0)

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['carried_in'] == 0.0


def test_ordering_by_year_month_not_by_run_id(tmp_db_conn):
    """Prod's real shape: run 3 = 2026-05, run 4 = 2026-04 (lower id, LATER
    month). Plant the same inversion: the LOWER-id run is the LATER month."""
    eid = _mk_employee(tmp_db_conn, 'T_CF7', 'ordering', '2025-01-01',
                       monthly_salary=15000.0)
    run_later_month_lower_id = _plant_finalized_run(tmp_db_conn, eid, '2026-01', 300.0)
    run_earlier_month_higher_id = _plant_finalized_run(tmp_db_conn, eid, '2025-12', 999.0)
    assert run_earlier_month_higher_id > run_later_month_lower_id, (
        "fixture inversion did not happen — the id order must be the REVERSE "
        "of the year_month order for this test to pin anything")

    run = hr.generate_run('2026-02', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # the LATER month (2026-01, carried_out=300) must win, not the higher id
    # (2025-12, carried_out=999) and not the higher amount either.
    assert it['carried_in'] == 300.0


def test_gap_month_is_bridged_not_dropped(tmp_db_conn):
    """Jan carried_out=950, NO Feb run at all, generate March -> carried_in
    still 950. Guards against a `previous_ym()`-based reimplementation, which
    is strictly-previous-month and would silently drop the debt at the gap.

    ⚠ Precondition, not an assumption: DELETE any run in the gap month and
    assert its absence BEFORE seeding, since tmp_db_conn clones the live dev
    DB, which drifts over time as the team runs more real payroll months."""
    tmp_db_conn.execute(
        "DELETE FROM payroll_items WHERE run_id IN "
        "(SELECT id FROM payroll_runs WHERE company_id=1 AND year_month='2026-02')")
    tmp_db_conn.execute(
        "DELETE FROM payroll_runs WHERE company_id=1 AND year_month='2026-02'")
    tmp_db_conn.commit()
    gap_count = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM payroll_runs WHERE company_id=1 AND year_month='2026-02'"
    ).fetchone()[0]
    assert gap_count == 0, "precondition failed: a 2026-02 run still exists"

    eid = _mk_employee(tmp_db_conn, 'T_CF8', 'gap month', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2026-01', 950.0)
    # deliberately NO run planted for 2026-02

    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['carried_in'] == 950.0


def test_hand_edit_does_not_survive_regenerate(tmp_db_conn):
    """ไม่มีการยกหนี้ให้ (plan.md, Put's ruling) — there is no forgiveness
    FEATURE, and this pins that a raw hand edit isn't one either: carried_in
    is DERIVED at every generate, never read back from the row it wrote last
    time, so attempting to zero it out by hand is silently undone the next
    time the run is (re)generated."""
    eid = _mk_employee(tmp_db_conn, 'T_CF9', 'hand edit', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 950.0)

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['carried_in'] == 950.0, "control: carry must actually be applied first"

    # attempted hand "forgiveness"
    tmp_db_conn.execute(
        "UPDATE payroll_items SET carried_in = 0, net_pay = gross WHERE id = ?",
        (it['id'],))
    tmp_db_conn.commit()
    tampered = _item(tmp_db_conn, run['id'], eid)
    assert tampered['carried_in'] == 0.0, "control: the tamper must actually land"

    run2 = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    after = _item(tmp_db_conn, run2['id'], eid)
    assert after['carried_in'] == 950.0, "hand edit survived a regenerate"
    assert after['net_pay'] == 15000.0 - 950.0


def test_carry_clamped_item_refuses_salary_payment(tmp_db_conn):
    """Regression pin for post_salary_payment's existing `net_pay <= 0`
    refusal (hr.py:1571) — not new code, but the carry clamp is a NEW way to
    reach net_pay==0, and this proves the guard still holds for it."""
    eid = _mk_employee(tmp_db_conn, 'T_CF10', 'clamped no-pay', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 15200.0)  # net -> 0 after clamp

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['net_pay'] == 0.0, "control: this item must actually be clamped to 0"

    hr.finalize_run(run['id'], conn=tmp_db_conn)

    acct = tmp_db_conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer=0 AND is_active=1 "
        "LIMIT 1").fetchone()[0]

    with pytest.raises(ValueError):
        hr.post_salary_payment(it['id'], acct, '2026-01-28', 'test', conn=tmp_db_conn)

    linked = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_item_id = ?",
        (it['id'],)).fetchone()[0]
    assert linked == 0, "no cashbook_transactions row must exist after the refusal"


def test_other_additions_zeroes_carry_side_effect(tmp_db_conn):
    """⚠ Documented emergent side effect (plan.md, not a bug to fix): keying
    other_additions (an existing admin field for a genuine allowance) raises
    gross and can silently zero a real carried_out. Pinned so a future reader
    is not surprised, per the plan's explicit instruction."""
    eid = _mk_employee(tmp_db_conn, 'T_CF11', 'other additions', '2025-01-01',
                       monthly_salary=15000.0)
    # carried_in (15200) exceeds gross (15000) -> starts CLAMPED
    _plant_finalized_run(tmp_db_conn, eid, '2025-12', 15200.0)

    run = hr.generate_run('2026-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['carried_in'] == 15200.0
    assert it['net_pay'] == 0.0 and it['carried_out'] == 200.0, (
        "control: this item must actually start clamped for the side effect "
        "to have anything to undo")

    updated = hr.update_payroll_item(
        it['id'], other_additions=5000.0, conn=tmp_db_conn)
    # gross rises by the addition (20000); carried_in (15200) no longer
    # exceeds it, so the item is no longer clamped and carried_out silently
    # drops to 0 — the exact side effect the plan documents, not a bug.
    assert updated['gross'] == 20000.0
    assert updated['carried_in'] == 15200.0, "carried_in must be untouched by an admin edit"
    assert updated['net_pay'] == 20000.0 - 15200.0
    assert updated['carried_out'] == 0.0, (
        "the documented side effect: an unrelated admin credit silently "
        "erased a real carried-forward debt")
