"""Re-point a BSN code (and all its historical ledger rows) to another
product — WITHOUT merging the two products.

  python scripts/remap_bsn_code.py --code 001ก2600 --to 17 [--apply]

Used when a bsn_code maps to the wrong product of a deliberately-split
pair (e.g. Put keeps ตัว/แผง as separate SKUs but BSN sells the แผง one):
re-point the mapping + move every sales_transactions / purchase_transactions
row of that code onto the correct product, recalc stock for BOTH the new and
the previously-attributed products. Both products stay active (the split is
preserved — this is NOT merge_product).

unit_conversions are left as-is (they are product+unit scoped; the orphan
already has the right ones). Dry-run by default. --apply commits. Unique
backup first.
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

LEDGER = ("sales_transactions", "purchase_transactions")


def recalc(conn, pid):
    conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
    conn.execute("INSERT INTO stock_levels (product_id,quantity) "
                 "SELECT ?, COALESCE(SUM(quantity_change),0) FROM "
                 "transactions WHERE product_id=?", (pid, pid))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", required=True)
    ap.add_argument("--to", dest="dst", type=int, required=True)
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

    cur_map = conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=?",
        (a.code,)).fetchone()
    affected = {a.dst}
    if cur_map:
        affected.add(cur_map["product_id"])
    rows = conn.execute(
        "SELECT product_id pid, COUNT(*) n FROM (SELECT product_id FROM "
        "sales_transactions WHERE bsn_code=? UNION ALL SELECT product_id "
        "FROM purchase_transactions WHERE bsn_code=?) GROUP BY product_id",
        (a.code, a.code)).fetchall()
    for r in rows:
        if r["pid"] is not None:
            affected.add(r["pid"])

    print(f"=== remap bsn_code {a.code} → product {a.dst} ===")
    print(f"  mapping currently → "
          f"{cur_map['product_id'] if cur_map else 'NONE'}")
    print(f"  ledger rows by product: {[(r['pid'], r['n']) for r in rows]}")
    print(f"  stock recalc for: {sorted(affected)}")

    arch = EXPORTS / f"remap_{a.code}_to_{a.dst}_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bsn_code", "old_product", "new_product",
                    "ledger_rows_moved"])
        moved = sum(r["n"] for r in rows if r["pid"] != a.dst)
        w.writerow([a.code, cur_map["product_id"] if cur_map else "",
                    a.dst, moved])

    if not a.apply:
        print(f"\nDRY-RUN. Unique backup then --apply. → {arch.name}")
        conn.close()
        return 0

    conn.execute("BEGIN")
    try:
        if cur_map:
            conn.execute("UPDATE product_code_mapping SET product_id=?, "
                         "is_ignored=0, ignore_reason=NULL WHERE bsn_code=?",
                         (a.dst, a.code))
        else:
            conn.execute("INSERT INTO product_code_mapping (bsn_code,"
                         "bsn_name,product_id,is_ignored) VALUES (?,?,?,0)",
                         (a.code, a.code, a.dst))
        for t in LEDGER:
            conn.execute(f"UPDATE {t} SET product_id=? WHERE bsn_code=?",
                         (a.dst, a.code))
        for pid in affected:
            recalc(conn, pid)
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1

    import models  # noqa: E402
    try:
        for pid in affected:
            models.recalculate_product_wacc(pid, conn)
        conn.commit()
    except Exception as e:
        print(f"  [warn] WACC: {e}")

    bad = conn.execute(
        "SELECT COUNT(*) FROM (SELECT product_id FROM sales_transactions "
        "WHERE bsn_code=? UNION SELECT product_id FROM purchase_transactions "
        "WHERE bsn_code=?) WHERE product_id<>?",
        (a.code, a.code, a.dst)).fetchone()[0]
    m2 = conn.execute("SELECT product_id FROM product_code_mapping WHERE "
                      "bsn_code=?", (a.code,)).fetchone()[0]
    print(f"\nAPPLIED. mapping {a.code} → {m2} "
          f"({'OK' if m2 == a.dst else 'BAD'}); "
          f"stray ledger rows not on {a.dst}: {bad} (want 0)")
    conn.close()
    return 0 if (bad == 0 and m2 == a.dst) else 1


if __name__ == "__main__":
    raise SystemExit(main())
