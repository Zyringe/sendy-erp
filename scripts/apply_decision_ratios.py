"""DEPRECATED: one-off from 2026-05-19. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Bucket B — apply Put's reviewed `decision` ratios to unit_conversions.

Reads the reviewed stock_mapping_suggested CSV. For every row whose
`decision` is numeric AND the remap column is blank or '-', upsert
  unit_conversions(product_id, bsn_unit, ratio = decision)
(UPDATE if the (product_id,bsn_unit) row exists, else INSERT).

Rows handled by the other buckets are skipped here:
  - decision blank/'-' with a remap target  → apply_decision_remaps.py
  - free-text decision (unit_type change)    → apply_unit_type_change.py

Touches ONLY unit_conversions. Dry-run by default. --apply commits.
Unique backup first.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
_INPUT_DIR = Path(os.environ.get("SENDY_INPUT_DIR", os.path.expanduser("~/Downloads")))
DEFAULT_CSV = _INPUT_DIR / ("stock_mapping_suggested_20260518"
                            " - stock_mapping_suggested_20260518.csv")
import sqlite3  # noqa: E402

DEC = "decision"
REMAP = ("change mapping of particular bsn_name and bsn_unit to following "
         "product (with conversion = 1)")


def _num(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if not a.csv_path.exists():
        print(f"CSV not found: {a.csv_path}", file=sys.stderr)
        return 2
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(str(a.db))
    conn.row_factory = sqlite3.Row

    plan = []           # (pid, bsn_unit, old_ratio_or_None, new_ratio)
    skipped_no_pid = []
    with open(a.csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get(DEC) or "").strip()
            rm = (r.get(REMAP) or "").strip()
            unit = (r.get("bsn_unit") or "").strip()
            nv = _num(d)
            if nv is None or rm not in ("", "-") or not unit:
                continue
            try:
                pid = int(r["product_id"])
            except (TypeError, ValueError):
                continue
            ex = conn.execute("SELECT ratio FROM unit_conversions WHERE "
                              "product_id=? AND bsn_unit=?",
                              (pid, unit)).fetchone()
            if conn.execute("SELECT 1 FROM products WHERE id=?",
                            (pid,)).fetchone() is None:
                skipped_no_pid.append((pid, unit))
                continue
            plan.append((pid, unit, ex["ratio"] if ex else None, nv))

    changes = [p for p in plan if p[2] is None or abs(p[2] - p[3]) > 1e-9]
    ins = [p for p in changes if p[2] is None]
    upd = [p for p in changes if p[2] is not None]
    print(f"=== Bucket B set-ratio | {len(plan)} decision rows ===")
    print(f"  already-correct (noop): {len(plan) - len(changes)}")
    print(f"  UPDATE ratio: {len(upd)}   INSERT new conv: {len(ins)}")
    if skipped_no_pid:
        print(f"  skipped (pid missing): {len(skipped_no_pid)}")
    arch = EXPORTS / f"apply_decision_ratios_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "bsn_unit", "old_ratio", "new_ratio",
                    "action"])
        for pid, u, o, nv in changes:
            w.writerow([pid, u, "" if o is None else o, nv,
                        "INSERT" if o is None else "UPDATE"])
    for pid, u, o, nv in changes[:15]:
        print(f"    pid {pid:5} {u:6} {('—' if o is None else o)} → {nv}")

    if not a.apply:
        print(f"\nDRY-RUN. Unique backup then --apply. → {arch.name}")
        conn.close()
        return 0

    conn.execute("BEGIN")
    try:
        for pid, u, o, nv in changes:
            if o is None:
                conn.execute("INSERT INTO unit_conversions (product_id,"
                             "bsn_unit,ratio) VALUES (?,?,?)", (pid, u, nv))
            else:
                conn.execute("UPDATE unit_conversions SET ratio=? WHERE "
                             "product_id=? AND bsn_unit=?", (nv, pid, u))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1

    bad = 0
    for pid, u, o, nv in changes:
        cur = conn.execute("SELECT ratio FROM unit_conversions WHERE "
                           "product_id=? AND bsn_unit=?",
                           (pid, u)).fetchone()
        if cur is None or abs(cur[0] - nv) > 1e-9:
            bad += 1
    print(f"\nAPPLIED. {len(upd)} updated, {len(ins)} inserted. "
          f"mismatches: {bad} (want 0)")
    conn.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
