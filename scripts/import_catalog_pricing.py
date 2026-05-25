#!/usr/bin/env python3
"""Catalog-pricing CSV importer for Sendy.

Reads a normalized catalog CSV (output of `normalize_base_price.py`) and writes
its data into Sendy across three tables:

    products.base_sell_price    — UPDATE (skipped when CSV value matches DB)
    product_price_tiers          — INSERT (per tier; UNIQUE(product_id, qty_label))
    promotions                   — INSERT (per row's special_price + per row's promo)

Designed for ONE-SHOT execution. Re-running on an already-imported CSV will
fail loudly via the UNIQUE constraint on product_price_tiers and via duplicate
promo rows on promotions. For re-import, manually clear the relevant rows by
their promo_name (e.g. `DELETE FROM promotions WHERE promo_name LIKE 'catalog
2026-05-25%'`) first.

Default mode is DRY RUN (no writes). Pass --commit to actually write.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# Promo-name label embedded in INSERTed promotion rows. Lets you filter for
# re-cleanup later: `DELETE FROM promotions WHERE promo_name LIKE 'catalog 2026-05-25%'`.
CATALOG_BATCH_DATE = "2026-05-25"
PROMO_NAME_FROM_SPECIAL_PRICE = f"catalog {CATALOG_BATCH_DATE} (special_price)"
PROMO_NAME_FROM_PROMO_COL     = f"catalog {CATALOG_BATCH_DATE} (promo)"


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


# ── Per-row write planning ──────────────────────────────────────────────────

def plan_writes_for_row(row: dict, current_base_sell_price: float):
    """Return a dict describing what would be written for this CSV row.

    {
      'update_base': (new_value,) or None,
      'tier_inserts': [(qty_label, price, note), ...],
      'promo_inserts': [
        {
          'promo_type', 'discount_value', 'bundle_buy', 'bundle_free',
          'bundle_unit', 'bundle_condition', 'bundle_tiers_json',
          'gift_desc', 'gift_qty', 'promo_name'
        },
        ...
      ],
    }
    """
    plan = {"update_base": None, "tier_inserts": [], "promo_inserts": []}

    # 1) base_sell_price UPDATE — only if CSV has a value AND it differs from DB
    csv_bsp = to_float(row.get("base_sell_price", ""))
    if csv_bsp is not None and csv_bsp != current_base_sell_price:
        plan["update_base"] = (csv_bsp,)

    # 2) Tier INSERTs — tier1, tier2, extra_tiers_json entries
    for ql_key, pr_key, nt_key in [
        ("tier1_qty_label", "tier1_price", "tier1_note"),
        ("tier2_qty_label", "tier2_price", "tier2_note"),
    ]:
        ql = row.get(ql_key, "").strip()
        if ql:
            pr = to_float(row.get(pr_key, ""))
            if pr is not None:
                plan["tier_inserts"].append((ql, pr, row.get(nt_key, "") or None))

    extra_json = row.get("extra_tiers_json", "").strip()
    if extra_json:
        try:
            for et in json.loads(extra_json):
                if "qty_label" in et and "price" in et:
                    plan["tier_inserts"].append(
                        (et["qty_label"], float(et["price"]), et.get("note") or None)
                    )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Bad JSON → caller decides; we just drop the extras
            pass

    # 3) Promo from special_price (numeric → 'fixed' promo)
    csv_sp = to_float(row.get("special_price", ""))
    if csv_sp is not None and csv_sp > 0:
        plan["promo_inserts"].append({
            "promo_type": "fixed",
            "discount_value": csv_sp,
            "bundle_buy": None, "bundle_free": None,
            "bundle_unit": None, "bundle_condition": None,
            "bundle_tiers_json": None,
            "gift_desc": None, "gift_qty": None,
            "promo_name": PROMO_NAME_FROM_SPECIAL_PRICE,
        })

    # 4) Promo from promo_type column (any type)
    pt = row.get("promo_type", "").strip()
    if pt:
        plan["promo_inserts"].append({
            "promo_type": pt,
            "discount_value": to_float(row.get("promo_value", "")),
            "bundle_buy": to_int(row.get("bundle_buy", "")),
            "bundle_free": to_int(row.get("bundle_free", "")),
            "bundle_unit": row.get("bundle_unit", "").strip() or None,
            "bundle_condition": row.get("bundle_condition", "").strip() or None,
            "bundle_tiers_json": row.get("bundle_tiers_json", "").strip() or None,
            "gift_desc": row.get("gift_desc", "").strip() or None,
            "gift_qty": row.get("gift_qty", "").strip() or None,
            "promo_name": PROMO_NAME_FROM_PROMO_COL,
        })

    return plan


# ── Main import flow ────────────────────────────────────────────────────────

def run_import(csv_path: Path, db_path: Path, commit: bool, limit: Optional[int],
               show_sample: int = 10, verbose: bool = True):
    """Plan + (optionally) execute the import. Returns a stats dict."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    rows_all = list(load_csv(csv_path))
    if verbose:
        print(f"Loaded {len(rows_all)} CSV rows from {csv_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Snapshot product baseline prices for all referenced product_ids
    csv_pids = []
    skipped_non_int = []
    for r in rows_all:
        pid_raw = r.get("product_id", "").strip()
        try:
            csv_pids.append(int(pid_raw))
        except ValueError:
            skipped_non_int.append(r.get("sku_code", "(unknown)"))

    if verbose and skipped_non_int:
        print(f"Skipping {len(skipped_non_int)} rows with non-integer product_id "
              f"(likely new products not yet in Sendy)")

    # Bulk-fetch current base_sell_price for all referenced pids
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
    plans = []  # list of (row, plan, pid)
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
        current = bsp_lookup[pid]
        plan = plan_writes_for_row(r, current)
        plans.append((r, plan, pid))
        if r.get("normalize_notes", "").strip():
            flagged_rows.append(r)

    if verbose and skipped_missing_pids:
        print(f"Skipping {len(skipped_missing_pids)} rows whose product_id is not in Sendy")

    if limit is not None:
        plans = plans[:limit]
        if verbose:
            print(f"--limit {limit} → processing first {len(plans)} rows only")

    # ── Compute summary stats ──────────────────────────────────────────────
    stats = {
        "rows_processed": len(plans),
        "base_updates_total": 0,
        "base_updates_from_zero": 0,
        "base_updates_from_nonzero": 0,
        "base_noops": 0,
        "tier_inserts": 0,
        "tier_inserts_tier1": 0,
        "tier_inserts_tier2": 0,
        "tier_inserts_extra": 0,
        "promo_inserts_total": 0,
        "promo_inserts_by_type": {"percent": 0, "fixed": 0, "bundle": 0,
                                  "mixed": 0, "gift": 0},
        "promo_inserts_from_special_price": 0,
        "promo_inserts_from_promo_col": 0,
        "rows_flagged": len(flagged_rows),
    }
    for row, plan, pid in plans:
        if plan["update_base"]:
            stats["base_updates_total"] += 1
            current = bsp_lookup[pid]
            if current == 0:
                stats["base_updates_from_zero"] += 1
            else:
                stats["base_updates_from_nonzero"] += 1
        stats["tier_inserts"] += len(plan["tier_inserts"])
        # Sub-counts for tier1/tier2/extra
        tier1_present = bool(row.get("tier1_qty_label", "").strip())
        tier2_present = bool(row.get("tier2_qty_label", "").strip())
        if tier1_present:
            stats["tier_inserts_tier1"] += 1
        if tier2_present:
            stats["tier_inserts_tier2"] += 1
        # extras = total - (tier1 count) - (tier2 count) for this row
        row_extras = len(plan["tier_inserts"]) - int(tier1_present) - int(tier2_present)
        if row_extras > 0:
            stats["tier_inserts_extra"] += row_extras
        for p in plan["promo_inserts"]:
            stats["promo_inserts_total"] += 1
            stats["promo_inserts_by_type"][p["promo_type"]] += 1
            if p["promo_name"] == PROMO_NAME_FROM_SPECIAL_PRICE:
                stats["promo_inserts_from_special_price"] += 1
            else:
                stats["promo_inserts_from_promo_col"] += 1

    # ── Print summary ──────────────────────────────────────────────────────
    mode = "COMMIT" if commit else "DRY RUN"
    print()
    print("=" * 72)
    print(f"=== {mode} — {'will write to DB' if commit else 'no writes'} ===")
    print("=" * 72)
    print(f"\nRows processed:        {stats['rows_processed']}")
    print(f"Rows flagged for review: {stats['rows_flagged']}")
    print()
    print(f"products.base_sell_price UPDATEs:  {stats['base_updates_total']}")
    print(f"  from 0.0 → real value:           {stats['base_updates_from_zero']}")
    print(f"  from existing value → diff:      {stats['base_updates_from_nonzero']}")
    print()
    print(f"product_price_tiers INSERTs:       {stats['tier_inserts']}")
    print(f"  tier1:                           {stats['tier_inserts_tier1']}")
    print(f"  tier2:                           {stats['tier_inserts_tier2']}")
    print(f"  extra_tiers_json (3+ tiers):     {stats['tier_inserts_extra']}")
    print()
    print(f"promotions INSERTs:                {stats['promo_inserts_total']}")
    for t, c in stats["promo_inserts_by_type"].items():
        print(f"  {t:12s}                     {c}")
    print(f"  (from special_price column):     {stats['promo_inserts_from_special_price']}")
    print(f"  (from โปรโมชั่น column):           {stats['promo_inserts_from_promo_col']}")

    # ── Print flagged rows ─────────────────────────────────────────────────
    if flagged_rows:
        print()
        print(f"⚠ Flagged rows ({len(flagged_rows)}) — auto-imported but review the notes:")
        for r in flagged_rows:
            print(f"  {r['sku_code']:50s} {r['normalize_notes']}")

    # ── Sample diff ────────────────────────────────────────────────────────
    if show_sample and plans:
        print()
        print(f"📄 Sample of first {min(show_sample, len(plans))} planned writes:")
        for row, plan, pid in plans[:show_sample]:
            parts = []
            if plan["update_base"]:
                parts.append(f"UPDATE base_sell_price={plan['update_base'][0]} "
                             f"(was {bsp_lookup[pid]})")
            for ql, pr, nt in plan["tier_inserts"]:
                parts.append(f"+ tier {ql!r}=฿{pr}")
            for pp in plan["promo_inserts"]:
                desc = f"+ promo {pp['promo_type']}"
                if pp["discount_value"]:
                    desc += f" val={pp['discount_value']}"
                if pp["bundle_buy"]:
                    desc += f" buy={pp['bundle_buy']}+free={pp['bundle_free']}"
                if pp["bundle_condition"]:
                    desc += f" condition={pp['bundle_condition']}"
                if pp["gift_desc"]:
                    desc += f" gift={pp['gift_desc']}"
                parts.append(desc)
            if not parts:
                parts.append("(no writes)")
            print(f"  pid={pid} {row['sku_code'][:40]:40s}")
            for p in parts:
                print(f"      {p}")

    # ── Execute (or stop) ──────────────────────────────────────────────────
    if not commit:
        print()
        print(f"DRY RUN complete. To commit, re-run with: --commit")
        conn.close()
        return stats

    # COMMIT path: single atomic transaction
    print()
    print(f"💾 Executing {stats['base_updates_total']} UPDATEs + "
          f"{stats['tier_inserts']} tier INSERTs + "
          f"{stats['promo_inserts_total']} promo INSERTs ...")
    try:
        conn.execute("BEGIN")
        for row, plan, pid in plans:
            if plan["update_base"]:
                conn.execute(
                    "UPDATE products SET base_sell_price=? WHERE id=?",
                    (plan["update_base"][0], pid))
            for ql, pr, nt in plan["tier_inserts"]:
                conn.execute(
                    "INSERT INTO product_price_tiers (product_id, qty_label, price, note) "
                    "VALUES (?, ?, ?, ?)",
                    (pid, ql, pr, nt))
            for pp in plan["promo_inserts"]:
                conn.execute(
                    "INSERT INTO promotions ("
                    "  product_id, promo_name, promo_type, discount_value,"
                    "  bundle_buy, bundle_free, bundle_unit, bundle_condition,"
                    "  bundle_tiers_json, gift_desc, gift_qty"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, pp["promo_name"], pp["promo_type"], pp["discount_value"],
                     pp["bundle_buy"], pp["bundle_free"], pp["bundle_unit"],
                     pp["bundle_condition"], pp["bundle_tiers_json"],
                     pp["gift_desc"], pp["gift_qty"]))
        conn.execute("COMMIT")
        print(f"✅ COMMIT complete.")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"❌ COMMIT FAILED — transaction rolled back. Error: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()
    return stats


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
    parser.add_argument("--commit", action="store_true",
                        help="Actually write to the DB (default is dry-run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N CSV rows (useful with --dry-run for sampling)")
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

    run_import(
        csv_path=csv_path,
        db_path=db_path,
        commit=args.commit,
        limit=args.limit,
        show_sample=args.sample,
    )


if __name__ == "__main__":
    main()
