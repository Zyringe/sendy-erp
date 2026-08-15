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


def test_w1_refuses_when_columns_match_old_but_product_name_does_not(hammer_db, tmp_path):
    """The structured columns (series/model) alone are not the full OLD
    contract: 268's series/model already match the documented old_fields,
    but product_name is a stale/drifted string neither the documented OLD
    nor NEW name (e.g. left by some other process). Must refuse, not treat
    this as the untouched baseline and rename it anyway."""
    conn = sqlite3.connect(hammer_db)
    conn.execute(
        "UPDATE products SET product_name='ชื่อค้างเก่าที่ไม่ตรงเอกสาร' WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w1(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after["products"][268]["product_name"] == 'ชื่อค้างเก่าที่ไม่ตรงเอกสาร'
    assert after == before


def test_w1_refuses_when_columns_match_old_but_sku_code_does_not(hammer_db, tmp_path):
    """Same shape as above, but for sku_code: columns and product_name both
    match the documented OLD baseline for 268, but sku_code has drifted.
    Must refuse rather than silently proceed on a partial match — a stale
    sku_code here means this is NOT the exact row the plan was written
    against, and save_product's own "sku_code moved" guard only fires
    AFTER the rename already committed."""
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET sku_code='HMR-DRIFTED-CODE' WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w1(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after["products"][268]["sku_code"] == 'HMR-DRIFTED-CODE'
    assert after == before


def test_w1_refuses_done_when_name_and_columns_match_but_sku_code_moved(hammer_db, tmp_path):
    """DONE must check sku_code too, not just OLD: apply W1 for real, then
    simulate some OTHER process moving 268's sku_code afterward (the exact
    failure W1 exists to prevent — sku_code is the join key to photo
    folders and live TikTok SKUs). A re-run must refuse, not read the
    correct name + matching structured columns as 'already applied' and
    silently skip past a moved sku_code."""
    bdir = _backup_dir(tmp_path)
    r1 = datafix.run_w1(hammer_db, bdir, "test")
    assert {r["status"] for r in r1} == {"applied"}

    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET sku_code='HMR-DRIFTED-AFTER-RENAME' WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w1(hammer_db, bdir, "test")
    after = _snap(hammer_db)

    # 268 still holds the drifted sku (not silently accepted, not "fixed"),
    # and — because 268 is first in W1_TARGETS — 269/271 (already correctly
    # applied) were never even re-examined for a write.
    assert after["products"][268]["sku_code"] == 'HMR-DRIFTED-AFTER-RENAME'
    assert after == before


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


def test_w2_refuses_when_opening_cost_matches_old_but_cost_price_does_not(hammer_db, tmp_path):
    """opening_cost matching the documented OLD value alone is not enough:
    268's opening_cost is still 76.0 (OLD), but cost_price has drifted to
    999.0 (e.g. an unrelated WACC recompute since the plan's baseline table
    was written). Must refuse rather than silently overwrite a cost_price
    that is no longer the documented starting point."""
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET cost_price=999.0 WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after["products"][268]["opening_cost"] == 76.0
    assert after["products"][268]["cost_price"] == 999.0
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
    """869 already applied by hand (e.g. a prior interrupted run) — REALLY
    applied: opening_cost written AND the ledger replayed via the real
    recalculate_product_wacc, not just the columns hand-edited (a
    columns-only edit is a DIFFERENT scenario, now correctly refused by the
    DONE gate's ledger check — see
    test_w2_refuses_done_when_columns_match_but_ledger_is_missing /
    ..._is_stale below). 268/269 still at baseline. A resume must apply
    only what is left."""
    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE products SET opening_cost=5.0 WHERE id=869")
    datafix.recalculate_product_wacc(869, conn=conn)
    conn.commit()
    conn.close()

    results = datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    by_pid = {r["pid"]: r for r in results}
    assert by_pid[869]["status"] == "skipped (already applied)"
    assert by_pid[268]["status"] == "applied"
    assert by_pid[269]["status"] == "applied"


def test_w2_refuses_done_when_columns_match_but_ledger_is_missing(hammer_db, tmp_path):
    """BLOCKER fix: columns matching the target alone is not proof of a
    replay. 268's opening_cost/cost_price are hand-set to the target NEW
    values, but product_cost_ledger for 268 is still empty (hammer_db wipes
    it) — the exact 'columns right, ledger missing' shape a re-run must
    catch, not silently skip as done."""
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET opening_cost=71.0, cost_price=71.0 WHERE id=268")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after["products"][268]["opening_cost"] == 71.0
    assert after["products"][268]["cost_price"] == 71.0
    assert after == before

    conn = sqlite3.connect(hammer_db)
    ledger_count = conn.execute(
        "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id=268").fetchone()[0]
    conn.close()
    assert ledger_count == 0, "still no ledger — not silently created either"


def test_w2_refuses_done_when_columns_match_but_ledger_is_stale(hammer_db, tmp_path):
    """Same shape as above but the ledger is not EMPTY, it is WRONG: an
    INITIAL row exists for 268 but at the OLD cost (76), not the target
    (71) — e.g. a partial/corrupted write, or a ledger nobody ever
    re-replayed after the columns were hand-fixed. Must refuse, not treat
    a stale ledger as proof of a real replay."""
    conn = sqlite3.connect(hammer_db)
    conn.execute("UPDATE products SET opening_cost=71.0, cost_price=71.0 WHERE id=268")
    conn.execute(
        "INSERT INTO product_cost_ledger(product_id, event_type, event_date,"
        " qty_change, unit_cost, stock_after, wacc_after, reference_no, note)"
        " VALUES (268, 'INITIAL', '2026-03-03', 0, 76.0, 0, 76.0, NULL, 'stale')")
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after["products"][268]["opening_cost"] == 71.0
    assert after["products"][268]["cost_price"] == 71.0
    assert after == before

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    ledger = conn.execute(
        "SELECT unit_cost, wacc_after FROM product_cost_ledger"
        " WHERE product_id=268").fetchall()
    conn.close()
    # The stale row is untouched, not replaced/repaired by the refused run.
    assert len(ledger) == 1
    assert ledger[0]["unit_cost"] == 76.0
    assert ledger[0]["wacc_after"] == 76.0


# ── W2 prod-shaped: real transaction history, not the clean slate ─────────
# `hammer_db` deletes transactions/purchase_transactions/conversion_cost_log/
# product_cost_ledger for 268/269/869 so the OTHER W2 tests above get a
# deterministic INITIAL-only walk. That clean slate proves "no history +
# opening_cost X ⇒ cost_price X" — it does NOT prove W2 survives the actual
# reason it exists: on prod, 268 and 269 carry real sales that outrun a
# later stock-in and drive the WACC walk negative, where
# models/wacc.py::_recalculate_product_wacc's negative-stock branch FREEZES
# WACC instead of blending in the new cost. Each test below seeds that shape
# directly on top of the (already-cleaned) fixture and asserts the ledger
# ROWS (event types + order), wacc_after, stock_after, and the final
# products.cost_price SEPARATELY — cost_price alone could be right while the
# ledger it came from is wrong.

def test_w2_prod_shaped_268_negative_stock_freeze_survives_real_history(hammer_db, tmp_path):
    """268: a -9 sale (unlogged — plain OUT rows never append a
    product_cost_ledger entry) drives stock to -9 before a +3 conversion
    arrives. -9 + 3 = -6. The conversion IS logged, and because stock is
    negative when it lands, WACC must FREEZE at the seeded 71 rather than
    blend in the conversion's own unit_cost of 80."""
    conn = sqlite3.connect(hammer_db)
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at) VALUES"
        " (268,'OUT',-9,'unit','SO-268-A','BSN ขาย','2026-03-05 10:00:00')")
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at) VALUES"
        " (268,'IN',3,'unit','PACK-268-A','แปลง: PACK-268-A','2026-03-06 10:00:00')")
    conn.execute(
        "INSERT INTO conversion_cost_log(output_product_id, reference_no, event_date,"
        " output_qty, total_input_cost, unit_cost) VALUES"
        " (268,'PACK-268-A','2026-03-06',3,240.0,80.0)")
    conn.commit()
    conn.close()

    results = datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    by_pid = {r["pid"]: r for r in results}
    assert by_pid[268]["status"] == "applied"
    assert by_pid[268]["cost_price"] == 71.0

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    ledger = conn.execute(
        "SELECT event_type, unit_cost, wacc_after, stock_after"
        " FROM product_cost_ledger WHERE product_id=268 ORDER BY id").fetchall()
    row = conn.execute(
        "SELECT opening_cost, cost_price FROM products WHERE id=268").fetchone()
    conn.close()

    assert [r["event_type"] for r in ledger] == ["INITIAL", "CONVERSION_IN"]
    assert ledger[0]["wacc_after"] == 71.0
    assert ledger[1]["unit_cost"] == 80.0
    # Frozen, not blended: 71 survives the negative-stock conversion untouched.
    assert ledger[1]["wacc_after"] == 71.0
    assert ledger[1]["stock_after"] == -6.0
    assert row["opening_cost"] == 71.0
    assert row["cost_price"] == 71.0


def test_w2_prod_shaped_269_no_initial_before_seed_then_negative_stock_freeze(hammer_db, tmp_path):
    """269's real prod shape is the sharper case: opening_cost is 0 (the
    documented OLD baseline) so recalculating WACC on this exact history
    BEFORE W2 produces NO INITIAL ledger row at all (`if cost_price > 0`
    never passes) and a real 90-baht/unit conversion cost is thrown away —
    WACC stuck at 0 forever. That is the concrete failure W2's seed exists
    to prevent. -15 sale then +3 conversion = -12."""
    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row  # recalculate_product_wacc needs Row access
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at) VALUES"
        " (269,'OUT',-15,'unit','SO-269-A','BSN ขาย','2026-03-05 09:00:00')")
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at) VALUES"
        " (269,'IN',3,'unit','PACK-269-A','แปลง: PACK-269-A','2026-03-06 09:00:00')")
    conn.execute(
        "INSERT INTO conversion_cost_log(output_product_id, reference_no, event_date,"
        " output_qty, total_input_cost, unit_cost) VALUES"
        " (269,'PACK-269-A','2026-03-06',3,270.0,90.0)")
    conn.commit()

    # BEFORE the fix (opening_cost still 0, the documented OLD baseline):
    # confirm the "no INITIAL at all" / frozen-at-0 shape is real, on THIS
    # seeded history, before relying on it as the premise for the "after"
    # assertions below.
    datafix.recalculate_product_wacc(269, conn=conn)
    conn.commit()
    before_ledger = conn.execute(
        "SELECT event_type, wacc_after FROM product_cost_ledger"
        " WHERE product_id=269 ORDER BY id").fetchall()
    conn.close()
    assert [r[0] for r in before_ledger] == ["CONVERSION_IN"]  # no INITIAL row
    assert before_ledger[0][1] == 0.0  # frozen at 0, the 90 cost never absorbed

    results = datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    by_pid = {r["pid"]: r for r in results}
    assert by_pid[269]["status"] == "applied"
    assert by_pid[269]["cost_price"] == 73.0

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    ledger = conn.execute(
        "SELECT event_type, unit_cost, wacc_after, stock_after"
        " FROM product_cost_ledger WHERE product_id=269 ORDER BY id").fetchall()
    row = conn.execute(
        "SELECT opening_cost, cost_price FROM products WHERE id=269").fetchone()
    conn.close()

    assert [r["event_type"] for r in ledger] == ["INITIAL", "CONVERSION_IN"]
    assert ledger[0]["wacc_after"] == 73.0
    assert ledger[1]["unit_cost"] == 90.0
    # Frozen at the seeded 73, not blended with the conversion's 90.
    assert ledger[1]["wacc_after"] == 73.0
    assert ledger[1]["stock_after"] == -12.0
    assert row["opening_cost"] == 73.0
    assert row["cost_price"] == 73.0


def test_w2_prod_shaped_869_real_purchase_history_not_just_clean_slate(hammer_db, tmp_path):
    """869 (the packaging product) has no negative-stock hazard on prod, but
    the seed still needs to work correctly ALONGSIDE a real purchase instead
    of only on an empty ledger: a 20-unit purchase at net 100 (5/unit,
    matching the seeded opening_cost) fully sold back out."""
    conn = sqlite3.connect(hammer_db)
    conn.execute(
        "INSERT INTO purchase_transactions(date_iso, doc_no, product_id, qty, net)"
        " VALUES ('2026-03-05','PO-869-A',869,20,100.0)")
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at) VALUES"
        " (869,'IN',20,'unit','PO-869-A','BSN ซื้อ','2026-03-05 08:00:00')")
    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at) VALUES"
        " (869,'OUT',-20,'unit','SO-869-A','BSN ขาย','2026-03-07 08:00:00')")
    conn.commit()
    conn.close()

    results = datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    by_pid = {r["pid"]: r for r in results}
    assert by_pid[869]["status"] == "applied"
    assert by_pid[869]["cost_price"] == 5.0

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    ledger = conn.execute(
        "SELECT event_type, unit_cost, wacc_after, stock_after"
        " FROM product_cost_ledger WHERE product_id=869 ORDER BY id").fetchall()
    row = conn.execute(
        "SELECT opening_cost, cost_price FROM products WHERE id=869").fetchone()
    conn.close()

    assert [r["event_type"] for r in ledger] == ["INITIAL", "PURCHASE"]
    assert ledger[0]["wacc_after"] == 5.0
    assert ledger[1]["unit_cost"] == 5.0
    assert ledger[1]["wacc_after"] == 5.0
    assert ledger[1]["stock_after"] == 20.0
    assert row["opening_cost"] == 5.0
    assert row["cost_price"] == 5.0


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


def test_w3_refuses_when_a_generic_non_pack_formula_already_exists_for_output(hammer_db, tmp_path):
    """mig 158's unique index only covers `[แพ็ค]`-prefixed names
    (`ux_conv_active_pack_per_output ... WHERE ... name LIKE '[แพ็ค]%'`), so
    nothing in the DB stops a GENERIC active formula from coexisting for the
    same output, and get_buildable sums both. This checkpoint's documented
    OLD baseline (plan §3) is 'no formula at all' for 268/269 — so ANY
    active formula for the output, not just [แพ็ค]-prefixed ones, must
    refuse rather than let this add a second recipe alongside it."""
    conn = sqlite3.connect(hammer_db)
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active)"
        " VALUES ('สูตรแปลงทั่วไป (ไม่ใช่แพ็ค)', 268, 1, 1)")
    fid = cur.lastrowid
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 270, 1, NULL)", (fid,))
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert len(after["formulas"]) == 1
    assert after["formulas"][0]["name"] == 'สูตรแปลงทั่วไป (ไม่ใช่แพ็ค)'
    assert after == before


def test_w3_state_old_when_output_has_no_active_formula_of_any_kind(hammer_db):
    """Control for the test above: confirm `_w3_state` still classifies as
    OLD (not unknown) when there is truly no active formula at all for the
    output — otherwise the "any active formula refuses" fix could have
    been written too broadly and always refuse."""
    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    try:
        state, info = datafix._w3_state(conn, datafix.W3_TARGETS[0])
    finally:
        conn.close()
    assert state == "old"
    assert info is None


def test_w3_fresh_matches_rejects_wrong_name():
    """Pins the independent post-commit verification to the SAME complete
    contract _w3_state's DONE gate checks — name, is_active, output_qty,
    and input shape — not a subset. Direct unit test of the extracted
    helper: run_w3's own INSERT always writes the correct name, so the
    integration path cannot itself produce a mismatch between the write
    and the fresh re-read; this pins the comparison logic directly."""
    target = datafix.W3_TARGETS[0]
    inputs = [
        {"product_id": target["component_pid"], "quantity": 1, "role": "component"},
        {"product_id": target["packaging_pid"], "quantity": 1, "role": "packaging"},
    ]
    right_row = {"name": target["name"], "is_active": 1, "output_qty": 1}
    wrong_row = {"name": "ชื่อผิด ไม่ตรงเป้าหมาย", "is_active": 1, "output_qty": 1}

    assert datafix._w3_fresh_matches(right_row, inputs, target) is True
    assert datafix._w3_fresh_matches(wrong_row, inputs, target) is False


def test_w3_refuses_when_existing_formula_has_correct_inputs_but_stale_name(hammer_db, tmp_path):
    """A [แพ็ค] formula for 268 with EXACTLY the right component/packaging
    shape (270 qty1 component, 869 qty1 packaging) but a NAME that still
    references the pre-W1 product name — e.g. built through the Phase 2 form
    before W1 renamed the product. Inputs matching alone must NOT read as
    "done": the name is part of the documented target contract, and a
    formula whose name references the old product name is exactly the
    silent-leftover this gate exists to catch."""
    conn = sqlite3.connect(hammer_db)
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active)"
        " VALUES (?, 268, 1, 1)",
        ("[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน",))  # missing #BSN01
    fid = cur.lastrowid
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 270, 1, 'component')", (fid,))
    conn.execute(
        "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
        " VALUES (?, 869, 1, 'packaging')", (fid,))
    conn.commit()
    conn.close()

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    # Not silently "fixed" or renamed, not skipped as done, not touched at all.
    assert after == before
    assert len(after["formulas"]) == 1
    assert after["formulas"][0]["name"] == "[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน"


def test_w3_refuses_when_existing_formula_has_correct_inputs_but_wrong_output_qty(hammer_db, tmp_path):
    """Same shape as above but for output_qty: name and inputs are exactly
    the target, output_qty is 2 instead of 1. Must refuse, not skip."""
    conn = sqlite3.connect(hammer_db)
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active)"
        " VALUES (?, 268, 2, 1)",
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

    before = _snap(hammer_db)
    with pytest.raises(datafix.CheckpointBaselineError):
        datafix.run_w3(hammer_db, _backup_dir(tmp_path), "test")
    after = _snap(hammer_db)

    assert after == before
    assert len(after["formulas"]) == 1
    assert after["formulas"][0]["output_qty"] == 2


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


# ── rehearse must refuse a real DB ───────────────────────────────────────────
# Without this guard the two modes differ ONLY in whether --confirm is typed,
# which makes `rehearse` an unguarded write path onto the real DB. Break-it-once:
# neutering is_protected_db_path() turns both tests below red (rc 0, prod path
# mutated) — verified before shipping.

def test_rehearse_refuses_the_prod_path_and_opens_nothing(tmp_path, monkeypatch):
    """The prod path is never even opened, so this asserts on a path that does
    not exist: a run that got past the guard would fail loudly instead."""
    ghost = str(tmp_path / "no-such-dir" / "inventory.db")
    monkeypatch.setattr(datafix, "_PROTECTED_DB_PATHS", (ghost,))
    rc = datafix.main(["--db-path", ghost, "--mode", "rehearse",
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 2
    assert not os.path.exists(ghost), "guard let the run open/create the protected DB"


def test_rehearse_refuses_a_protected_path_and_touches_nothing(hammer_db, tmp_path, monkeypatch):
    """Control: the SAME db that rehearses fine above is refused once it is
    listed as protected — so this pins the guard, not some unrelated failure."""
    before = _snap(hammer_db)
    monkeypatch.setattr(datafix, "_PROTECTED_DB_PATHS", (hammer_db,))
    rc = datafix.main(["--db-path", hammer_db, "--mode", "rehearse",
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 2
    assert _snap(hammer_db) == before


def test_live_with_confirm_still_allowed_on_a_protected_path(hammer_db, tmp_path, monkeypatch):
    """The guard must not lock out the intended prod flow: live + matching
    --confirm still runs on a protected path."""
    monkeypatch.setattr(datafix, "_PROTECTED_DB_PATHS", (hammer_db,))
    rc = datafix.main(["--db-path", hammer_db, "--mode", "live",
                       "--confirm", hammer_db,
                       "--backup-dir", _backup_dir(tmp_path)])
    assert rc == 0
    conn = sqlite3.connect(hammer_db)
    row = conn.execute("SELECT cost_price FROM products WHERE id=268").fetchone()
    conn.close()
    assert row[0] == 71.0


def test_real_prod_path_is_protected_by_default():
    """The shipped constant must actually cover the path this script will be
    pointed at on prod — the test above monkeypatches, this one does not."""
    assert datafix.is_protected_db_path("/data/inventory.db")
    assert not datafix.is_protected_db_path("/tmp/rehearsal-snapshot.db")


def test_w2_rolls_back_when_cost_price_is_right_but_the_ledger_was_never_written(
        hammer_db, tmp_path, monkeypatch):
    """The round-5 blocker, as a mutation test.

    A replay that sets `cost_price` to EXACTLY the target while writing no
    `product_cost_ledger` rows is the one shape the column checks cannot
    see. Before the fix this committed on the first run — the rerun gate
    would then classify it UNKNOWN and refuse, but prod would already have
    been mutated. The ledger is what W2 exists to rebuild; cost_price is
    only its derived output.

    Assert the FULL rollback, not merely that it raised: opening_cost and
    cost_price back at baseline for all three products, and no ledger rows
    conjured for them.
    """
    def fake_wacc_no_ledger(pid, conn=None, operation=None):
        expected = {t["pid"]: t["expected_cost_price"] for t in datafix.W2_TARGETS}[pid]
        # Exactly the target the postcondition's column check wants...
        conn.execute("UPDATE products SET cost_price=? WHERE id=?", (expected, pid))
        # ...and deliberately NO product_cost_ledger write.
        return expected

    # Control: the ledger really is absent for these pids to begin with, so
    # a passing assertion below cannot be an artifact of pre-existing rows.
    conn = sqlite3.connect(hammer_db)
    before_ledger = conn.execute(
        "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id IN (268,269,869)"
    ).fetchone()[0]
    conn.close()
    assert before_ledger == 0

    monkeypatch.setattr(datafix, "recalculate_product_wacc", fake_wacc_no_ledger)
    with pytest.raises(datafix.CheckpointPostconditionError) as exc:
        datafix.run_w2(hammer_db, _backup_dir(tmp_path), "test")
    assert "product_cost_ledger" in str(exc.value)

    conn = sqlite3.connect(hammer_db)
    conn.row_factory = sqlite3.Row
    for pid in (268, 269, 869):
        row = conn.execute(
            "SELECT opening_cost, cost_price FROM products WHERE id=?", (pid,)).fetchone()
        assert row["opening_cost"] == BASELINE[pid]["opening_cost"], pid
        assert row["cost_price"] == BASELINE[pid]["cost_price"], pid
    n = conn.execute(
        "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id IN (268,269,869)"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "rolled back, so no ledger rows should exist either"
