"""Re-point a BSN code (and all its historical ledger rows) to another
product — WITHOUT merging the two products.

  python scripts/remap_bsn_code.py --code 001ก2600 --to 17 [--apply]
  python scripts/remap_bsn_code.py --code 030บ4000 --to 96 --bsn-unit แผง [--apply]

Used when a bsn_code maps to the wrong product of a deliberately-split
pair (e.g. Put keeps ตัว/แผง as separate SKUs but BSN sells the แผง one):
re-point the mapping + move every sales_transactions / purchase_transactions
row of that code onto the correct product, recalc stock for BOTH the new and
the previously-attributed products. Both products stay active (the split is
preserved — this is NOT merge_product).

--bsn-unit (optional): mig 124 restored per-unit split mapping
(product_code_mapping.bsn_unit) — a code CAN map to different products per
BSN unit again (e.g. แผง vs ตัว). Omit --bsn-unit to re-point the WHOLE code
(every mapping row + every source row, any unit) — the common case. Pass
--bsn-unit to re-point only that unit-scoped slice of a split code, leaving
the sibling unit's row/product untouched.

All the actual mutation (mapping + source rows + ledger rebuild + WACC) is
done by models.repoint_bsn_code() — see its docstring for the Reconciliation
Procedure this implements and why. ROOT-CAUSE FIX (2026-07-03): this script
used to only move the source-table rows and recalc stock_levels via a raw
SUM(quantity_change); it never touched the `transactions` ledger rows tagged
'BSN%', so those stayed STRANDED on the OLD product and its final check only
looked at the source tables — it reported "OK" while 398 orphan ledger rows
sat on 141 products. See decisions/log.md 2026-07-02/07-03.

Dry-run by default (read-only preview + a CSV export of what WOULD move).
--apply commits.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", required=True)
    ap.add_argument("--to", dest="dst", type=int, required=True)
    ap.add_argument("--bsn-unit", dest="bsn_unit", default=None,
                     help="Re-point only this unit-scoped slice of a split "
                          "code (mig 124). Omit to re-point the whole code.")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(str(a.db))
    conn.row_factory = sqlite3.Row

    if not conn.execute("SELECT 1 FROM products WHERE id=?",
                        (a.dst,)).fetchone():
        print(f"product {a.dst} not found", file=sys.stderr)
        return 2

    import models  # noqa: E402

    norm_unit = models.bsn_units.normalize_unit(a.bsn_unit) if a.bsn_unit else None

    if norm_unit is not None:
        mapping_rows = conn.execute(
            "SELECT bsn_unit, product_id FROM product_code_mapping "
            "WHERE bsn_code=? AND bsn_unit=?", (a.code, norm_unit)).fetchall()
    else:
        mapping_rows = conn.execute(
            "SELECT bsn_unit, product_id FROM product_code_mapping "
            "WHERE bsn_code=?", (a.code,)).fetchall()

    def _ledger_pids(table):
        rows = conn.execute(
            f"SELECT product_id, unit FROM {table} WHERE bsn_code=?", (a.code,)
        ).fetchall()
        if norm_unit is not None:
            rows = [r for r in rows
                    if (models.bsn_units.normalize_unit(r["unit"]) or "") == norm_unit]
        return rows

    ledger_rows = _ledger_pids("sales_transactions") + _ledger_pids("purchase_transactions")
    by_pid = {}
    for r in ledger_rows:
        by_pid[r["product_id"]] = by_pid.get(r["product_id"], 0) + 1

    print(f"=== remap bsn_code {a.code!r}"
          f"{f' (bsn_unit={norm_unit!r})' if norm_unit else ''} → product {a.dst} ===")
    print(f"  mapping rows currently: "
          f"{[(r['bsn_unit'], r['product_id']) for r in mapping_rows] or 'NONE'}")
    print(f"  source rows by product: {sorted(by_pid.items())}")

    arch = EXPORTS / f"remap_{a.code}_to_{a.dst}_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bsn_code", "bsn_unit", "old_products", "new_product", "source_rows_to_move"])
        moved = sum(n for pid, n in by_pid.items() if pid != a.dst)
        old_pids = sorted({pid for pid in by_pid if pid != a.dst}
                           | {r["product_id"] for r in mapping_rows if r["product_id"] != a.dst})
        w.writerow([a.code, norm_unit or "", old_pids, a.dst, moved])

    if not a.apply:
        print(f"\nDRY-RUN. Re-run with --apply to commit. Preview → {arch.name}")
        conn.close()
        return 0

    try:
        report = models.repoint_bsn_code(conn, a.code, a.dst, bsn_unit=norm_unit)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1

    print(f"\nAPPLIED. affected products: {report['affected_pids']}")
    print(f"  rows moved: sales={report['rows_moved']['sales']} "
          f"purchase={report['rows_moved']['purchase']}")
    print(f"  stock before: {report['stock_before']}")
    print(f"  stock after:  {report['stock_after']}")

    # Verification — checks BOTH surfaces, not just the source table (the old
    # bug's final check only looked at sales_transactions/purchase_transactions
    # and never noticed the `transactions` ledger itself was still stranded).
    if norm_unit is not None:
        rows = conn.execute(
            "SELECT product_id, unit FROM sales_transactions WHERE bsn_code=? "
            "UNION ALL SELECT product_id, unit FROM purchase_transactions "
            "WHERE bsn_code=?", (a.code, a.code)).fetchall()
        bad = sum(1 for r in rows
                  if (models.bsn_units.normalize_unit(r["unit"]) or "") == norm_unit
                  and r["product_id"] != a.dst)
    else:
        bad = conn.execute(
            "SELECT COUNT(*) FROM (SELECT product_id FROM sales_transactions "
            "WHERE bsn_code=? UNION ALL SELECT product_id FROM purchase_transactions "
            "WHERE bsn_code=?) WHERE product_id<>?",
            (a.code, a.code, a.dst)).fetchone()[0]
    orphans = report["orphan_rows_after"]
    print(f"\n  source rows not on {a.dst}: {bad} (want 0)")
    print(f"  stray `transactions` ledger orphans for {a.code!r}: {orphans} (want 0)")
    conn.close()
    return 0 if (bad == 0 and orphans == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
