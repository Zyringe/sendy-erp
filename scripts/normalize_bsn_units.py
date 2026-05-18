"""Global BSN-unit acronym → full-Thai normalisation.

The 2026-05-18 stock_and_mapping apply only normalised the reviewed TRUE-row
subset. ~1,569 unit_conversions rows + ~13k ledger rows still hold the
acronym form (หล/อน/ผง/ตว/...). They still sync (acronym↔acronym matches),
but are not standardised. This converts the rest.

Rule: for every acronym in data/reference/bsn_unit_full.json["map"] whose
value differs from the key, rename it to the full Thai form in BOTH
  - unit_conversions.bsn_unit
  - sales_transactions.unit  +  purchase_transactions.unit
together (must stay paired or _get_base_qty stops matching).

UNIQUE(product_id, bsn_unit) collisions (product already has the full-form
row): if the two ratios are equal → drop the acronym row (dedupe). If they
differ → keep the LARGER ratio on the surviving full row, drop the acronym
row, and FLAG it. (In this data the discrepancy is always ratio 1 = a wrong
identity placeholder vs the real pack multiplier, so max is correct; Put
confirmed pid 1704→12 and pid 906→1000 on 2026-05-18.) Ledger rename is
deterministic and global.

Does NOT touch the `transactions` ledger or `stock_levels` (already-synced
stock is unaffected — this only standardises text + future-sync keys).

Dry-run by default. --apply commits. Back up with a UNIQUE filename first.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
MAP_JSON = ROOT / "data" / "reference" / "bsn_unit_full.json"

sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

LEDGER = ("sales_transactions", "purchase_transactions")


def load_map(path):
    m = json.load(open(path, encoding="utf-8"))["map"]
    return {k: v for k, v in m.items() if k != v}      # acronyms to change


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--map", type=Path, default=MAP_JSON)
    p.add_argument("--apply", action="store_true",
                   help="commit (default dry-run)")
    args = p.parse_args(argv)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    M = load_map(args.map)

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    uc = conn.execute(
        "SELECT id, product_id, bsn_unit, ratio FROM unit_conversions "
        "WHERE bsn_unit IN (%s)" % ",".join("?" * len(M)),
        tuple(M)).fetchall()

    rename, dedupe, conflict = [], [], []
    for r in uc:
        tgt = M[r["bsn_unit"]]
        ex = conn.execute(
            "SELECT id, ratio FROM unit_conversions "
            "WHERE product_id=? AND bsn_unit=? AND id<>?",
            (r["product_id"], tgt, r["id"])).fetchone()
        if ex is None:
            rename.append((r["id"], r["product_id"], r["bsn_unit"], tgt))
        elif abs((ex["ratio"] or 0) - (r["ratio"] or 0)) < 1e-9:
            dedupe.append((r["id"], r["product_id"], r["bsn_unit"], tgt))
        else:
            keep = max(r["ratio"] or 0, ex["ratio"] or 0)
            conflict.append((r["product_id"], r["bsn_unit"], r["ratio"],
                             tgt, ex["ratio"], ex["id"], keep))
            dedupe.append((r["id"], r["product_id"], r["bsn_unit"], tgt))

    led_counts = {}
    for t in LEDGER:
        led_counts[t] = {
            a: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE unit=?",
                            (a,)).fetchone()[0] for a in M}

    print(f"=== normalize_bsn_units | {len(M)} acronyms ===")
    print(f"  unit_conversions: rename {len(rename)}, "
          f"dedupe {len(dedupe)} (incl {len(conflict)} ratio-CONFLICT)")
    for t in LEDGER:
        tot = sum(led_counts[t].values())
        print(f"  {t}.unit rows to rewrite: {tot}")
    if conflict:
        print("  ⚠ ratio conflicts (keep MAX ratio on full row — flagged):")
        for pid, a, ra, tg, rf, _fid, keep in conflict[:10]:
            print(f"    pid {pid} {a}(r={ra}) vs {tg}(r={rf}) → keep {keep}")

    with open(EXPORTS / "normalize_bsn_units_plan.csv", "w",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["action", "uc_id", "product_id", "from", "to"])
        for a, pid, fr, to in rename:
            w.writerow(["rename", a, pid, fr, to])
        for a, pid, fr, to in dedupe:
            w.writerow(["dedupe", a, pid, fr, to])
    with open(EXPORTS / f"normalize_bsn_units_conflicts_{ts}.csv", "w",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "acronym", "ratio_acronym",
                    "full", "ratio_full_orig", "full_uc_id",
                    "ratio_kept_max"])
        w.writerows(conflict)

    if not args.apply:
        print("\nDRY-RUN. Unique backup then --apply. Plan: "
              f"{EXPORTS}/normalize_bsn_units_plan.csv")
        conn.close()
        return 0

    # ---- APPLY ----
    # bump the surviving full row to the larger ratio before dropping acronym
    for pid, a, ra, tg, rf, fid, keep in conflict:
        conn.execute("UPDATE unit_conversions SET ratio=? WHERE id=?",
                      (keep, fid))
    for _id, _pid, _fr, _to in dedupe:
        conn.execute("DELETE FROM unit_conversions WHERE id=?", (_id,))
    for _id, _pid, _fr, _to in rename:
        conn.execute("UPDATE unit_conversions SET bsn_unit=? WHERE id=?",
                      (_to, _id))
    for t in LEDGER:
        for a, full in M.items():
            conn.execute(f"UPDATE {t} SET unit=? WHERE unit=?", (full, a))
    conn.commit()

    bad_uc = conn.execute(
        "SELECT COUNT(*) FROM unit_conversions WHERE bsn_unit IN (%s)"
        % ",".join("?" * len(M)), tuple(M)).fetchone()[0]
    bad_led = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE unit IN (%s)"
                               % ",".join("?" * len(M)),
                               tuple(M)).fetchone()[0] for t in LEDGER)
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT product_id,bsn_unit FROM "
        "unit_conversions GROUP BY product_id,bsn_unit HAVING COUNT(*)>1)"
    ).fetchone()[0]
    print(f"\nAPPLIED. uc renamed {len(rename)}, deduped {len(dedupe)}, "
          f"ledger rewritten.")
    print(f"  acronym left: unit_conversions={bad_uc} ledger={bad_led} "
          f"(want 0/0)")
    print(f"  unit_conversions UNIQUE dups: {dup} (want 0)")
    print(f"  ratio conflicts flagged: {len(conflict)} "
          f"→ normalize_bsn_units_conflicts_{ts}.csv")
    conn.close()
    return 0 if (bad_uc == 0 and bad_led == 0 and dup == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
