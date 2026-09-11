#!/usr/bin/env python3
"""Catalog-pricing CSV importer for Sendy.

Reads a normalized catalog CSV (output of `normalize_base_price.py`) and
reconciles it into Sendy across three tables:

    products.base_sell_price    — UPDATE (skipped when CSV value matches DB)
    product_price_tiers          — INSERT new (product_id, qty_label) pairs,
                                    UPDATE in place when price/note differ,
                                    leave alone everything else
    promotions                   — close-and-reopen an offer that changed,
                                    preserve one that didn't (see below)

IDEMPOTENT by design (phase-2-finish-plan.md, PR B / 2c). A CSV row is the
desired state ONLY for the slots and tier labels it NAMES — anything it does
not mention is left alone. Re-running the identical file (any --batch-date)
reports all-zero counters and changes nothing.

One failure rule for the whole importer: every validation failure (a CSV
row producing two price-slot promos, two rows for one product, a row that
would silently half-close a `mixed` promo occupying both slots, a batch
date that would close a promo before its own start) ABORTS THE ENTIRE RUN
BEFORE ANY WRITE, naming the offending product/row/promo IDs, and exits
non-zero. Never skip-a-row-and-continue.

Default mode is DRY RUN (no writes persisted — the planner still runs for
real against a transaction, then rolls back). Pass --commit to persist.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inventory_app"))
from models import promotions as promo_models  # noqa: E402  (needs sys.path above)


class RunAbort(Exception):
    """A validation failure that must stop the whole run before any write.
    Carries a human-readable message naming the offending product/row/promo
    IDs (B3/B4/B5 in phase-2-finish-plan.md)."""


# Offer identity (phase-2-finish-plan.md B2) — promo_name is deliberately
# excluded: it carries the batch date and would make every re-import of the
# same offer look "changed" just because --batch-date differs.
_IDENTITY_FIELDS = (
    "promo_type", "discount_value", "bundle_buy", "bundle_free",
    "bundle_unit", "bundle_condition", "bundle_tiers_json",
    "gift_desc", "gift_qty",
)


def _offer_identity(row_or_dict):
    if isinstance(row_or_dict, sqlite3.Row):
        return tuple(row_or_dict[f] for f in _IDENTITY_FIELDS)
    return tuple(row_or_dict.get(f) for f in _IDENTITY_FIELDS)


def _add_days(iso_date: str, n: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=n)).isoformat()


# ── Helpers ─────────────────────────────────────────────────────────────────

def to_float(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load_csv(path: Path):
    """Yield CSV rows as dicts. Empty fields stay as empty strings."""
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            yield r


def _validate_batch_date(batch_date: str) -> None:
    try:
        date.fromisoformat(batch_date)
    except (ValueError, TypeError):
        raise ValueError(f"--batch-date must be ISO YYYY-MM-DD, got {batch_date!r}")


def _validate_batch_date_not_backdated(conn, batch_date: str) -> None:
    """Refuse a --batch-date that predates the FIRST catalogue batch this
    database ever recorded (issue #500 follow-up).

    #500: `2024-01-01` was typed as --batch-date on all three real catalogue
    runs, so every promotion claimed to have started before the ERP had any
    catalogue prices at all, and no promotion could ever move the price epoch.

    Why a refusal and not a warning: the importer already prints
    `Batch date: ...` on every run, dry-run and commit alike. That line was
    read past three times, so more output was never going to be the fix.

    Why the bound is `created_at` and not `date_start`: date_start is only what
    a batch CLAIMED, and all three bad runs claimed the same date — a
    date_start bound could never have fired.

    Why MIN and not MAX (Put, 2026-09-12): both refuse the real mistake
    identically (2024-01-01 is below either bound), and they differ only on a
    plausible back-date such as dating a batch 2026-07-01 when the last one
    was recorded 2026-08-11. MAX was implemented and measured first: it breaks
    22 existing tests in tests/test_import_catalog_pricing.py, which inherit
    the cloned dev DB's catalogue rows and then import with an older batch
    date. MIN breaks none and still catches the shape that actually occurred —
    a batch dated before this catalogue existed.

    The FIRST catalogue batch on a fresh DB has nothing to compare against and
    is allowed. This narrows the window for the mistake, it does not close it.

    Only `source = 'catalog-import'` rows set the bound — a promo Put typed by
    hand says nothing about when the last FILE was imported. There is
    deliberately no override flag: a genuine historical back-fill should be a
    considered code change, not a flag that turns the guard off in a hurry.
    """
    row = conn.execute(
        "SELECT MIN(date(created_at)) AS d FROM promotions "
        "WHERE source = 'catalog-import'"
    ).fetchone()
    first = row["d"] if row is not None else None
    if first and batch_date < first:
        raise RunAbort(
            f"--batch-date {batch_date} predates the first catalogue batch this "
            f"database ever recorded ({first}), so every promotion in this file "
            f"would claim to have started before the catalogue existed. Re-run "
            f"with the date this catalogue actually takes effect (usually today)."
        )


# ── Per-row planning (pure — no DB writes) ──────────────────────────────────

def _base_update_for_row(row: dict, current_base_sell_price: float):
    """New base_sell_price, or None if the CSV has no value or it matches DB."""
    csv_bsp = to_float(row.get("base_sell_price", ""))
    if csv_bsp is not None and csv_bsp != current_base_sell_price:
        return csv_bsp
    return None


def _tiers_from_row(row: dict) -> list:
    """[(qty_label, price, note), ...] from tier1/tier2/extra_tiers_json."""
    out = []
    for ql_key, pr_key, nt_key in [
        ("tier1_qty_label", "tier1_price", "tier1_note"),
        ("tier2_qty_label", "tier2_price", "tier2_note"),
    ]:
        ql = row.get(ql_key, "").strip()
        if ql:
            pr = to_float(row.get(pr_key, ""))
            if pr is not None:
                out.append((ql, pr, row.get(nt_key, "") or None))

    extra_json = row.get("extra_tiers_json", "").strip()
    if extra_json:
        try:
            for et in json.loads(extra_json):
                if "qty_label" in et and "price" in et:
                    out.append(
                        (et["qty_label"], float(et["price"]), et.get("note") or None)
                    )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Bad JSON → caller decides; we just drop the extras
            pass
    return out


def _promo_intents_from_row(row: dict, batch_date: str) -> list:
    """0-2 candidate promo offers this row names — NOT yet reconciled against
    the DB. Two independent sources: a numeric special_price → 'fixed', and
    the structured promo_type/promo_value/bundle/gift columns → any type.
    promo_name is cosmetic (derived from batch_date); identity ignores it."""
    intents = []

    csv_sp = to_float(row.get("special_price", ""))
    if csv_sp is not None and csv_sp > 0:
        intents.append({
            "promo_type": "fixed",
            "discount_value": csv_sp,
            "bundle_buy": None, "bundle_free": None,
            "bundle_unit": None, "bundle_condition": None,
            "bundle_tiers_json": None,
            "gift_desc": None, "gift_qty": None,
            "promo_name": f"catalog {batch_date} (special_price)",
        })

    pt = row.get("promo_type", "").strip()
    if pt:
        intents.append({
            "promo_type": pt,
            "discount_value": to_float(row.get("promo_value", "")),
            "bundle_buy": to_int(row.get("bundle_buy", "")),
            "bundle_free": to_int(row.get("bundle_free", "")),
            "bundle_unit": row.get("bundle_unit", "").strip() or None,
            "bundle_condition": row.get("bundle_condition", "").strip() or None,
            "bundle_tiers_json": row.get("bundle_tiers_json", "").strip() or None,
            "gift_desc": row.get("gift_desc", "").strip() or None,
            "gift_qty": row.get("gift_qty", "").strip() or None,
            "promo_name": f"catalog {batch_date} (promo)",
        })

    return intents


def plan_writes_for_row(row: dict, current_base_sell_price: float, batch_date: str):
    """What this ONE row names, as data — not yet reconciled against DB
    state (that's `_reconcile_tiers` / `_reconcile_promos`).

    {
      'update_base': (new_value,) or None,
      'tier_inserts': [(qty_label, price, note), ...],
      'promo_intents': [{'promo_type', 'discount_value', ..., 'promo_name'}, ...],
    }
    """
    base = _base_update_for_row(row, current_base_sell_price)
    return {
        "update_base": (base,) if base is not None else None,
        "tier_inserts": _tiers_from_row(row),
        "promo_intents": _promo_intents_from_row(row, batch_date),
    }


# ── File-shape validation (B4/B5) — pure CSV shape, before any DB read ──────

def _validate_file_shape(conn, rows, batch_date):
    """Structural checks that need no table state: one row per product, no
    duplicate tier label within a row, no two price-slot promo intents in
    one row. Raises RunAbort naming the offending row(s)/product before any
    DB read of base/tiers/promos happens. `conn` is used only to evaluate
    promo_slots_for's throwaway 1-row SELECT — no table is touched."""
    seen_pids = {}
    for i, r in enumerate(rows):
        pid_raw = r.get("product_id", "").strip()
        try:
            pid = int(pid_raw)
        except ValueError:
            continue  # non-integer product_id — skipped later, not this file's problem

        if pid in seen_pids:
            raise RunAbort(
                f"duplicate product_id {pid}: rows {seen_pids[pid]} and {i} both "
                f"reference it — one row per product only"
            )
        seen_pids[pid] = i

        tiers = _tiers_from_row(r)
        labels = [ql for ql, _, _ in tiers]
        dupes = sorted({l for l in labels if labels.count(l) > 1})
        if dupes:
            raise RunAbort(
                f"row {i} (product_id {pid}): duplicate tier qty_label {dupes} "
                f"within the same row"
            )

        intents = _promo_intents_from_row(r, batch_date)
        price_slot_count = 0
        for it in intents:
            occ_price, _ = promo_models.promo_slots_for(
                conn, it["promo_type"], it["discount_value"],
                it["bundle_buy"], it["gift_desc"])
            if occ_price:
                price_slot_count += 1
        if price_slot_count > 1:
            raise RunAbort(
                f"row {i} (product_id {pid}): {price_slot_count} price-slot promo "
                f"intents in one row (special_price + promo_type column both "
                f"resolve to the price slot) — cannot know which one the "
                f"catalogue meant"
            )


# ── Tier reconciliation (B0) ─────────────────────────────────────────────────

def _reconcile_tiers(conn, product_id, csv_tiers):
    """ops: (product_id, 'insert'|'update', tier_id_or_None, qty_label,
    price, note, price_changed). A tier present in the DB but absent from
    the CSV is left alone (not returned as an op)."""
    existing = {
        row["qty_label"]: row
        for row in conn.execute(
            "SELECT id, qty_label, price, note FROM product_price_tiers "
            "WHERE product_id=?", (product_id,)
        ).fetchall()
    }
    ops = []
    for ql, price, note in csv_tiers:
        cur = existing.get(ql)
        if cur is None:
            ops.append((product_id, "insert", None, ql, price, note, True))
            continue
        price_changed = cur["price"] != price
        note_changed = (cur["note"] or None) != (note or None)
        if price_changed or note_changed:
            ops.append((product_id, "update", cur["id"], ql, price, note, price_changed))
        # else: identical — no-op, nothing appended
    return ops


# ── Promo reconciliation (B2/B3/B1) ──────────────────────────────────────────

def _reconcile_promos(conn, product_id, intents, batch_date):
    """(close_ops, insert_ops) or raises RunAbort.

    close_ops:  [(product_id, promo_id, close_date), ...]
    insert_ops: [(product_id, full_promo_dict), ...]
    """
    if not intents:
        return [], []  # B7: a row with no promo columns leaves existing promos alone

    for it in intents:
        it["_price"], it["_qty"] = promo_models.promo_slots_for(
            conn, it["promo_type"], it["discount_value"],
            it["bundle_buy"], it["gift_desc"])
    union_price = any(it["_price"] for it in intents)
    union_qty = any(it["_qty"] for it in intents)

    # A "live occupant" is is_active=1 AND not already date-closed as of
    # batch_date. Closing only ever sets date_end (2a's rule: never flip
    # is_active early), so an already-closed row stays is_active=1 forever
    # — without this filter a row this importer closed in an EARLIER run
    # keeps coming back as a candidate on every later run and gets
    # re-touched (found via the real-catalog rehearsal: a product carrying
    # two independent single-slot promos had its already-closed one
    # re-closed a second time on a subsequent run, breaking idempotency).
    occupants = conn.execute(
        "SELECT * FROM promotions WHERE product_id=? AND is_active=1 "
        "AND (date_end IS NULL OR date_end >= ?)", (product_id, batch_date)
    ).fetchall()

    touched = []  # (occ_row, occ_price, occ_qty)
    for occ in occupants:
        occ_price, occ_qty = promo_models.promo_slots_for(
            conn, occ["promo_type"], occ["discount_value"],
            occ["bundle_buy"], occ["gift_desc"])
        if not (occ_price or occ_qty):
            continue
        if not ((occ_price and union_price) or (occ_qty and union_qty)):
            continue  # this occupant's slot(s) aren't touched by this row at all

        # B3: an occupant's own footprint must be FULLY covered by what this
        # row replaces — otherwise closing it would silently drop the slot
        # the row does not provide a replacement for.
        if (occ_price and not union_price) or (occ_qty and not union_qty):
            occ_slots = "price+qty" if (occ_price and occ_qty) else ("price" if occ_price else "qty")
            row_slots = ("price+qty" if (union_price and union_qty)
                         else ("price" if union_price else "qty"))
            raise RunAbort(
                f"product {product_id}: promo {occ['id']} ({occ['promo_type']}) "
                f"occupies {occ_slots}, but this row's promo(s) only cover "
                f"{row_slots} — closing it would silently drop the uncovered "
                f"slot. Clear/split it by hand first."
            )
        touched.append((occ, occ_price, occ_qty))

    # Match each intent to an occupant that exactly matches its slot-set AND
    # its offer identity → preserved (no write for either side).
    preserved_intent_idx = set()
    preserved_occ_ids = set()
    for idx, it in enumerate(intents):
        it_slots = (it["_price"], it["_qty"])
        for occ, occ_price, occ_qty in touched:
            if occ["id"] in preserved_occ_ids:
                continue
            if (occ_price, occ_qty) == it_slots and _offer_identity(occ) == _offer_identity(it):
                preserved_intent_idx.add(idx)
                preserved_occ_ids.add(occ["id"])
                break

    close_date = _add_days(batch_date, -1)
    close_ops = []
    for occ, _occ_price, _occ_qty in touched:
        if occ["id"] in preserved_occ_ids:
            continue
        existing_start = occ["date_start"] or "0000-01-01"
        if close_date < existing_start:
            raise RunAbort(
                f"product {product_id}: --batch-date {batch_date} would close "
                f"promo {occ['id']} at {close_date}, before its own date_start "
                f"{occ['date_start']} — refusing to backdate"
            )
        close_ops.append((product_id, occ["id"], close_date))

    insert_ops = []
    for idx, it in enumerate(intents):
        if idx in preserved_intent_idx:
            continue
        full = {k: v for k, v in it.items() if not k.startswith("_")}
        full["date_start"] = batch_date
        full["date_end"] = None
        full["source"] = "catalog-import"
        insert_ops.append((product_id, full))

    return close_ops, insert_ops


# ── Pass 1: build the full plan (reads only — no writes) ────────────────────

def _build_ops(conn, rows_all, batch_date, limit):
    csv_pids = []
    skipped_non_int = []
    for r in rows_all:
        pid_raw = r.get("product_id", "").strip()
        try:
            csv_pids.append(int(pid_raw))
        except ValueError:
            skipped_non_int.append(r.get("sku_code", "(unknown)"))

    bsp_lookup = {}
    sendy_known_pids = set()
    if csv_pids:
        # SQLite has a 999-param limit by default; chunk if larger
        for i in range(0, len(csv_pids), 500):
            chunk = csv_pids[i:i + 500]
            qmarks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT id, base_sell_price FROM products WHERE id IN ({qmarks})",
                chunk):
                bsp_lookup[row["id"]] = row["base_sell_price"]
                sendy_known_pids.add(row["id"])

    skipped_missing_pids = []
    row_plans = []  # (row, pid)
    flagged_rows = []
    for r in rows_all:
        pid_raw = r.get("product_id", "").strip()
        try:
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid not in sendy_known_pids:
            skipped_missing_pids.append((pid, r.get("sku_code", "(unknown)")))
            continue
        row_plans.append((r, pid))
        if r.get("normalize_notes", "").strip():
            flagged_rows.append(r)

    if limit is not None:
        row_plans = row_plans[:limit]

    ops = {"base": [], "tiers": [], "promo_close": [], "promo_insert": []}

    for r, pid in row_plans:
        current_base = bsp_lookup[pid]
        plan = plan_writes_for_row(r, current_base, batch_date)

        if plan["update_base"] is not None:
            ops["base"].append((pid, plan["update_base"][0]))

        ops["tiers"].extend(_reconcile_tiers(conn, pid, plan["tier_inserts"]))

        close_ops, insert_ops = _reconcile_promos(conn, pid, plan["promo_intents"], batch_date)
        ops["promo_close"].extend(close_ops)
        ops["promo_insert"].extend(insert_ops)

    meta = {
        "rows_processed": len(row_plans),
        "rows_flagged": len(flagged_rows),
        "flagged_rows": flagged_rows,
        "skipped_non_int": skipped_non_int,
        "skipped_missing_pids": skipped_missing_pids,
    }
    return ops, meta


# ── Pass 2: execute the plan (writes) ────────────────────────────────────────

def _execute_ops(conn, ops):
    for pid, new_price in ops["base"]:
        conn.execute(
            "UPDATE products SET base_sell_price=? WHERE id=?", (new_price, pid))

    for pid, kind, tier_id, ql, price, note, _price_changed in ops["tiers"]:
        if kind == "insert":
            conn.execute(
                "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
                "VALUES (?, ?, ?, ?)", (pid, ql, price, note))
        else:
            conn.execute(
                "UPDATE product_price_tiers SET price=?, note=?, "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (price, note, tier_id))

    for pid, promo_id, close_date in ops["promo_close"]:
        conn.execute(
            "UPDATE promotions SET date_end=? WHERE id=?", (close_date, promo_id))

    inserted_promo_ids = []
    for pid, full in ops["promo_insert"]:
        cur = conn.execute("""
            INSERT INTO promotions (
                product_id, promo_name, promo_type, discount_value,
                date_start, date_end, source,
                bundle_buy, bundle_free, bundle_unit, bundle_condition,
                bundle_tiers_json, gift_desc, gift_qty
            ) VALUES (
                :product_id, :promo_name, :promo_type, :discount_value,
                :date_start, :date_end, :source,
                :bundle_buy, :bundle_free, :bundle_unit, :bundle_condition,
                :bundle_tiers_json, :gift_desc, :gift_qty
            )
        """, {**full, "product_id": pid})
        inserted_promo_ids.append(cur.lastrowid)

    return inserted_promo_ids


def _assert_invariants(conn, ops, inserted_promo_ids):
    """Re-read every planned write on the SAME connection, inside the same
    transaction, before commit/rollback is decided — verification-discipline
    ("write-scripts: verify by re-read in the SAME step")."""
    problems = []

    for pid, new_price in ops["base"]:
        got = conn.execute(
            "SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        if got != new_price:
            problems.append(f"product {pid}: base_sell_price expected {new_price}, got {got}")

    for pid, kind, tier_id, ql, price, note, _pc in ops["tiers"]:
        got = conn.execute(
            "SELECT price, note FROM product_price_tiers WHERE product_id=? AND qty_label=?",
            (pid, ql)).fetchone()
        if got is None or got["price"] != price or (got["note"] or None) != (note or None):
            problems.append(
                f"product {pid} tier {ql!r}: expected price={price} note={note!r}, "
                f"got {dict(got) if got else None}")

    for pid, promo_id, close_date in ops["promo_close"]:
        got = conn.execute(
            "SELECT date_end FROM promotions WHERE id=?", (promo_id,)).fetchone()[0]
        if got != close_date:
            problems.append(f"promo {promo_id}: expected date_end={close_date}, got {got}")

    if len(inserted_promo_ids) != len(ops["promo_insert"]):
        problems.append(
            f"promo insert count mismatch: planned {len(ops['promo_insert'])}, "
            f"executed {len(inserted_promo_ids)}")
    # /scrutinize: existence-by-id was the only check here, while base prices,
    # tiers and closes all had their VALUES re-read. That asymmetry mattered
    # most for `date_start`: it is the evidence epoch price_lookup reads, and it
    # is deliberately NOT part of _offer_identity — so a wrong date_start would
    # be invisible to the idempotency contract too (the next run still matches
    # the offer and reports zero) and would silently shift the window every
    # price answer is computed in. Re-read the values, not just the row.
    # inserted_promo_ids is appended in ops["promo_insert"] order (_execute_ops).
    for promo_id, (pid, full) in zip(inserted_promo_ids, ops["promo_insert"]):
        got = conn.execute(
            "SELECT * FROM promotions WHERE id=?", (promo_id,)).fetchone()
        if got is None:
            problems.append(f"promo {promo_id}: not found after insert")
            continue
        for field in ('product_id', 'date_start', 'date_end', 'source') + _IDENTITY_FIELDS:
            want = pid if field == 'product_id' else full.get(field)
            if got[field] != want:
                problems.append(
                    f"promo {promo_id} (product {pid}): {field} expected "
                    f"{want!r}, got {got[field]!r}")

    if problems:
        raise RuntimeError(
            "post-write invariant check failed (rolled back):\n  " + "\n  ".join(problems))


# ── Printing ─────────────────────────────────────────────────────────────────

def _print_summary(mode, stats, ops, meta, show_sample, verbose):
    if not verbose:
        return
    print()
    print("=" * 72)
    print(f"=== {mode} — {'will write to DB' if mode == 'COMMIT' else 'no writes persisted'} ===")
    print("=" * 72)
    print(f"\nRows processed:          {stats['rows_processed']}")
    print(f"Rows flagged for review: {stats['rows_flagged']}")
    if meta["skipped_non_int"]:
        print(f"Skipped (non-integer product_id): {len(meta['skipped_non_int'])}")
    if meta["skipped_missing_pids"]:
        print(f"Skipped (product_id not in Sendy): {len(meta['skipped_missing_pids'])}")
    print()
    print(f"products.base_sell_price:  {stats['base_updated']} updated")
    print(f"product_price_tiers:       {stats['tiers_inserted']} inserted / "
          f"{stats['tiers_updated']} updated")
    print(f"promotions:                {stats['promos_closed']} closed / "
          f"{stats['promos_inserted']} inserted")

    if meta["flagged_rows"]:
        print()
        print(f"⚠ Flagged rows ({len(meta['flagged_rows'])}) — auto-imported but review the notes:")
        for r in meta["flagged_rows"]:
            print(f"  {r['sku_code']:50s} {r['normalize_notes']}")

    if show_sample and ops["promo_insert"]:
        print()
        print(f"📄 Sample of first {min(show_sample, len(ops['promo_insert']))} new promo rows:")
        for pid, full in ops["promo_insert"][:show_sample]:
            print(f"  pid={pid} {full['promo_type']} val={full.get('discount_value')} "
                  f"start={full['date_start']}")


# ── Main import flow ────────────────────────────────────────────────────────

def run_import(csv_path: Path, db_path: Path, commit: bool, limit: Optional[int],
               show_sample: int = 10, verbose: bool = True,
               batch_date: Optional[str] = None, _after_begin_hook=None):
    """Plan + reconcile + (optionally persist) the import. Returns a stats
    dict. Both dry-run and commit run the IDENTICAL planner against a real
    `BEGIN IMMEDIATE` transaction — dry-run just rolls back at the end
    instead of committing (phase-2-finish-plan.md B5)."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    if batch_date is None:
        raise ValueError("batch_date is required (pass --batch-date, ISO YYYY-MM-DD)")
    _validate_batch_date(batch_date)

    rows_all = list(load_csv(csv_path))
    if verbose:
        print(f"Loaded {len(rows_all)} CSV rows from {csv_path}")
        print(f"Batch date: {batch_date}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # B5: validate the file's own shape BEFORE any DB read of state.
        _validate_file_shape(conn, rows_all, batch_date)
        _validate_batch_date_not_backdated(conn, batch_date)

        conn.execute("BEGIN IMMEDIATE")
        if _after_begin_hook is not None:
            _after_begin_hook(conn)

        # Pass 1 — plan (reads only; raises RunAbort before any write).
        ops, meta = _build_ops(conn, rows_all, batch_date, limit)

        stats = {
            "rows_processed": meta["rows_processed"],
            "rows_flagged": meta["rows_flagged"],
            "base_updated": len(ops["base"]),
            "tiers_inserted": sum(1 for o in ops["tiers"] if o[1] == "insert"),
            "tiers_updated": sum(1 for o in ops["tiers"] if o[1] == "update"),
            "promos_closed": len(ops["promo_close"]),
            "promos_inserted": len(ops["promo_insert"]),
        }

        today_iso = date.today().isoformat()
        tier_epoch_risk = any(o[-1] for o in ops["tiers"])  # any price-changing tier op
        if tier_epoch_risk and batch_date != today_iso and verbose:
            print(
                f"\n⚠ --batch-date {batch_date} != today ({today_iso}): tier PRICE "
                f"changes in this run are epoch-dated at IMPORT TIME ({today_iso}), "
                f"not --batch-date, while promo changes carry {batch_date}. "
                f"See phase-2-finish-plan.md B0."
            )

        # Pass 2 — execute, then verify by re-read in the SAME transaction.
        inserted_promo_ids = _execute_ops(conn, ops)
        _assert_invariants(conn, ops, inserted_promo_ids)

        mode = "COMMIT" if commit else "DRY RUN"
        _print_summary(mode, stats, ops, meta, show_sample, verbose)

        if commit:
            conn.commit()
            if verbose:
                print("\n✅ COMMIT complete.")
        else:
            conn.rollback()
            if verbose:
                print("\nDRY RUN complete (rolled back). To commit, re-run with: --commit")
        return stats

    except RunAbort as e:
        conn.rollback()
        if verbose:
            print(f"\n❌ ABORTED — nothing written. {e}", file=sys.stderr)
        raise
    except Exception as e:
        conn.rollback()
        if verbose:
            print(f"\n❌ FAILED — transaction rolled back. Error: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


def backup_db(db_path: Path) -> Path:
    """Copy the DB to a timestamped backup file alongside it. Returns the backup path."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = db_path.parent / f"{db_path.name}.backup-pre-catalog-import-{ts}"
    shutil.copy2(db_path, dst)
    # Also copy -wal/-shm sidecars if they exist
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            shutil.copy2(side, str(dst) + suffix)
    return dst


def _iso_date(s):
    try:
        date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be ISO YYYY-MM-DD, got {s!r}")
    return s


def main():
    parser = argparse.ArgumentParser(
        description="Catalog-pricing CSV importer for Sendy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--csv", required=True, type=Path,
                        help="Path to normalized catalog CSV")
    parser.add_argument("--db", type=Path,
                        default=Path(__file__).parent.parent / "inventory_app/instance/inventory.db",
                        help="Path to Sendy inventory.db (default: ../inventory_app/instance/inventory.db)")
    parser.add_argument("--batch-date", required=True, type=_iso_date,
                        help="ISO batch date (YYYY-MM-DD). Written ONLY to "
                             "promotions.date_start for any new/changed promo "
                             "row in this run (base prices and tiers carry no "
                             "date). Never inferred or defaulted.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write to the DB (default is dry-run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N CSV rows (useful with dry-run for sampling)")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip the automatic DB backup before --commit (NOT recommended)")
    parser.add_argument("--sample", type=int, default=10,
                        help="Number of sample rows to print in the diff preview (default 10)")
    args = parser.parse_args()

    csv_path = args.csv.resolve()
    db_path = args.db.resolve()

    if args.commit and not args.no_backup:
        backup_path = backup_db(db_path)
        print(f"📦 DB backed up to: {backup_path}")

    try:
        run_import(
            csv_path=csv_path,
            db_path=db_path,
            commit=args.commit,
            limit=args.limit,
            show_sample=args.sample,
            batch_date=args.batch_date,
        )
    except RunAbort:
        # run_import already printed "❌ ABORTED — nothing written. <reason>" to
        # stderr with the offending product/row/promo IDs. This is a hand-run
        # operator tool: dumping a Python traceback on top of that reads as a
        # crash rather than the deliberate refusal it is. Exit non-zero (the
        # one-failure rule) without the traceback.
        sys.exit(1)


if __name__ == "__main__":
    main()
