"""Delete orphan unit_conversions rows.

Orphan = a unit_conversions row whose product has NO active (non-ignored)
product_code_mapping AND whose (product_id, bsn_unit) is NOT referenced by
any sales_transactions / purchase_transactions row.

Such rows are inert seed/default leftovers (mostly ratio 1, bsn_unit == base)
created by old smart-mapping / migrations before any BSN code was bound, or
left behind after a merge/ignore. They are unused — deleting them does not
affect stock, sync, or any ledger.

Rows that ARE ledger-referenced (the BSN code was later removed/ignored but
historical bills still use that product+unit) are KEPT — deleting them would
break a re-sync.

Does NOT touch transactions / stock_levels / product_code_mapping /
products. Dry-run by default. --apply commits. Unique backup first.
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

ORPHAN_SQL = """
SELECT u.id, u.product_id, u.bsn_unit, u.ratio
FROM unit_conversions u
WHERE u.product_id NOT IN (
        SELECT product_id FROM product_code_mapping
        WHERE COALESCE(is_ignored,0)=0
          AND product_id IS NOT NULL)   -- NULL in NOT IN → matches nothing
                                        -- (unmapped codes have NULL pid)
  AND NOT EXISTS (SELECT 1 FROM sales_transactions s
        WHERE s.product_id=u.product_id AND s.unit=u.bsn_unit)
  AND NOT EXISTS (SELECT 1 FROM purchase_transactions x
        WHERE x.product_id=u.product_id AND x.unit=u.bsn_unit)
ORDER BY u.product_id, u.bsn_unit
"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--apply", action="store_true",
                   help="commit (default dry-run)")
    args = p.parse_args(argv)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(ORPHAN_SQL).fetchall()

    arch = EXPORTS / f"removed_orphan_unit_conversions_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["uc_id", "product_id", "bsn_unit", "ratio",
                    "product_name"])
        for r in rows:
            nm = conn.execute("SELECT product_name FROM products WHERE id=?",
                              (r["product_id"],)).fetchone()
            w.writerow([r["id"], r["product_id"], r["bsn_unit"], r["ratio"],
                        nm[0] if nm else ""])

    print(f"=== orphan unit_conversions (no mapping + no ledger ref) ===")
    print(f"  rows to delete: {len(rows)}  → {arch.name}")
    for r in rows[:20]:
        print(f"    uc {r['id']:5} pid {r['product_id']:5} "
              f"{r['bsn_unit']} r={r['ratio']}")

    if not args.apply:
        print("\nDRY-RUN. Unique backup then --apply.")
        conn.close()
        return 0

    sl0 = conn.execute("SELECT COUNT(*),COALESCE(SUM(quantity),0) "
                        "FROM stock_levels").fetchone()
    tx0 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.executemany("DELETE FROM unit_conversions WHERE id=?",
                     [(r["id"],) for r in rows])
    conn.commit()
    sl1 = conn.execute("SELECT COUNT(*),COALESCE(SUM(quantity),0) "
                        "FROM stock_levels").fetchone()
    tx1 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    left = conn.execute(ORPHAN_SQL).fetchall()
    print(f"\nAPPLIED. deleted {len(rows)} orphan unit_conversions.")
    print(f"  orphans left: {len(left)} (want 0)")
    print(f"  stock_levels unchanged: "
          f"{'OK' if (sl0[0],sl0[1])==(sl1[0],sl1[1]) else 'CHANGED!'}; "
          f"transactions unchanged: {'OK' if tx0==tx1 else 'CHANGED!'}")
    conn.close()
    return 0 if not left and (sl0[0], sl0[1]) == (sl1[0], sl1[1]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
