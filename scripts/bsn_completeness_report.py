"""BSN mapping/unit completeness — read-only consolidated report.

"Consolidates" product_code_mapping + unit_conversions + BSN ledger + stock
into ONE row per product (the safe way to join the two tables — no schema
merge, which would denormalise ratio and break sync). Each product gets a
`status`:

  mapped_with_ledger  has active mapping + has BSN bills            (healthy)
  mapped_no_ledger    mapped but no BSN bill yet                    (new/spec)
  conv_orphan         unit_conversions but no active mapping        (review)
  no_bsn_has_history  no BSN code but has stock ledger              (non-BSN)
  no_bsn_no_history   no BSN code, no stock history                 (idle)

Plus `sync_gap=Y` iff an UNSYNCED BSN bill exists whose unit ≠ base unit and
has no unit_conversions row → the only state that genuinely blocks sync.

Read-only: never writes to the DB. Outputs a CSV to data/exports/.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
import sqlite3  # noqa: E402

SQL = """
WITH map AS (SELECT product_id,
        COUNT(*) n, GROUP_CONCAT(bsn_code,' | ') codes
     FROM product_code_mapping WHERE COALESCE(is_ignored,0)=0
     GROUP BY product_id),
 conv AS (SELECT product_id, COUNT(*) n,
        GROUP_CONCAT(bsn_unit||':'||ratio,' | ') uc
     FROM unit_conversions GROUP BY product_id),
 sled AS (SELECT product_id, COUNT(*) n,
        SUM(CASE WHEN COALESCE(synced_to_stock,0)=0 THEN 1 ELSE 0 END) uns
     FROM sales_transactions GROUP BY product_id),
 pled AS (SELECT product_id, COUNT(*) n,
        SUM(CASE WHEN COALESCE(synced_to_stock,0)=0 THEN 1 ELSE 0 END) uns
     FROM purchase_transactions GROUP BY product_id),
 hist AS (SELECT product_id, COUNT(*) n FROM transactions
     WHERE product_id IS NOT NULL GROUP BY product_id),
 ecom AS (SELECT DISTINCT product_id FROM ecommerce_listings),
 gap AS (
   SELECT DISTINCT product_id FROM sales_transactions s
   WHERE COALESCE(s.synced_to_stock,0)=0
     AND s.unit <> (SELECT unit_type FROM products WHERE id=s.product_id)
     AND NOT EXISTS (SELECT 1 FROM unit_conversions u
        WHERE u.product_id=s.product_id AND u.bsn_unit=s.unit)
   UNION
   SELECT DISTINCT product_id FROM purchase_transactions x
   WHERE COALESCE(x.synced_to_stock,0)=0
     AND x.unit <> (SELECT unit_type FROM products WHERE id=x.product_id)
     AND NOT EXISTS (SELECT 1 FROM unit_conversions u
        WHERE u.product_id=x.product_id AND u.bsn_unit=x.unit))
SELECT p.id product_id, p.sku_code, p.product_name,
   p.unit_type base_unit, p.is_active,
   COALESCE(map.n,0) n_bsn_codes, COALESCE(map.codes,'') bsn_codes,
   COALESCE(conv.n,0) n_unit_conv, COALESCE(conv.uc,'') unit_conversions,
   COALESCE(sled.n,0)+COALESCE(pled.n,0) n_bsn_ledger,
   COALESCE(sled.uns,0)+COALESCE(pled.uns,0) n_unsynced,
   COALESCE(hist.n,0) n_stock_history,
   COALESCE(sl.quantity,0) stock_now,
   CASE WHEN ecom.product_id IS NOT NULL THEN 'Y' ELSE '' END on_ecommerce,
   CASE WHEN gap.product_id IS NOT NULL THEN 'Y' ELSE '' END sync_gap,
   CASE
     WHEN map.product_id IS NOT NULL
          AND COALESCE(sled.n,0)+COALESCE(pled.n,0)>0
       THEN 'mapped_with_ledger'
     WHEN map.product_id IS NOT NULL THEN 'mapped_no_ledger'
     WHEN conv.product_id IS NOT NULL THEN 'conv_orphan'
     WHEN COALESCE(hist.n,0)>0 THEN 'no_bsn_has_history'
     ELSE 'no_bsn_no_history'
   END status
FROM products p
LEFT JOIN map  ON map.product_id=p.id
LEFT JOIN conv ON conv.product_id=p.id
LEFT JOIN sled ON sled.product_id=p.id
LEFT JOIN pled ON pled.product_id=p.id
LEFT JOIN hist ON hist.product_id=p.id
LEFT JOIN ecom ON ecom.product_id=p.id
LEFT JOIN gap  ON gap.product_id=p.id
LEFT JOIN stock_levels sl ON sl.product_id=p.id
ORDER BY p.id
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path,
                    default=EXPORTS / "bsn_completeness_report.csv")
    args = ap.parse_args(argv)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL).fetchall()
    cols = rows[0].keys() if rows else []
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
    from collections import Counter
    st = Counter(r["status"] for r in rows)
    gap = sum(1 for r in rows if r["sync_gap"] == "Y")
    print(f"=== BSN completeness — {len(rows)} products → {args.out.name} ===")
    for k, v in st.most_common():
        print(f"  {k:22} {v}")
    print(f"  *** sync_gap=Y (genuinely blocked): {gap} ***")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
