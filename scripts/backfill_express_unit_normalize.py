"""One-time, idempotent backfill: normalize historical express_sales.unit
and recompute express_sales.brand_kind.

Why: Express sales were imported with the RAW BSN unit acronym (e.g. 'กล')
while product_code_mapping.bsn_unit is canonical (e.g. 'กล่อง'). The
unit-aware resolver (migration 063 trigger/backfill, _topup_pre_feb_for_product)
compares the two, so for split BSN codes the exact-unit override never
matched real Express data and rows fell through to the catch-all product —
wrong brand_kind / commission. import_express.py now normalizes on write
(parity with the weekly import, models.py); this script fixes the rows that
were already imported, then re-runs the 063 brand_kind recompute now that
units are comparable.

NOTE: as of migration 064 this normalization + recompute runs AUTOMATICALLY
on deploy (064 seeds bsn_unit_alias and does it in SQL). This script is now
OPTIONAL — kept as an idempotent ad-hoc re-run tool (e.g. after a manual
data poke), not a required deploy step.

    ~/.virtualenvs/erp/bin/python scripts/backfill_express_unit_normalize.py

Idempotent: normalize_unit() is a no-op on already-canonical units and the
brand_kind recompute is deterministic, so re-running changes nothing.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'inventory_app'))  # for bsn_units
import bsn_units  # noqa: E402

DB_PATH = _HERE.parent / 'inventory_app' / 'instance' / 'inventory.db'

# Exact 063 brand_kind recompute (resolver-faithful: resolve product_id from
# product_code_mapping ALONE, then look up brand; unresolved rows untouched).
_RECOMPUTE_BRAND_KIND = """
UPDATE express_sales
   SET brand_kind = (
       SELECT CASE WHEN br.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
         FROM brands br
        WHERE br.id = (
              SELECT p.brand_id FROM products p
               WHERE p.id = (
                     SELECT m.product_id
                       FROM product_code_mapping m
                      WHERE m.bsn_code = express_sales.product_code
                        AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
                        AND m.product_id IS NOT NULL
                      ORDER BY (m.bsn_unit = '')
                      LIMIT 1
               )
        )
   )
 WHERE EXISTS (
       SELECT 1
         FROM product_code_mapping m
        WHERE m.bsn_code = express_sales.product_code
          AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
          AND m.product_id IS NOT NULL
   );
"""


def normalize_and_recompute(conn):
    """Normalize express_sales.unit in place, then recompute brand_kind.
    Returns (units_changed, brand_kind_rows_touched)."""
    rows = conn.execute(
        "SELECT DISTINCT unit FROM express_sales "
        "WHERE unit IS NOT NULL AND unit <> ''"
    ).fetchall()
    units_changed = 0
    for (raw,) in rows:
        canon = bsn_units.normalize_unit(raw)
        if canon != raw:
            cur = conn.execute(
                "UPDATE express_sales SET unit = ? WHERE unit = ?",
                (canon, raw))
            units_changed += cur.rowcount
    cur = conn.execute(_RECOMPUTE_BRAND_KIND)
    bk = cur.rowcount
    conn.commit()
    return units_changed, bk


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        u, bk = normalize_and_recompute(conn)
        print(f"[backfill_express_unit_normalize] normalized {u} "
              f"express_sales rows; recomputed brand_kind on {bk} rows.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
