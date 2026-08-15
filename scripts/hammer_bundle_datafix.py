"""Phase 1 data fix — hammer แผง = อัน + การ์ด rename + bundle formulas.

See docs/plans/2026-08-14-hammer-pack-bundle-plan.md §5 "Phase 1" for the
full design + review history. This script does exactly the three checkpoints
that document assigns to Phase 1 — nothing else:

  W1  rename 268 / 269 / 271 through naming_cascade.save_product (the exact
      function behind /naming — it backs up, runs BEGIN IMMEDIATE, rebuilds
      product_name, and never touches sku_code). 271's edit closes a latent
      silent-rename trap: its stored name has a space where `series` has an
      underscore, so the NEXT unrelated /naming save on 271 would have
      silently renamed it.
  W2  seed products.opening_cost (869=5, 268=71, 269=73) and replay WACC via
      models.wacc.recalculate_product_wacc, so cost_price reads 71/73/5.
      This rewrites cost HISTORY, not just today's cost_price — Put approved
      this as a correction of a wrong opening valuation (plan §5 W2).
  W3  insert the two [แพ็ค] bundle formulas — 268 <- 270(component) +
      869(packaging), 269 <- 271(component) + 869(packaging) — validated
      through models.conversion_roles.validate_pack_inputs (mig 158).

Does NOT do W4 (the +18 card ADJUST and the two conversion runs on
269/268). That stays a human, one-submit-at-a-time action in the UI per the
plan's runbook — this script never touches stock_levels or transactions.

SAFETY
  Every checkpoint: read current state -> classify as
    DONE     already matches the target -> skip, mutate nothing
    OLD      matches the documented baseline -> proceed
    UNKNOWN  matches neither -> refuse, mutate nothing, exit non-zero
  never "fix" a surprise.

  W2 and W3 mutate inside ONE `BEGIN IMMEDIATE` each: assert every invariant
  BEFORE commit, roll back on any failure, commit, then re-verify on a FRESH
  connection (a post-commit check is confirmation, not protection — the one
  that matters is the one before COMMIT). W1 goes through save_product,
  which owns its own transaction and backup; this script instead gates
  BEFORE calling it (so the call is only ever made from the exact documented
  baseline) and re-reads on a fresh connection after, halting the whole run
  if the rendered name is not exactly what the plan says.

  Idempotent: re-running after every checkpoint is already applied reports
  every checkpoint "skipped (already applied)" and writes nothing —
  proven by tests/test_hammer_bundle_datafix.py.

USAGE
  Rehearsal (point at a `.backup` snapshot / tmp copy — never the live file):
    python hammer_bundle_datafix.py --db-path /tmp/rehearsal.db --mode rehearse

  Live (the real file; --confirm must repeat --db-path verbatim — a
  deliberate SECOND typing, not a flag, so a copy-pasted command line with a
  stale --db-path can never silently write to the wrong DB):
    python hammer_bundle_datafix.py --db-path /data/inventory.db --mode live \\
        --confirm /data/inventory.db

  There is no default db-path in either mode — it is always an explicit
  argument, so this can never fall back onto production by accident.

  stdlib + this app's own modules only (naming_cascade, models.wacc,
  models.conversion_roles, db_backup) — no pandas, no sqlite3 CLI — so it
  runs unmodified on prod via `railway ssh ... /opt/venv/bin/python`.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.join(_HERE, '..', 'inventory_app')
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import db_backup  # noqa: E402
import naming_cascade  # noqa: E402
from models import conversion_roles  # noqa: E402
from models.wacc import recalculate_product_wacc  # noqa: E402


# Paths this script refuses to mutate in `rehearse` mode. Without this the two
# modes differ only in whether --confirm is required, which makes `rehearse` an
# UNGUARDED write path onto the real DB — the "a rehearse|live flag that only
# changes printed output is a lie" trap in .claude/rules/verification-discipline.md.
# `/data/inventory.db` is prod (the Railway volume); the second is the shared dev
# DB other tabs run against. Rehearsals belong on a `.backup` snapshot.
_PROTECTED_DB_PATHS = (
    '/data/inventory.db',
    os.path.expanduser('~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db'),
)


def is_protected_db_path(path):
    """True when `path` resolves to a DB that `rehearse` must refuse."""
    resolved = os.path.realpath(os.path.abspath(path))
    return any(resolved == os.path.realpath(os.path.abspath(p))
               for p in _PROTECTED_DB_PATHS)


class CheckpointBaselineError(RuntimeError):
    """Current DB state matches neither the documented OLD baseline nor the
    already-applied NEW state — refuses to guess, changes nothing."""


class CheckpointPostconditionError(RuntimeError):
    """A checkpoint's mutation did not produce the exact expected result.
    For W1 this fires AFTER save_product has already committed (it owns its
    own transaction) — the run still halts so nothing downstream compounds
    the surprise, but recovery of this one checkpoint is manual (restore
    from the backup save_product just took)."""


# ── W1 — rename 268 / 269 / 271 ─────────────────────────────────────────────
# Only the COLUMNS actually written are checked for OLD/DONE classification
# (not the full row) — that's what makes pid 271 distinguishable: its
# rendered name is IDENTICAL before and after (only `series` moves from
# 'ตารางกันลื่น_FN' to 'ตารางกันลื่น FN'), so name-only detection could not
# tell "already applied" from "never touched".
W1_TARGETS = [
    dict(
        pid=268,
        old_fields={"series": None, "model": None},
        old_name="ฆ้อนด้ามไฟเบอร์ Sendai (แผง)",
        old_sku_code="HMR-HFBRV573-SD-PN",
        fields={"model": "#BSN01"},
        new_name="ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง)",
    ),
    dict(
        pid=269,
        old_fields={"series": "ตารางกันลื่น", "model": None},
        old_name="ฆ้อนด้ามไฟเบอร์ตารางกันลื่น Sendai (แผง)",
        old_sku_code="HMR-HFBRGRID-SD-SD028-PN",
        fields={"model": "#BSN02", "series": "ตารางกันลื่น FN"},
        new_name="ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02 (แผง)",
    ),
    dict(
        pid=271,
        old_fields={"series": "ตารางกันลื่น_FN"},
        old_name="ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02",  # unchanged text
        old_sku_code="HMR-HFBR-SD-S6F4B-#BSN02",
        fields={"series": "ตารางกันลื่น FN"},
        new_name="ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02",  # unchanged text
    ),
]


def _w1_state(conn, target):
    cols = list(target["fields"].keys())
    row = conn.execute(
        "SELECT {}, product_name, sku_code FROM products WHERE id=?".format(
            ", ".join(cols)),
        (target["pid"],),
    ).fetchone()
    if row is None:
        raise CheckpointBaselineError(f"W1 pid {target['pid']}: product not found")
    current = {c: row[c] for c in cols}
    # sku_code must NEVER move (W1's single most important invariant) — so
    # DONE checks it too, not just OLD: "renamed correctly, then some other
    # process moved the sku" must refuse on a re-run, not read as "already
    # applied". sku_code has one value across the whole OLD/DONE lifecycle
    # (save_product never touches it), so DONE compares against the same
    # old_sku_code the OLD branch below checks.
    if (current == target["fields"] and row["product_name"] == target["new_name"]
            and row["sku_code"] == target["old_sku_code"]):
        return "done", row
    old_expected = {c: target["old_fields"][c] for c in cols
                    if c in target["old_fields"]}
    # old_fields may cover a subset of `fields`' columns (pid 271 only
    # documents `series` as OLD even though `fields` only sets `series`
    # too) — every documented column must match for an OLD classification.
    # product_name/sku_code must ALSO match the documented OLD baseline: the
    # structured columns matching alone does not prove this is the exact
    # untouched row the plan was written against (e.g. a stale product_name
    # left by some other process). A mismatch here is UNKNOWN, not OLD.
    if (len(old_expected) == len(cols) and current == old_expected
            and row["product_name"] == target["old_name"]
            and row["sku_code"] == target["old_sku_code"]):
        return "old", row
    return "unknown", row


def run_w1(db_path, backup_dir, reason):
    """Rename checkpoint. Returns a list of per-product result dicts."""
    results = []
    for target in W1_TARGETS:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            state, row = _w1_state(conn, target)
        finally:
            conn.close()

        if state == "done":
            results.append({"pid": target["pid"], "status": "skipped (already applied)",
                            "name": row["product_name"]})
            continue
        if state == "unknown":
            current = {c: row[c] for c in target["fields"]}
            raise CheckpointBaselineError(
                f"W1 pid {target['pid']}: current {current} matches neither the "
                f"documented OLD baseline nor the already-applied state — refusing")

        sku_before = row["sku_code"]
        name_before = row["product_name"]
        result = naming_cascade.save_product(
            db_path, target["pid"], target["fields"],
            backup_dir=backup_dir, reason=reason)

        problems = []
        if result["new_name"] != target["new_name"]:
            problems.append(
                f"save_product produced {result['new_name']!r}, expected "
                f"{target['new_name']!r}")
        if result["sku_code"] != sku_before:
            problems.append(
                f"sku_code moved: {sku_before!r} -> {result['sku_code']!r}")

        # Fresh connection, independent of save_product's own — proves the
        # commit actually persisted, not just that the function returned.
        fresh = sqlite3.connect(db_path, timeout=10)
        fresh.row_factory = sqlite3.Row
        try:
            after = fresh.execute(
                "SELECT product_name, sku_code FROM products WHERE id=?",
                (target["pid"],)).fetchone()
        finally:
            fresh.close()
        if after["product_name"] != target["new_name"]:
            problems.append(
                f"fresh re-read product_name={after['product_name']!r}, expected "
                f"{target['new_name']!r}")
        if after["sku_code"] != sku_before:
            problems.append(
                f"fresh re-read sku_code={after['sku_code']!r}, expected "
                f"{sku_before!r} (unchanged)")

        if problems:
            raise CheckpointPostconditionError(
                f"W1 pid {target['pid']}: committed by save_product but the "
                f"result is wrong ({'; '.join(problems)}) — STOP, do not run "
                f"W2/W3; the backup save_product just took is the recovery path")

        results.append({"pid": target["pid"], "status": "applied",
                        "before": name_before, "after": after["product_name"]})
    return results


# ── W2 — cost basis (869 -> 5, 268 -> 71, 269 -> 73) ────────────────────────
W2_TARGETS = [
    dict(pid=869, old_opening_cost=0.0, old_cost_price=0.0,
        new_opening_cost=5.0, expected_cost_price=5.0),
    dict(pid=268, old_opening_cost=76.0, old_cost_price=76.0,
        new_opening_cost=71.0, expected_cost_price=71.0),
    dict(pid=269, old_opening_cost=0.0, old_cost_price=0.0,
        new_opening_cost=73.0, expected_cost_price=73.0),
]


def _w2_ledger_replayed(conn, target):
    """True iff product_cost_ledger PROVES the replay actually happened at
    the target cost — an INITIAL row seeded at the target unit cost/WACC,
    AND the ledger's own tail (the same ordering get_current_wacc uses)
    also reads the target WACC. products.cost_price matching alone does not
    prove this: the column could be right while the ledger backing it is
    missing (never replayed) or stale (a partial/corrupted write)."""
    initial = conn.execute(
        "SELECT unit_cost, wacc_after FROM product_cost_ledger"
        " WHERE product_id=? AND event_type='INITIAL'"
        " ORDER BY id DESC LIMIT 1", (target["pid"],)).fetchone()
    if (initial is None or initial["unit_cost"] != target["expected_cost_price"]
            or initial["wacc_after"] != target["expected_cost_price"]):
        return False
    tail = conn.execute(
        "SELECT wacc_after FROM product_cost_ledger WHERE product_id=?"
        " ORDER BY event_date DESC, id DESC LIMIT 1", (target["pid"],)).fetchone()
    return tail is not None and tail["wacc_after"] == target["expected_cost_price"]


def _w2_state(conn, target):
    row = conn.execute(
        "SELECT opening_cost, cost_price FROM products WHERE id=?",
        (target["pid"],)).fetchone()
    if row is None:
        raise CheckpointBaselineError(f"W2 pid {target['pid']}: product not found")
    if (row["opening_cost"] == target["new_opening_cost"]
            and row["cost_price"] == target["expected_cost_price"]):
        # Columns alone are not proof of a replay: require product_cost_ledger
        # to back them up too, or a product whose columns happen to match
        # while its ledger is missing/stale gets silently skipped on a
        # re-run — and W4 then proceeds on a stale cost history. Refuse
        # rather than silently re-replay; a human has to look.
        if _w2_ledger_replayed(conn, target):
            return "done", row
        return "unknown", row
    # opening_cost matching alone is not enough for OLD: cost_price must also
    # match the documented old value, or a drifted cost_price (e.g. from an
    # unrelated WACC recompute since the plan was written) would be silently
    # carried through as though it were the untouched baseline.
    if (row["opening_cost"] == target["old_opening_cost"]
            and row["cost_price"] == target["old_cost_price"]):
        return "old", row
    return "unknown", row


def run_w2(db_path, backup_dir, reason):
    """Cost-basis checkpoint. Returns a list of per-product result dicts."""
    probe = sqlite3.connect(db_path, timeout=10)
    probe.row_factory = sqlite3.Row
    try:
        results, to_apply = [], []
        for target in W2_TARGETS:
            state, row = _w2_state(probe, target)
            if state == "done":
                results.append({"pid": target["pid"], "status": "skipped (already applied)",
                                "cost_price": row["cost_price"]})
            elif state == "unknown":
                raise CheckpointBaselineError(
                    f"W2 pid {target['pid']}: opening_cost={row['opening_cost']} "
                    f"matches neither the documented OLD baseline nor the "
                    f"already-applied state — refusing")
            else:
                to_apply.append(target)
    finally:
        probe.close()

    if not to_apply:
        return results

    info, err = db_backup.safe_create_backup(reason, db_path=db_path, backup_dir=backup_dir)
    if err:
        raise RuntimeError(f"W2: backup failed, checkpoint aborted: {err}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        # Recompute-inside-the-transaction guard: refuse if a concurrent
        # writer moved any target's opening_cost since the probe above.
        for target in to_apply:
            state, row = _w2_state(conn, target)
            if state != "old":
                conn.execute("ROLLBACK")
                raise CheckpointBaselineError(
                    f"W2 pid {target['pid']}: state changed since the pre-check "
                    f"(now opening_cost={row['opening_cost']}) — refusing")

        for target in to_apply:
            conn.execute("UPDATE products SET opening_cost=? WHERE id=?",
                         (target["new_opening_cost"], target["pid"]))
        for target in to_apply:
            recalculate_product_wacc(target["pid"], conn=conn)

        problems = []
        after = {}
        for target in to_apply:
            row = conn.execute(
                "SELECT opening_cost, cost_price FROM products WHERE id=?",
                (target["pid"],)).fetchone()
            after[target["pid"]] = row
            if row["opening_cost"] != target["new_opening_cost"]:
                problems.append(
                    f"pid {target['pid']}: opening_cost={row['opening_cost']}, "
                    f"expected {target['new_opening_cost']}")
            if row["cost_price"] != target["expected_cost_price"]:
                problems.append(
                    f"pid {target['pid']}: cost_price={row['cost_price']}, "
                    f"expected {target['expected_cost_price']}")
            # The LEDGER is what W2 exists to rebuild; cost_price is only its
            # derived output. Checking the columns alone would let a replay
            # that set cost_price correctly while writing no ledger (or a
            # wrong one) COMMIT on the first run — the rerun gate would then
            # refuse, but prod would already have been mutated. Assert the
            # real invariant INSIDE the transaction, where refusing still
            # costs nothing (verification-discipline: the post-commit re-read
            # is confirmation, not protection).
            if not _w2_ledger_replayed(conn, target):
                problems.append(
                    f"pid {target['pid']}: product_cost_ledger does not prove the "
                    f"replay at {target['expected_cost_price']} (missing INITIAL, "
                    f"wrong unit_cost/wacc_after, or a tail that disagrees)")
        if problems:
            conn.execute("ROLLBACK")
            raise CheckpointPostconditionError("W2: " + "; ".join(problems))

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    # Independent re-read on a FRESH connection.
    fresh = sqlite3.connect(db_path, timeout=10)
    fresh.row_factory = sqlite3.Row
    try:
        for target in to_apply:
            row = fresh.execute(
                "SELECT opening_cost, cost_price FROM products WHERE id=?",
                (target["pid"],)).fetchone()
            if (row["opening_cost"] != target["new_opening_cost"]
                    or row["cost_price"] != target["expected_cost_price"]):
                raise CheckpointPostconditionError(
                    f"W2 pid {target['pid']}: fresh re-read opening_cost="
                    f"{row['opening_cost']} cost_price={row['cost_price']}, "
                    f"expected {target['new_opening_cost']}/"
                    f"{target['expected_cost_price']}")
            # The independent read must prove the SAME contract as the
            # pre-commit assert and the DONE gate, not a weaker one.
            if not _w2_ledger_replayed(fresh, target):
                raise CheckpointPostconditionError(
                    f"W2 pid {target['pid']}: committed, but the fresh re-read "
                    f"cannot prove product_cost_ledger was replayed at "
                    f"{target['expected_cost_price']} — STOP, do not run W3")
            results.append({"pid": target["pid"], "status": "applied",
                            "cost_price": row["cost_price"]})
    finally:
        fresh.close()
    return results


# ── W3 — the two [แพ็ค] bundle formulas ─────────────────────────────────────
W3_TARGETS = [
    dict(output_pid=268, component_pid=270, packaging_pid=869,
        name="[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน"),
    dict(output_pid=269, component_pid=271, packaging_pid=869,
        name="[แพ็ค] ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02 (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน"),
]


def _w3_inputs(conn, formula_id):
    return conn.execute(
        "SELECT product_id, quantity, role FROM conversion_formula_inputs"
        " WHERE formula_id=?", (formula_id,)).fetchall()


def _w3_matches_target(rows, target):
    if len(rows) != 2:
        return False
    by_role = {r["role"]: r for r in rows}
    comp, pack = by_role.get("component"), by_role.get("packaging")
    if comp is None or pack is None:
        return False
    return (comp["product_id"] == target["component_pid"] and comp["quantity"] == 1
            and pack["product_id"] == target["packaging_pid"] and pack["quantity"] == 1)


def _w3_fresh_matches(row, inputs, target):
    """The independent post-commit verification must prove the SAME complete
    contract _w3_state's DONE gate checks — name, is_active, output_qty, and
    the role/input shape — not a subset. A partial check here (e.g. skipping
    `name`) would let a committed row with the right inputs but a wrong name
    (drifted between the write and this re-read) pass as verified."""
    return (row is not None and row["name"] == target["name"]
            and row["is_active"] == 1 and row["output_qty"] == 1
            and _w3_matches_target(inputs, target))


def _w3_state(conn, target):
    # Deliberately NOT filtered to `[แพ็ค]%` names: this checkpoint's
    # documented OLD baseline (plan §3) is "no formula at all" for 268/269,
    # and mig 158's unique index only covers `[แพ็ค]`-prefixed names — a
    # generic active formula for the same output is not blocked by any DB
    # constraint, and get_buildable sums both. So ANY active formula for
    # this output — not just [แพ็ค]-prefixed ones — must refuse rather than
    # let this checkpoint add a second recipe alongside it. This is scoped
    # to the OUTPUT side only: 270/271/869 as INPUTS of some OTHER recipe
    # are untouched and unrestricted (a loose hammer or a card may
    # legitimately appear in other formulas).
    existing = conn.execute(
        "SELECT id, name, output_qty FROM conversion_formulas"
        " WHERE output_product_id=? AND is_active=1",
        (target["output_pid"],)).fetchall()
    if not existing:
        return "old", None
    # "done" requires the COMPLETE target contract, not just the inputs: a
    # formula with the right component/packaging shape but a stale `name`
    # (e.g. built through the Phase 2 form before W1 renamed the product) or
    # a wrong `output_qty` is NOT the documented target — it is UNKNOWN, and
    # this refuses rather than silently treating a near-miss as done.
    if (len(existing) == 1
            and existing[0]["name"] == target["name"]
            and existing[0]["output_qty"] == 1
            and _w3_matches_target(_w3_inputs(conn, existing[0]["id"]), target)):
        return "done", existing[0]["id"]
    return "unknown", existing


def run_w3(db_path, backup_dir, reason):
    """Bundle-formula checkpoint. Returns a list of per-output result dicts."""
    probe = sqlite3.connect(db_path, timeout=10)
    probe.row_factory = sqlite3.Row
    try:
        results, to_apply = [], []
        for target in W3_TARGETS:
            state, info = _w3_state(probe, target)
            if state == "done":
                results.append({"output_pid": target["output_pid"],
                                "status": "skipped (already applied)", "formula_id": info})
            elif state == "unknown":
                raise CheckpointBaselineError(
                    f"W3 output {target['output_pid']}: an existing active "
                    f"[แพ็ค] formula does not match the target shape — refusing "
                    f"({info})")
            else:
                to_apply.append(target)
    finally:
        probe.close()

    if not to_apply:
        return results

    info, err = db_backup.safe_create_backup(reason, db_path=db_path, backup_dir=backup_dir)
    if err:
        raise RuntimeError(f"W3: backup failed, checkpoint aborted: {err}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    new_ids = {}
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")

        for target in to_apply:
            state, info = _w3_state(conn, target)
            if state != "old":
                conn.execute("ROLLBACK")
                raise CheckpointBaselineError(
                    f"W3 output {target['output_pid']}: state changed since the "
                    f"pre-check — refusing ({info})")

        for target in to_apply:
            cur = conn.execute(
                "INSERT INTO conversion_formulas"
                " (name, output_product_id, output_qty, note, is_active)"
                " VALUES (?,?,?,?,1)",
                (target["name"], target["output_pid"], 1, None))
            fid = cur.lastrowid
            new_ids[target["output_pid"]] = fid
            # Deliberately inserted PACKAGING before COMPONENT — the reverse
            # of "business" reading order — so role is proven to never
            # depend on row/insertion order (conversion_formula_inputs has
            # no index at all; see models/conversion_roles.py's docstring).
            conn.execute(
                "INSERT INTO conversion_formula_inputs"
                " (formula_id, product_id, quantity, role) VALUES (?,?,?,?)",
                (fid, target["packaging_pid"], 1, conversion_roles.ROLE_PACKAGING))
            conn.execute(
                "INSERT INTO conversion_formula_inputs"
                " (formula_id, product_id, quantity, role) VALUES (?,?,?,?)",
                (fid, target["component_pid"], 1, conversion_roles.ROLE_COMPONENT))

        problems = []
        for target in to_apply:
            fid = new_ids[target["output_pid"]]
            row = conn.execute(
                "SELECT name, is_active, output_qty FROM conversion_formulas WHERE id=?",
                (fid,)).fetchone()
            inputs = _w3_inputs(conn, fid)
            try:
                conversion_roles.validate_pack_inputs(row["name"], row["is_active"], inputs)
            except conversion_roles.ConversionRoleError as e:
                problems.append(f"formula {fid}: {e}")
                continue
            comp_id = conversion_roles.component_product_id(row["name"], row["is_active"], inputs)
            if comp_id != target["component_pid"]:
                problems.append(
                    f"formula {fid}: component_product_id={comp_id}, expected "
                    f"{target['component_pid']}")
            if not _w3_matches_target(inputs, target):
                problems.append(f"formula {fid}: inputs do not match the target shape")
            if row["output_qty"] != 1:
                problems.append(f"formula {fid}: output_qty={row['output_qty']}, expected 1")
        if problems:
            conn.execute("ROLLBACK")
            raise CheckpointPostconditionError("W3: " + "; ".join(problems))

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    # Independent re-read on a FRESH connection.
    fresh = sqlite3.connect(db_path, timeout=10)
    fresh.row_factory = sqlite3.Row
    try:
        for target in to_apply:
            fid = new_ids[target["output_pid"]]
            row = fresh.execute(
                "SELECT name, is_active, output_qty FROM conversion_formulas WHERE id=?",
                (fid,)).fetchone()
            inputs = _w3_inputs(fresh, fid)
            if not _w3_fresh_matches(row, inputs, target):
                raise CheckpointPostconditionError(
                    f"W3 formula {fid}: fresh re-read does not match the target "
                    f"shape")
            results.append({"output_pid": target["output_pid"], "status": "applied",
                            "formula_id": fid, "name": row["name"]})
    finally:
        fresh.close()
    return results


CHECKPOINTS = [("W1", run_w1), ("W2", run_w2), ("W3", run_w3)]


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", required=True,
                  help="SQLite DB to operate on. No default, ever.")
    p.add_argument("--mode", required=True, choices=("rehearse", "live"))
    p.add_argument("--confirm", default=None,
                  help="Required for --mode live: must repeat --db-path exactly.")
    p.add_argument("--backup-dir", default=None,
                  help="Override backup dir (default: <dirname(db-path)>/backups).")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    if args.mode == "live" and args.confirm != args.db_path:
        print(
            "REFUSED: --mode live requires --confirm to repeat --db-path exactly.\n"
            f"  --db-path {args.db_path!r}\n  --confirm  {args.confirm!r}\n"
            "Nothing was opened, nothing changed.", file=sys.stderr)
        return 2

    # Without this, `rehearse` and `live` would differ only in whether --confirm
    # is typed — i.e. rehearse would be an unguarded write path onto the real DB.
    # A rehearsal belongs on a snapshot; mutating the real thing is `live`, which
    # costs the operator a deliberate second typing of the path.
    if args.mode == "rehearse" and is_protected_db_path(args.db_path):
        print(
            "REFUSED: --mode rehearse will not touch a real database.\n"
            f"  --db-path {args.db_path!r} resolves to a protected path.\n"
            "Rehearse against a .backup snapshot, or use --mode live --confirm <path>.\n"
            "Nothing was opened, nothing changed.", file=sys.stderr)
        return 2

    backup_dir = args.backup_dir or db_backup.default_backup_dir(args.db_path)
    reason = f"hammer_bundle_datafix_{args.mode}"

    for label, fn in CHECKPOINTS:
        print(f"\n=== {label} ===")
        try:
            rows = fn(args.db_path, backup_dir, reason)
        except (CheckpointBaselineError, CheckpointPostconditionError) as e:
            print(f"REFUSED at {label}: {e}", file=sys.stderr)
            return 1
        for row in rows:
            print(f"  {row}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
