"""Migration 157 — WHT (ภาษีหัก ณ ที่จ่าย) single source of truth.

TDD — written FIRST, run RED, then hr.py/the migration made to pass.
See projects/sendy-wht-single-source/plan.md for the full design.

Put's ฿32/month PIT withholding lived in two unconnected places: the payroll
report's `company_profile.json.tax_schedule` config and
`payroll_items.other_deductions` — they drifted silently. This migration adds
`employee_wht_history` (effective-dated, mirrors `employee_salary_history`)
and `payroll_items.wht_amount` (dedicated column, not a reuse of
`other_deductions`), backfills the two manually-keyed rows that exist, and
seeds history so the payroll engine applies WHT automatically going forward.

Fixture `pre157_conn`: a copy of the live local dev DB (`tmp_db_conn`), reset
to the pre-157 state if migration 157 has ALREADY been applied there for real
(defensive — same shape as `pre134_conn` in test_mig134, generalised to a
data-carrying copy since tests 5/6 need real payroll_items rows: the 2 WHT
rows for employee_id=1 the local dev DB actually carries, runs 3 and 6 —
run 7 is deliberately still un-fixed locally, see plan.md "Live data the
backfill must handle").
"""
import os
import sqlite3

import pytest

import hr

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_157 = os.path.join(REPO, "data", "migrations", "157_wht_single_source.sql")
ROLLBACK_157 = os.path.join(
    REPO, "data", "migrations", "157_wht_single_source.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre157_conn(tmp_db_conn):
    """tmp_db_conn is a fresh copy of the live local DB. Once 157 has been
    applied to that live DB for real (e.g. after this PR merges and someone
    boots the app), every fresh copy already carries employee_wht_history +
    payroll_items.wht_amount, and re-applying MIG_157 raises 'duplicate
    column' / 'table already exists'. Detect and roll back first so this
    fixture — and the tests built on it — work in EITHER state."""
    cols = {r["name"] for r in tmp_db_conn.execute("PRAGMA table_info(payroll_items)")}
    if "wht_amount" in cols:
        # ⚠ Normalise BEFORE invoking the rollback. Post-157 an admin can key an
        # ordinary deduction onto a row that already carries the auto-applied
        # WHT; the rollback then (correctly) REFUSES, and fixture construction
        # aborts before any test runs — the tests would report "rollback
        # refused" for every case, including ones about something else entirely
        # (Codex round 4, reproduced by planting such a row).
        # Folding the two into one amount is exactly what the pre-157 schema
        # represented, so this is a faithful pre-state, and none of these rows
        # survive: _seed_pre157() wipes every WHT marking and plants its own.
        tmp_db_conn.execute(
            "UPDATE payroll_items "
            "   SET other_deductions = other_deductions + wht_amount, "
            "       wht_amount = 0 "
            " WHERE wht_amount > 0 AND other_deductions > 0")
        tmp_db_conn.commit()
        _apply(tmp_db_conn, ROLLBACK_157)
    return tmp_db_conn


def _seed_pre157(conn):
    """Build a DETERMINISTIC pre-157 WHT population and return its oracle.

    Why this exists (Codex round 3): the migration/rollback tests used to read
    whatever WHT rows the live dev DB happened to carry. Two problems, both real:

    1. `pre157_conn` builds its pre-157 state by running THE ROLLBACK UNDER TEST.
       So a rollback mutation makes the round-trip test go red inside the fixture,
       before the subject is ever reached — red for the wrong reason (shapes #1
       and #6 in .claude/rules/verification-discipline.md).
    2. Inherited rows drift. A refresh of the local DB from prod changes the
       employees, months and amounts the oracle is derived from, and a real row
       carrying BOTH a WHT and another deduction would trip the rollback's
       refusal inside the fixture.

    So: wipe every inherited WHT marking first, then plant exactly the cases the
    contract has to satisfy —
      * a pure WHT row (the ordinary case),
      * the same employee across TWO months (proves "earliest run wins"),
      * a same-month tie across two runs with DIFFERENT amounts (proves the
        `ROW_NUMBER(... ORDER BY year_month, pi.id)` tie-break, not MIN()),
      * a second employee, so per-employee partitioning is exercised.
    The mixed WHT+deduction case is planted per-test, since it must be absent
    for the round-trip and present for the refusal.

    Returns {employee_id: (effective_date, monthly_wht)} — the seed the migration
    is expected to produce, derived the SAME way the migration derives it.
    """
    conn.execute(
        "UPDATE payroll_items SET other_deductions = 0, other_deductions_note = NULL "
        "WHERE other_deductions_note = 'ภาษีหัก ณ ที่จ่าย'")

    # Test-OWNED companies, so `payroll_runs UNIQUE(year_month, company_id)` can
    # never collide with a real run no matter which months the live DB carries
    # (hardcoding far-future months only defers the collision — Codex round 4).
    def company(code):
        cur = conn.execute(
            "INSERT INTO companies (code, name_th, short_name, is_active) VALUES (?,?,?,1)",
            (code, 'ทดสอบ ' + code, code))
        return cur.lastrowid

    cA, cB = company('TSTA'), company('TSTB')
    e1 = _mk_employee(conn, 'T_SEED1', 'seed one', '2025-01-01')
    e2 = _mk_employee(conn, 'T_SEED2', 'seed two', '2025-01-01')

    def run(year_month, company_id):
        cur = conn.execute(
            "INSERT INTO payroll_runs (year_month, company_id, status) VALUES (?,?,'draft')",
            (year_month, company_id))
        return cur.lastrowid

    def item(run_id, eid, wht):
        cur = conn.execute(
            """INSERT INTO payroll_items
                 (run_id, employee_id, salary_rate, base_amount, unpaid_leave_days,
                  unpaid_leave_deduction, diligence_allowance, diligence_forfeited,
                  bonus, other_additions, other_deductions, other_deductions_note,
                  salary_advance_deduction, sso_employee, sso_employer,
                  commission_amount, gross, net_pay)
               VALUES (?,?,15000,15000,0,0,0,0,0,0,?, 'ภาษีหัก ณ ที่จ่าย',0,0,0,0,15000,?)""",
            (run_id, eid, wht, 15000 - wht))
        return cur.lastrowid

    # e1: earliest month ฿20, later month ฿25 — "earliest run wins"
    first = item(run('2026-03', cA), e1, 20.0)
    item(run('2026-04', cA), e1, 25.0)
    # e1 SAME-month tie, different amount, inserted AFTER `first`. Only the
    # `pi.id ASC` half of the tie-break decides between ฿20 and ฿77.
    tie = item(run('2026-03', cB), e1, 77.0)
    assert tie > first, "the tie row must be inserted after the winner for this to test anything"
    # e2: single month, third amount
    item(run('2026-05', cA), e2, 15.0)
    conn.commit()

    # ⚠ LITERAL oracle. Deriving it with the migration's own
    # ROW_NUMBER(... year_month, pi.id) made the test a second copy of the
    # implementation: flipping BOTH to `pi.id DESC` left all five tests green
    # while blessing ฿77 (Codex round 4, reproduced). These are the values a
    # human decided are correct, written out.
    oracle = {e1: ('2026-03-01', 20.0), e2: ('2026-05-01', 15.0)}
    assert len(oracle) == 2, f"seed oracle should cover 2 employees, got {oracle}"
    return oracle


# ── helpers (mirror tests/test_hr_payroll.py's _mk_employee / _item) ───────

def _mk_employee(conn, emp_code, full_name, start_date,
                 monthly_salary=15000.0, sso_enrolled=0, company_id=1):
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


def _add_wht(conn, employee_id, effective_date, monthly_wht, reason='initial'):
    conn.execute(
        """INSERT INTO employee_wht_history
             (employee_id, effective_date, monthly_wht, reason)
           VALUES (?, ?, ?, ?)""",
        (employee_id, effective_date, monthly_wht, reason),
    )
    conn.commit()


def _item(conn, run_id, employee_id):
    r = conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run_id, employee_id),
    ).fetchone()
    assert r is not None, "payroll_items row missing"
    return r


# ── test 1: resolve_wht ─────────────────────────────────────────────────────

def test_resolve_wht_picks_latest_row_at_or_before_month_end(pre157_conn):
    _apply(pre157_conn, MIG_157)
    eid = _mk_employee(pre157_conn, 'T_WHT1', 'wht resolve test', '2026-01-01')
    _add_wht(pre157_conn, eid, '2026-05-01', 32.0, 'initial')
    _add_wht(pre157_conn, eid, '2026-08-01', 50.0, 'adjust')

    # before the first effective date → None
    assert hr.resolve_wht(eid, '2026-04', conn=pre157_conn) is None
    # the month of the first row → picks it
    row = hr.resolve_wht(eid, '2026-05', conn=pre157_conn)
    assert row['monthly_wht'] == 32.0
    # between the two rows → still the first (latest <= month end)
    row = hr.resolve_wht(eid, '2026-07', conn=pre157_conn)
    assert row['monthly_wht'] == 32.0
    # the month of the second (adjusted) row → picks it
    row = hr.resolve_wht(eid, '2026-08', conn=pre157_conn)
    assert row['monthly_wht'] == 50.0


# ── test 2: generate_run applies wht_amount and reduces net_pay ────────────

def test_generate_run_applies_wht_amount_and_reduces_net_pay(pre157_conn):
    _apply(pre157_conn, MIG_157)
    eid = _mk_employee(pre157_conn, 'T_WHT2', 'wht applied', '2026-01-01')
    _add_wht(pre157_conn, eid, '2026-01-01', 32.0, 'initial')

    run = hr.generate_run('2027-01', 1, created_by=1, conn=pre157_conn)
    it = _item(pre157_conn, run['id'], eid)

    assert it['wht_amount'] == 32.0
    assert it['net_pay'] == it['gross'] - 32.0


# ── test 3: no WHT row → 0 and unchanged net_pay ────────────────────────────

def test_generate_run_defaults_wht_zero_with_no_history_row(pre157_conn):
    _apply(pre157_conn, MIG_157)
    eid = _mk_employee(pre157_conn, 'T_WHT3', 'no wht', '2026-01-01')

    run = hr.generate_run('2027-01', 1, created_by=1, conn=pre157_conn)
    it = _item(pre157_conn, run['id'], eid)

    assert it['wht_amount'] == 0
    assert it['net_pay'] == it['gross']


# ── test 4: update_payroll_item(wht_amount=X) ───────────────────────────────

def test_update_payroll_item_wht_amount_recomputes_net_pay(pre157_conn):
    _apply(pre157_conn, MIG_157)
    eid = _mk_employee(pre157_conn, 'T_WHT4', 'wht edit', '2026-01-01')
    run = hr.generate_run('2027-01', 1, created_by=1, conn=pre157_conn)
    it = _item(pre157_conn, run['id'], eid)
    assert it['wht_amount'] == 0

    updated = hr.update_payroll_item(it['id'], wht_amount=40.0, conn=pre157_conn)
    assert updated['wht_amount'] == 40.0
    assert updated['net_pay'] == it['gross'] - 40.0


def test_update_payroll_item_wht_amount_refuses_on_finalized_run(pre157_conn):
    _apply(pre157_conn, MIG_157)
    eid = _mk_employee(pre157_conn, 'T_WHT5', 'wht finalize', '2026-01-01')
    run = hr.generate_run('2027-01', 1, created_by=1, conn=pre157_conn)
    it = _item(pre157_conn, run['id'], eid)
    hr.finalize_run(run['id'], conn=pre157_conn)

    with pytest.raises(ValueError):
        hr.update_payroll_item(it['id'], wht_amount=99.0, conn=pre157_conn)


def test_update_payroll_item_refuses_negative_wht(pre157_conn):
    """payroll_items cannot carry a CHECK without a full table rebuild, so this
    code path IS the guard. _recompute_totals subtracts wht_amount — a negative
    value would quietly pay the employee MORE than gross."""
    _apply(pre157_conn, MIG_157)
    eid = _mk_employee(pre157_conn, 'T_WHT6', 'wht negative', '2026-01-01')
    run = hr.generate_run('2027-01', 1, created_by=1, conn=pre157_conn)
    it = _item(pre157_conn, run['id'], eid)

    with pytest.raises(ValueError):
        hr.update_payroll_item(it['id'], wht_amount=-32.0, conn=pre157_conn)

    # unchanged, and 0 is still accepted (that is how withholding is stopped)
    after = _item(pre157_conn, run['id'], eid)
    assert (after['wht_amount'], after['net_pay']) == (it['wht_amount'], it['net_pay'])
    assert hr.update_payroll_item(
        it['id'], wht_amount=0.0, conn=pre157_conn)['wht_amount'] == 0.0


# ── test 5: migration invariant — column MOVE, not a money change ──────────

def test_migration_moves_money_without_changing_net_pay_or_gross(pre157_conn):
    seed = _seed_pre157(pre157_conn)
    before = {
        r["id"]: (r["net_pay"], r["gross"], r["other_deductions"])
        for r in pre157_conn.execute(
            "SELECT id, net_pay, gross, other_deductions FROM payroll_items")
    }
    assert before, "fixture has no payroll_items rows to test against"
    # DERIVE the expected count — never hardcode it. The fixture is a copy of
    # whatever DB conftest resolves, and local vs prod legitimately differ on
    # this exact field (2026-08-10: local 2 rows, prod 3 — the 2026-07 fix was
    # applied to prod only). A hardcoded count turns a routine local refresh
    # into a red money test.
    wht_before = pre157_conn.execute(
        "SELECT COUNT(*) FROM payroll_items "
        "WHERE other_deductions_note = 'ภาษีหัก ณ ที่จ่าย' AND other_deductions > 0"
    ).fetchone()[0]
    assert wht_before > 0, (
        "fixture DB has no ภาษีหัก ณ ที่จ่าย rows at all — this test would pass "
        "vacuously (it would assert a move that never happened). Check that "
        "conftest resolved a real dev DB."
    )

    _apply(pre157_conn, MIG_157)

    after = pre157_conn.execute(
        "SELECT id, net_pay, gross, other_deductions, wht_amount FROM payroll_items"
    ).fetchall()
    assert len(after) == len(before)
    for r in after:
        old_net, old_gross, old_other = before[r["id"]]
        assert r["net_pay"] == old_net, (
            f"row {r['id']} net_pay moved: {old_net} -> {r['net_pay']} "
            "(this must be a column MOVE, not a money change)"
        )
        assert r["gross"] == old_gross
        assert r["wht_amount"] + r["other_deductions"] == old_other

    moved = pre157_conn.execute(
        "SELECT COUNT(*) FROM payroll_items WHERE wht_amount > 0"
    ).fetchone()[0]
    assert moved == wht_before, (
        f"{wht_before} rows were keyed as ภาษีหัก ณ ที่จ่าย but {moved} carry wht_amount"
    )


# ── test 6: rollback restores other_deductions + drops schema cleanly ──────

def test_rollback_restores_other_deductions_and_drops_schema(pre157_conn):
    _seed_pre157(pre157_conn)
    before_data = {
        r["id"]: (r["other_deductions"], r["other_deductions_note"])
        for r in pre157_conn.execute(
            "SELECT id, other_deductions, other_deductions_note FROM payroll_items")
    }
    before_triggers = {
        r["name"]: r["sql"] for r in pre157_conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='payroll_items'")
    }

    _apply(pre157_conn, MIG_157)
    _apply(pre157_conn, ROLLBACK_157)

    after_data = {
        r["id"]: (r["other_deductions"], r["other_deductions_note"])
        for r in pre157_conn.execute(
            "SELECT id, other_deductions, other_deductions_note FROM payroll_items")
    }
    assert after_data == before_data

    after_triggers = {
        r["name"]: r["sql"] for r in pre157_conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='payroll_items'")
    }
    assert after_triggers == before_triggers, "payroll_items trigger bodies not byte-identical"

    cols = {r["name"] for r in pre157_conn.execute("PRAGMA table_info(payroll_items)")}
    assert "wht_amount" not in cols

    tables = {
        r[0] for r in pre157_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "employee_wht_history" not in tables

    # re-apply from scratch must work identically after rollback
    expected = pre157_conn.execute(
        "SELECT COUNT(*) FROM payroll_items "
        "WHERE other_deductions_note = 'ภาษีหัก ณ ที่จ่าย' AND other_deductions > 0"
    ).fetchone()[0]
    assert expected > 0, "rollback produced no ภาษีหัก ณ ที่จ่าย rows to re-migrate"
    _apply(pre157_conn, MIG_157)
    n = pre157_conn.execute(
        "SELECT COUNT(*) FROM payroll_items WHERE wht_amount > 0"
    ).fetchone()[0]
    assert n == expected


# ── test 6b: rollback must ADD to a co-existing deduction, never overwrite ──
# The first version of the rollback assigned `other_deductions = wht_amount`,
# which destroyed a real ฿500 ค่าปรับ sitting on the same row (probe, 2026-08-10).
# Test 6 could not see it: no row in the live dev DB carries both.

def test_rollback_refuses_when_a_row_carries_both(pre157_conn):
    """Summing ฿500 ค่าปรับ + ฿32 WHT preserves the money but writes a composite
    note the forward migration's exact-match backfill cannot re-read — so a later
    forward→rollback→forward cycle strands the whole ฿532 with wht_amount 0 and no
    standing rate, i.e. payroll silently stops withholding. Refuse instead.
    (Codex review 2026-08-11; the earlier overwrite version destroyed the ฿500.)"""
    _seed_pre157(pre157_conn)
    _apply(pre157_conn, MIG_157)
    row = pre157_conn.execute(
        "SELECT id, wht_amount FROM payroll_items WHERE wht_amount > 0 "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row, "no backfilled WHT row to test against"
    iid = row["id"]

    pre157_conn.execute(
        "UPDATE payroll_items SET other_deductions = 500, "
        "other_deductions_note = 'ค่าปรับ' WHERE id = ?", (iid,))
    pre157_conn.commit()

    with pytest.raises(sqlite3.IntegrityError) as e:
        _apply(pre157_conn, ROLLBACK_157)
    assert "rollback refused" in str(e.value)

    # nothing was touched — the ฿500 and the ฿32 both still stand
    after = pre157_conn.execute(
        "SELECT wht_amount, other_deductions, other_deductions_note "
        "FROM payroll_items WHERE id = ?", (iid,)).fetchone()
    assert (after["wht_amount"], after["other_deductions"],
            after["other_deductions_note"]) == (row["wht_amount"], 500, 'ค่าปรับ')

    # ⚠ the refusal must leave the connection CLEAN. RAISE(ABORT) undoes only the
    # failing statement: the transaction stays open, the write lock is held, and
    # the TEMP table+trigger survive — so a retry dies on "table
    # _wht_rollback_check already exists" and the next executescript() implicitly
    # commits the abandoned transaction. RAISE(ROLLBACK) is what makes this pass.
    assert not pre157_conn.in_transaction, "refusal left a transaction open"
    assert [r[0] for r in pre157_conn.execute(
        "SELECT name FROM sqlite_temp_master")] == [], "refusal stranded TEMP objects"

    # and the SAME connection can retry and get the same refusal, not a leftover-state error
    with pytest.raises(sqlite3.IntegrityError) as e2:
        _apply(pre157_conn, ROLLBACK_157)
    assert "rollback refused" in str(e2.value)


def test_forward_rollback_forward_round_trips(pre157_conn):
    """Pure-WHT rows must survive forward -> rollback -> forward with the money
    and the standing rate intact.

    Population is planted by _seed_pre157, NOT inherited: `pre157_conn` builds
    its pre-state by running the rollback UNDER TEST, so a rollback mutation used
    to make this test red inside the fixture, before the subject was reached."""
    seed = _seed_pre157(pre157_conn)
    _apply(pre157_conn, MIG_157)
    before = {r["id"]: r["wht_amount"] for r in pre157_conn.execute(
        "SELECT id, wht_amount FROM payroll_items WHERE wht_amount > 0")}
    seeded_before = {r["employee_id"]: (r["effective_date"], r["monthly_wht"])
                     for r in pre157_conn.execute(
                         "SELECT employee_id, effective_date, monthly_wht "
                         "FROM employee_wht_history")}
    # CONTROL: the planted population really is what we are round-tripping, and
    # the first forward pass already produced the expected standing rates. If this
    # fails the fixture is broken, which is a different failure from the subject.
    assert seeded_before == seed, f"first forward pass seeded {seeded_before}, want {seed}"
    assert len(before) >= 4, f"expected the 4 planted WHT rows, got {before}"

    _apply(pre157_conn, ROLLBACK_157)
    _apply(pre157_conn, MIG_157)

    after = {r["id"]: r["wht_amount"] for r in pre157_conn.execute(
        "SELECT id, wht_amount FROM payroll_items WHERE wht_amount > 0")}
    seeded_after = {r["employee_id"]: (r["effective_date"], r["monthly_wht"])
                    for r in pre157_conn.execute(
                        "SELECT employee_id, effective_date, monthly_wht "
                        "FROM employee_wht_history")}
    assert after == before, f"WHT moved across the cycle: {before} -> {after}"
    assert seeded_after == seeded_before, (
        f"standing rate moved across the cycle: {seeded_before} -> {seeded_after}")


def test_negative_standing_wht_is_refused_by_the_schema(pre157_conn):
    """monthly_wht is SUBTRACTED from net_pay, so a negative rate would pay MORE.
    The form's min=0 does not survive a direct POST — the CHECK does."""
    _apply(pre157_conn, MIG_157)
    with pytest.raises(sqlite3.IntegrityError):
        pre157_conn.execute(
            "INSERT INTO employee_wht_history (employee_id, effective_date, "
            "monthly_wht, reason) VALUES (1, '2027-01-01', -32.0, 'adjust')")
    # zero IS allowed — that is how a withholding is stopped
    pre157_conn.execute(
        "INSERT INTO employee_wht_history (employee_id, effective_date, "
        "monthly_wht, reason) VALUES (1, '2027-01-01', 0.0, 'adjust')")


def test_negative_payroll_item_wht_is_refused_by_the_schema(pre157_conn):
    """The column itself must refuse it, not only hr.update_payroll_item — direct
    SQL, a future migration, or a new write path would otherwise slip past.
    SQLite accepts a CHECK on ADD COLUMN, so there is no reason to rely on code."""
    _apply(pre157_conn, MIG_157)
    iid = pre157_conn.execute(
        "SELECT id FROM payroll_items LIMIT 1").fetchone()["id"]

    with pytest.raises(sqlite3.IntegrityError):
        pre157_conn.execute(
            "UPDATE payroll_items SET wht_amount = -1 WHERE id = ?", (iid,))

    # 0 and positive both fine
    pre157_conn.execute("UPDATE payroll_items SET wht_amount = 0 WHERE id = ?", (iid,))
    pre157_conn.execute("UPDATE payroll_items SET wht_amount = 5 WHERE id = ?", (iid,))
    assert pre157_conn.execute(
        "SELECT wht_amount FROM payroll_items WHERE id = ?", (iid,)).fetchone()[0] == 5


def test_migration_self_records_in_applied_migrations(pre157_conn):
    """The runner records the filename AFTER executescript has committed; a crash
    in that window would leave this non-re-runnable migration applied-but-unrecorded
    and the next boot would die on `duplicate column name`."""
    pre157_conn.execute(
        "DELETE FROM applied_migrations WHERE filename = '157_wht_single_source.sql'")
    pre157_conn.commit()
    _apply(pre157_conn, MIG_157)
    assert pre157_conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename = '157_wht_single_source.sql'").fetchone()[0] == 1
    # and the rollback removes it again
    _apply(pre157_conn, ROLLBACK_157)
    assert pre157_conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename = '157_wht_single_source.sql'").fetchone()[0] == 0


# ── test 7: the SEED — the whole point of the feature going forward ────────
# Without this the migration's INSERT INTO employee_wht_history can be deleted
# outright and every other test in this file still passes (verified by
# break-it-once, 2026-08-10) — yet Put's ฿32 would silently stop auto-applying
# from the next run onward, which is the exact drift this project exists to end.

def test_migration_seeds_wht_history_so_future_runs_auto_apply(pre157_conn):
    # Deterministic population, and the oracle is computed with the SAME
    # ROW_NUMBER(year_month, id) rule the migration uses — a MIN(year_month)
    # oracle cannot pin which amount wins a same-month tie.
    expected = _seed_pre157(pre157_conn)

    _apply(pre157_conn, MIG_157)

    seeded = {
        r["employee_id"]: (r["effective_date"], r["monthly_wht"])
        for r in pre157_conn.execute(
            "SELECT employee_id, effective_date, monthly_wht, reason "
            "FROM employee_wht_history")
    }
    reasons = {r["employee_id"]: r["reason"] for r in pre157_conn.execute(
        "SELECT employee_id, reason FROM employee_wht_history")}
    assert seeded == expected, f"seeded {seeded} != expected {expected}"
    assert set(reasons.values()) == {"initial"}

    # the outcome that matters: a FUTURE month resolves with nobody keying anything
    for emp, (eff, amt) in expected.items():
        y0 = int(eff[:4])
        for ym in (eff[:7], f"{y0}-12", f"{y0 + 2}-06"):   # its own month, later that year, 2 years on
            got = hr.resolve_wht(emp, ym, conn=pre157_conn)
            assert got is not None, f"employee {emp}: nothing resolved for {ym}"
            assert got["monthly_wht"] == amt, (
                f"employee {emp} {ym} resolved {got['monthly_wht']}, want {amt}")
        y, m = int(eff[:4]), int(eff[5:7])
        prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
        assert hr.resolve_wht(emp, prev, conn=pre157_conn) is None, (
            f"employee {emp}: resolved for {prev}, before its effective date {eff}")
