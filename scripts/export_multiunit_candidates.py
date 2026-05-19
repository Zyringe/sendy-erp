"""Read-only: list every BSN code whose history spans >1 distinct sold
unit, so Put can mark which ones are a real ตัว/แผง-style SKU split that
needs a per-unit mapping override (mig 061).

Most multi-unit codes are a single product (the unit just needs a ratio in
unit_conversions) — only a minority are deliberate SKU splits. Fill the two
blank columns (override_unit, override_product_id) ONLY for the real splits,
then feed the reviewed CSV to scripts/apply_unit_aware_remap.py.

  python scripts/export_multiunit_candidates.py

No --apply: this script never writes to the DB. CSV → data/exports/.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

COLS = ["bsn_code", "bsn_name", "distinct_units", "per_unit_counts",
        "current_mapped_pid", "current_mapped_name", "current_unit_type",
        "sibling_skus_by_unit", "override_unit", "override_product_id"]


def build_rows(conn):
    conn.row_factory = sqlite3.Row
    multi = conn.execute("""
        SELECT bsn_code,
               COUNT(DISTINCT unit) du,
               GROUP_CONCAT(DISTINCT unit) units
          FROM (SELECT bsn_code, unit FROM sales_transactions
                 WHERE bsn_code IS NOT NULL
                UNION ALL
                SELECT bsn_code, unit FROM purchase_transactions
                 WHERE bsn_code IS NOT NULL)
         GROUP BY bsn_code
        HAVING du > 1
         ORDER BY bsn_code
    """).fetchall()

    rows = []
    for m in multi:
        code = m["bsn_code"]
        units = sorted({u for u in (m["units"] or "").split(",") if u})
        counts = conn.execute("""
            SELECT unit, COUNT(*) n FROM (
                SELECT unit FROM sales_transactions WHERE bsn_code=?
                UNION ALL
                SELECT unit FROM purchase_transactions WHERE bsn_code=?)
            GROUP BY unit ORDER BY unit
        """, (code, code)).fetchall()
        per_unit = "; ".join(f"{r['unit']}:{r['n']}" for r in counts)

        # current catch-all mapping (mig 061: bsn_unit='')
        cm = conn.execute("""
            SELECT m.product_id, p.product_name, p.unit_type
              FROM product_code_mapping m
              LEFT JOIN products p ON p.id = m.product_id
             WHERE m.bsn_code=? ORDER BY (m.bsn_unit='') DESC LIMIT 1
        """, (code,)).fetchone()
        cur_pid = cm["product_id"] if cm else None
        cur_name = cm["product_name"] if cm else None
        cur_ut = cm["unit_type"] if cm else None

        # for each distinct unit, list active SKUs whose unit_type == unit
        sib = []
        for u in units:
            cands = conn.execute(
                "SELECT id, sku, product_name FROM products "
                "WHERE unit_type=? AND is_active=1 ORDER BY sku LIMIT 6",
                (u,)).fetchall()
            if cands:
                sib.append(f"[{u}] " + " | ".join(
                    f"pid{c['id']}/sku{c['sku']}:{c['product_name']}"
                    for c in cands))
        rows.append({
            "bsn_code": code,
            "bsn_name": (cm and cur_name) or "",
            "distinct_units": "|".join(units),
            "per_unit_counts": per_unit,
            "current_mapped_pid": cur_pid if cur_pid is not None else "",
            "current_mapped_name": cur_name or "",
            "current_unit_type": cur_ut or "",
            "sibling_skus_by_unit": "  ||  ".join(sib),
            "override_unit": "",
            "override_product_id": "",
        })
    return rows


def main(argv=None):
    db = DB_PATH
    if argv and len(argv) >= 2 and argv[0] == "--db":
        db = Path(argv[1])
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(str(db))
    rows = build_rows(conn)
    conn.close()
    out = EXPORTS / f"multiunit_candidates_{ts}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"multi-unit candidate codes: {len(rows)}")
    print(f"→ {out}")
    print("Fill override_unit + override_product_id only for real "
          "ตัว/แผง-style SKU splits, then run apply_unit_aware_remap.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
