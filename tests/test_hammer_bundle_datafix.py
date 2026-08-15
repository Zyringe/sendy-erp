"""Tests for scripts/hammer_bundle_datafix.py (Phase 1 of the hammer แผง =
อัน + การ์ด plan, docs/plans/2026-08-14-hammer-pack-bundle-plan.md §5).

Runs against a COPY of the live dev DB (tmp_db), with the documented Phase-1
baseline FORCED onto pids 268/269/270/271/869 — never inherited (see
erp-engineering-discipline.md: "tmp_db clones the LIVE dev DB with its data
... force the state you need"). Local/prod stock for 268/269 disagree
(local +6/0, prod -6/-12) and Phase 1 never touches stock_levels or
transactions for the two แผง products, so no test here depends on stock.
"""
import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import hammer_bundle_datafix as datafix  # noqa: E402

PID_268, PID_269, PID_270, PID_271, PID_869 = 268, 269, 270, 271, 869

# The exact baseline this Phase-1 script was written against (plan §3's
# ground-truth table, minus stock — see module docstring above).
BASELINE = {
    268: dict(series=None, model=None,
             product_name="ฆ้อนด้ามไฟเบอร์ Sendai (แผง)",
             sku_code="HMR-HFBRV573-SD-PN", opening_cost=76.0, cost_price=76.0),
    269: dict(series="ตารางกันลื่น", model=None,
             product_name="ฆ้อนด้ามไฟเบอร์ตารางกันลื่น Sendai (แผง)",
             sku_code="HMR-HFBRGRID-SD-SD028-PN", opening_cost=0.0, cost_price=0.0),
    270: dict(series=None, model="#BSN01",
             product_name="ฆ้อนด้ามไฟเบอร์ Sendai #BSN01",
             sku_code="HMR-HFBR-SD-#BSN01", opening_cost=66.0, cost_price=66.0),
    271: dict(series="ตารางกันลื่น_FN", model="#BSN02",
             product_name="ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02",
             sku_code="HMR-HFBR-SD-S6F4B-#BSN02", opening_cost=68.0, cost_price=68.0),
    869: dict(series=None, model=None,
             product_name="แผงฆ้อนหงอน",
             sku_code="HMR-HGEN", opening_cost=0.0, cost_price=0.0),
}

W1_NEW_NAMES = {
    268: "ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง)",
    269: "ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02 (แผง)",
    271: "ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02",  # unchanged text
}
W2_NEW_COST = {869: 5.0, 268: 71.0, 269: 73.0}


# ── fixture: force the Phase-1 baseline onto a copy of the live dev DB ─────

@pytest.fixture
def hammer_db(tmp_db):
    # The shared workspace's live dev DB (what tmp_db copies) is not
    # necessarily on this branch's migration — verified 2026-08-15: it was
    # still at 157, one behind mig 158 (the `role` column + the partial
    # unique index this script depends on). tmp_db only monkeypatches
    # database.DATABASE_PATH; it does not run the migration runner. Apply
    # whatever this branch's data/migrations/ still owes the copy before
    # forcing the Phase-1 baseline onto it.
    import database
    database.init_db()

    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    for pid, f in BASELINE.items():
        row = conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            conn.close()
            pytest.skip(f"pid {pid} not present in the live dev DB snapshot")
        conn.execute(
            "UPDATE products SET series=?, model=?, product_name=?, sku_code=?,"
            " opening_cost=?, cost_price=?, is_active=1 WHERE id=?",
            (f["series"], f["model"], f["product_name"], f["sku_code"],
             f["opening_cost"], f["cost_price"], pid))

    # Plan §3: "No formula references 268/269/270/271/869 today" — force it.
    ids = tuple(BASELINE)
    placeholders = ",".join("?" * len(ids))
    fids = {r[0] for r in conn.execute(
        f"SELECT DISTINCT formula_id FROM conversion_formula_inputs"
        f" WHERE product_id IN ({placeholders})", ids)}
    fids |= {r[0] for r in conn.execute(
        f"SELECT id FROM conversion_formulas"
        f" WHERE output_product_id IN ({placeholders})", ids)}
    for fid in fids:
        conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (fid,))
        conn.execute("DELETE FROM conversion_formulas WHERE id=?", (fid,))

    # Deterministic WACC walk for W2: wipe the ledger-affecting rows for the
    # 3 products it recalculates, so opening_cost's INITIAL entry is the
    # ONLY event and cost_price == opening_cost exactly after recalc,
    # regardless of whatever real BSN history the live dev DB happens to
    # carry today.
    for pid in (268, 269, 869):
        conn.execute("DELETE FROM transactions WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM purchase_transactions WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM conversion_cost_log WHERE output_product_id=?", (pid,))
        conn.execute("DELETE FROM product_cost_ledger WHERE product_id=?", (pid,))

    conn.commit()
    conn.close()
    return tmp_db


def _snap(db_path):
    """Every column any Phase-1 checkpoint could touch, for the 5 products
    plus the 2 target formulas — used to prove idempotent re-runs and
    rollbacks change NOTHING."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        products = {
            pid: dict(conn.execute(
                "SELECT series, model, product_name, sku_code, opening_cost,"
                " cost_price FROM products WHERE id=?", (pid,)).fetchone())
            for pid in BASELINE
        }
        formulas = [
            dict(r) for r in conn.execute(
                "SELECT id, name, output_product_id, output_qty, is_active"
                " FROM conversion_formulas WHERE output_product_id IN (268,269)"
                " ORDER BY id")
        ]
        inputs = [
            dict(r) for r in conn.execute(
                "SELECT formula_id, product_id, quantity, role"
                " FROM conversion_formula_inputs"
                " WHERE formula_id IN (SELECT id FROM conversion_formulas"
                "                       WHERE output_product_id IN (268,269))"
                " ORDER BY formula_id, id")
        ]
        return {"products": products, "formulas": formulas, "inputs": inputs}
    finally:
        conn.close()


def _backup_dir(tmp_path):
    d = str(tmp_path / "backups")
    os.makedirs(d, exist_ok=True)
    return d


# ── W1 ───────────────────────────────────────────────────────────────────

def test_w1_happy_path_renames_268_269_271(hammer_db, tmp_path):
    results = datafix.run_w1(hammer_db, _backup_dir(tmp_path), "test")
    assert len(results) == 3
    by_pid = {r["pid"]: r for r in results}
    assert by_pid[268]["status"] == "applied"
    assert by_pid[269]["status"] == "applied"
    assert by_pid[271]["status"] == "applied"

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    for pid, expected_name in W1_NEW_NAMES.items():
        row = conn.execute(
            "SELECT product_name, sku_code FROM products WHERE id=?", (pid,)
        ).fetchone()
        assert row["product_name"] == expected_name
        assert row["sku_code"] == BASELINE[pid]["sku_code"], (
            f"pid {pid}: sku_code moved")

    # 270 is never touched by W1.
    row270 = conn.execute(
        "SELECT product_name, sku_code, series, model FROM products WHERE id=?",
        (270,)).fetchone()
    assert row270["product_name"] == BASELINE[270]["product_name"]
    assert row270["sku_code"] == BASELINE[270]["sku_code"]
    conn.close()


def test_w1_second_run_skips_and_changes_nothing(hammer_db, tmp_path):
    bdir = _backup_dir(tmp_path)
    r1 = datafix.run_w1(hammer_db, bdir, "test")
    assert {r["status"] for r in r1} == {"applied"}
    snap_after_1 = _snap(hammer_db)

    r2 = datafix.run_w1(hammer_db, bdir, "test")
    assert {r["status"] for r in r2} == {"skipped (already applied)"}
    snap_after_2 = _snap(hammer_db)

    assert snap_after_1 == snap_after_2


def test_w1_refuses_on_unknown_state_and_touches_nothing(hammer_db, tmp_path):
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET model='WEIRD-DRIFTED' WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w1(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    # 268 itself is untouched (still the drifted value, not "fixed"), and —
    # because 268 is first in W1_TARGETS — 269/271 were never even attempted.
    assert after["products"][268]["model"] == "WEIRD-DRIFTED"
    assert after == before


def test_w1_postcondition_gate_is_not_vacuous(hammer_db, tmp_path, monkeypatch):
    """break-it-once: a save_product that returns the WRONG name must halt
    the run. Proves the post-check can actually fail, not just always agree
    with the thing it is checking."""
    def fake_save_product(db_path, pid, fields, **kw):
        return {"old_name": "x", "new_name": "ไม่ใช่ชื่อที่ถูกต้อง",
                "sku_code": BASELINE[pid]["sku_code"]}

    monkeypatch.setattr(datafix.naming_cascade, "save_product", fake_save_product)
    with pytest.raises(datafix.CheckpointPostconditionError):
        datafix.run_w1(hammer_db, _backup_dir(tmp_path), "test")


# ── W2 ───────────────────────────────────────────────────────────────────

def test_w2_happy_path_sets_opening_cost_and_replays_wacc(hammer_db, tmp_path):
    results = datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    assert len(results) == 3
    by_pid = {r["pid"]: r for r in results}
    for pid, expected in W2_NEW_COST.items():
        assert by_pid[pid]["status"] == "applied"
        assert by_pid[pid]["cost_price"] == expected

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    for pid, expected in W2_NEW_COST.items():
        row = conn.execute(
            "SELECT opening_cost, cost_price FROM products WHERE id=?", (pid,)
        ).fetchone()
        assert row["opening_cost"] == expected
        assert row["cost_price"] == expected
    conn.close()


def test_w2_second_run_skips_and_changes_nothing(hammer_db, tmp_path):
    bdir = _backup_dir(tmp_path)
    r1 = datafix.run_w2(hammer_db, bdir, "test")
    assert {r["status"] for r in r1} == {"applied"}
    snap1 = _snap(hammer_db)

    r2 = datafix.run_w2(hammer_db, bdir, "test")
    assert {r["status"] for r in r2} == {"skipped (already applied)"}
    snap2 = _snap(hammer_db)

    assert snap1 == snap2


def test_w2_refuses_on_unknown_baseline_and_touches_nothing(hammer_db, tmp_path):
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET opening_cost=12.34 WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after["products"][268]["opening_cost"] == 12.34
    assert after == before


def test_w2_rolls_back_fully_on_failed_postcondition(hammer_db, tmp_path, monkeypatch):
    """break-it-once: force recalculate_product_wacc to write a WRONG
    cost_price. The whole checkpoint (including the opening_cost UPDATE)
    must roll back to the ORIGINAL baseline — not land on the wrong value,
    not land on the intended target either."""
    def fake_wacc(pid, conn=None, operation=None):
        conn.execute("UPDATE products SET cost_price=? WHERE id=?", (999.0, pid))
        return 999.0

    monkeypatch.setattr(datafix, "recalculate_product_wacc", fake_wacc)
    with pytest.raises(datafix.CheckpointPostconditionError):
        datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    for pid in (268, 269, 869):
        row = conn.execute(
            "SELECT opening_cost, cost_price FROM products WHERE id=?", (pid,)
        ).fetchone()
        assert row["opening_cost"] == BASELINE[pid]["opening_cost"], pid
        assert row["cost_price"] == BASELINE[pid]["cost_price"], pid
    conn.close()


def test_w2_partial_resume_only_touches_the_unfinished_product(hammer_db, tmp_path):
    """869 already applied by hand (e.g. a prior interrupted run); 268/269
    still at baseline. A resume must apply only what is left."""
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET opening_cost=5.0, cost_price=5.0 WHERE id=869")
    conn.commit()
    conn.close()

    results = datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    by_pid = {r["pid"]: r for r in results}
    assert by_pid[869]["status"] == "skipped (already applied)"
    assert by_pid[268]["status"] == "applied"
    assert by_pid[269]["status"] == "applied"


# ── W3 ───────────────────────────────────────────────────────────────────

def test_w3_happy_path_creates_both_bundle_formulas(hammer_db, tmp_path):
    results = datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")
    assert len(results) == 2
    assert {r["status"] for r in results} == {"applied"}

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    for output_pid, component_pid in ((268, 270), (269, 271)):
        formulas = conn.execute(
            "SELECT id, name, output_qty, is_active FROM conversion_formulas"
            " WHERE output_product_id=? AND is_active=1", (output_pid,)
        ).fetchall()
        assert len(formulas) == 1, f"output {output_pid}: expected exactly 1 active formula"
        f = formulas[0]
        assert f["output_qty"] == 1
        assert f["name"].startswith("[แพ็ค]")

        inputs = conn.execute(
            "SELECT product_id, quantity, role FROM conversion_formula_inputs"
            " WHERE formula_id=? ORDER BY id", (f["id"],)).fetchall()
        assert len(inputs) == 2
        by_role = {r["role"]: r for r in inputs}
        assert by_role["component"]["product_id"] == component_pid
        assert by_role["component"]["quantity"] == 1
        assert by_role["packaging"]["product_id"] == PID_869
        assert by_role["packaging"]["quantity"] == 1

        # Deliberately inserted in REVERSE business order — the first row
        # (lowest id) is packaging, not component. Row order carries no
        # meaning; only `role` does.
        assert inputs[0]["role"] == "packaging"
        assert inputs[1]["role"] == "component"

        comp_id = datafix.conversion_roles.component_product_id(
            f["name"], f["is_active"], inputs)
        assert comp_id == component_pid
    conn.close()


def test_w3_second_run_skips_and_changes_nothing(hammer_db, tmp_path):
    bdir = _backup_dir(tmp_path)
    r1 = datafix.run_w3(hammer_db, bdir, "test")
    assert {r["status"] for r in r1} == {"applied"}
    snap1 = _snap(hammer_db)

    r2 = datafix.run_w3(hammer_db, bdir, "test")
    assert {r["status"] for r in r2} == {"skipped (already applied)"}
    snap2 = _snap(hammer_db)

    assert snap1 == snap2


def test_w3_refuses_on_conflicting_existing_formula(hammer_db, tmp_path):
    conn = sqlite3.connect(hammer_db)
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active)"
        " VALUES ('[แพ็ค] ของปลอม', 268, 1, 1)")
    fid = cur.lastrowid
    # Wrong component (870 instead of 270) — a genuinely conflicting shape.
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 869, 1, 'packaging')", (fid,))
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 270, 5, 'component')", (fid,))  # wrong qty too
    conn.commit()
    conn.close()

    before_count = _snap(hammer_db)["formulas"]
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")
    after_count = _snap(hammer_db)["formulas"]

    # 268's bogus formula is untouched, and — because 268 is checked first —
    # 269's formula was never created either.
    assert len(after_count) == 1
    assert after_count == before_count


def test_w3_rolls_back_fully_on_failed_postcondition(hammer_db, tmp_path, monkeypatch):
    """break-it-once: force validate_pack_inputs to always reject. Both
    INSERTed formulas (and their input rows) must vanish — not one, not
    half — proving the transaction actually rolls back rather than leaving
    a partially-built pair."""
    def always_reject(name, is_active, inputs):
        raise datafix.conversion_roles.ConversionRoleError("forced failure for the test")

    monkeypatch.setattr(datafix.conversion_roles, "validate_pack_inputs", always_reject)
    with pytest.raises(datafix.CheckpointPostconditionError):
        datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")

    conn = sqlite3.connect(hammer_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE output_product_id IN (268,269)"
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_w3_partial_resume_only_creates_the_missing_formula(hammer_db, tmp_path):
    """268's formula already exists (matching shape); 269's does not yet."""
    conn = sqlite3.connect(hammer_db)
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active)"
        " VALUES (?, 268, 1, 1)",
        ("[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน",))
    fid = cur.lastrowid
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 270, 1, 'component')", (fid,))
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 869, 1, 'packaging')", (fid,))
    conn.commit()
    conn.close()

    results = datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")
    by_output = {r["output_pid"]: r for r in results}
    assert by_output[268]["status"] == "skipped (already applied)"
    assert by_output[268]["formula_id"] == fid
    assert by_output[269]["status"] == "applied"


# ── sku_code must never move (across all three checkpoints) ───────────────

def test_sku_code_byte_identical_across_full_run(hammer_db, tmp_path):
    before = {pid: BASELINE[pid]["sku_code"] for pid in (268, 269, 270, 271)}
    bdir = _backup_dir(tmp_path)
    datafix.run_w1(hammer_db, bdir, "test")
    datafix.run_w2(hammer_db, bdir, "test")
    datafix.run_w3(hammer_db, bdir, "test")

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    for pid, sku in before.items():
        row = conn.execute("SELECT sku_code FROM products WHERE id=?", (pid,)).fetchone()
        assert row["sku_code"] == sku, f"pid {pid}: sku_code moved"
    conn.close()


# ── CLI surface (main / --mode / --confirm) ────────────────────────────────

def test_main_live_without_confirm_refuses_and_touches_nothing(hammer_db, tmp_path):
    before = _snap(hammer_db)
    rc = datafix.main(["--db-path", hammer_db, "--mode", "live",
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 2
    assert _snap(hammer_db) == before


def test_main_live_with_mismatched_confirm_refuses_and_touches_nothing(hammer_db, tmp_path):
    before = _snap(hammer_db)
    rc = datafix.main(["--db-path", hammer_db, "--mode", "live",
                       "--confirm", hammer_db + "-typo",
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 2
    assert _snap(hammer_db) == before


def test_main_live_with_matching_confirm_runs_all_three_checkpoints(hammer_db, tmp_path):
    rc = datafix.main(["--db-path", hammer_db, "--mode", "live",
                       "--confirm", hammer_db,
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 0

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT product_name, cost_price FROM products WHERE id=268").fetchone()
    assert row["product_name"] == W1_NEW_NAMES[268]
    assert row["cost_price"] == 71.0
    n = conn.execute(
        "SELECT COUNT(*) FROM conversion_formulas"
        " WHERE output_product_id IN (268,269) AND is_active=1").fetchone()[0]
    assert n == 2
    conn.close()


def test_main_rehearse_runs_without_confirm(hammer_db, tmp_path):
    rc = datafix.main(["--db-path", hammer_db, "--mode", "rehearse",
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 0
    conn = sqlite3.connect(hammer_db)
    row = conn.execute("SELECT cost_price FROM products WHERE id=269").fetchone()
    conn.close()
    assert row[0] == 73.0


def test_main_full_run_is_idempotent(hammer_db, tmp_path):
    """The team-lead's resumability proof: running the whole script twice
    must leave the SECOND run reporting nothing-to-do and the DB byte-for-
    byte (at the row level) unchanged from after the first run."""
    bdir = _backup_dir(tmp_path)
    rc1 = datafix.main(["--db-path", hammer_db, "--mode", "rehearse",
                        "--backup-dir", bdir])
    assert rc1 == 0
    snap1 = _snap(hammer_db)

    rc2 = datafix.main(["--db-path", hammer_db, "--mode", "rehearse",
                        "--backup-dir", bdir])
    assert rc2 == 0
    snap2 = _snap(hammer_db)

    assert snap1 == snap2
