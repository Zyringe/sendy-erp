"""Read-only: list every BSN code whose history spans >1 distinct sold
unit, so Put can mark which ones are a real ตัว/แผง-style SKU split that
needs a per-unit mapping override (mig 061).

Most multi-unit codes are a single product (the unit just needs a ratio in
unit_conversions) — only a minority are deliberate SKU splits. Since mig 112
each bsn_code maps to exactly one product (per-unit override mappings were
removed); this report is now detection-only — handle real ตัว/แผง splits via
unit_conversions or separate products in /mapping + /unit-conversions.

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

        # current mapping (mig 112: one row per bsn_code)
        cm = conn.execute("""
            SELECT m.product_id, p.product_name, p.unit_type
              FROM product_code_mapping m
              LEFT JOIN products p ON p.id = m.product_id
             WHERE m.bsn_code=? LIMIT 1
        """, (code,)).fetchone()
        cur_pid = cm["product_id"] if cm else None
        cur_name = cm["product_name"] if cm else None
        cur_ut = cm["unit_type"] if cm else None

        # for each distinct unit, list active SKUs whose unit_type == unit
        sib = []
        for u in units:
            cands = conn.execute(
                "SELECT id, product_name FROM products "
                "WHERE unit_type=? AND is_active=1 ORDER BY id LIMIT 6",
                (u,)).fetchall()
            if cands:
                sib.append(f"[{u}] " + " | ".join(
                    f"pid{c['id']}:{c['product_name']}"
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
    print("Detection-only since mig 112 (one product per bsn_code); handle "
          "real ตัว/แผง splits via /mapping + /unit-conversions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
