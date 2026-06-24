"""Triage the multi-unit candidate codes so Put confirms instead of
researching 300 blind.

Per bsn_code, per distinct sold unit that differs from the catch-all
product's unit_type, classify:

  RATIO   unit is a bulk/packaging unit (โหล/ลัง/กล่อง/แพ็ค/กก/ม้วน/…)
          → SAME product, just needs a unit_conversions ratio. NO override.
  NOISE   minority unit with a tiny count (<=2) vs a dominant unit
          → almost always a stray data-entry, NOT a SKU split. NO override.
  SUGGEST the catch-all product name is "<base> (<suffix>)" and a DIFFERENT
          active product exists whose de-suffixed name == <base> and whose
          unit_type == this unit → a real ตัว/แผง-style split. Pre-fill the
          override (Put just confirms / edits).
  MANUAL  multi-unit, none of the above → Put eyeballs.

Output: an enriched review CSV (one row per code+unit needing a decision).
Since mig 112 each bsn_code maps to exactly one product (per-unit override
mappings were removed), so this is detection-only — handle real splits via
unit_conversions or separate products in /mapping + /unit-conversions.

  python scripts/triage_multiunit_candidates.py            # dry, prints summary
  python scripts/triage_multiunit_candidates.py --db <db>

Read-only: never writes to the DB. CSV → data/exports/.
"""
from __future__ import annotations

import csv
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

# bulk / packaging units → same product, ratio (never a product split)
BULK = {"โหล", "ลัง", "กล่อง", "แพ็ค", "แพค", "กิโลกรัม", "กก", "กก.",
        "ม้วน", "ห่อ", "มัด", "ถุง", "กระสอบ", "แกลลอน", "ปี๊บ", "โหลล"}

SUFFIX = re.compile(r"\s*\(([^)]{1,10})\)\s*\d*\s*$")

COLS = ["bsn_code", "bsn_name", "category", "reason",
        "all_units", "per_unit_counts", "current_pid",
        "current_unit_type", "current_name",
        "override_unit", "override_product_id",
        "override_name"]


def _desuffix(name: str) -> str:
    return SUFFIX.sub("", (name or "").strip()).strip()


def classify(conn):
    conn.row_factory = sqlite3.Row
    multi = conn.execute("""
        SELECT bsn_code,
               GROUP_CONCAT(DISTINCT unit) units
          FROM (SELECT bsn_code, unit FROM sales_transactions
                 WHERE bsn_code IS NOT NULL
                UNION ALL
                SELECT bsn_code, unit FROM purchase_transactions
                 WHERE bsn_code IS NOT NULL)
         GROUP BY bsn_code
        HAVING COUNT(DISTINCT unit) > 1
         ORDER BY bsn_code
    """).fetchall()

    out, cat_n = [], Counter()
    for m in multi:
        code = m["bsn_code"]
        counts = {r["unit"]: r["n"] for r in conn.execute("""
            SELECT unit, COUNT(*) n FROM (
                SELECT unit FROM sales_transactions WHERE bsn_code=?
                UNION ALL
                SELECT unit FROM purchase_transactions WHERE bsn_code=?)
            GROUP BY unit""", (code, code))}
        units = sorted(u for u in counts if u)
        per = "; ".join(f"{u}:{counts[u]}" for u in units)
        cm = conn.execute("""
            SELECT m.product_id pid, p.product_name nm, p.unit_type ut
              FROM product_code_mapping m
              LEFT JOIN products p ON p.id=m.product_id
             WHERE m.bsn_code=? LIMIT 1
        """, (code,)).fetchone()
        cur_pid = cm["pid"] if cm else None
        cur_nm = cm["nm"] if cm else None
        cur_ut = (cm["ut"] if cm else None) or ""
        base = _desuffix(cur_nm) if cur_nm else ""
        dominant = max(counts.values()) if counts else 0

        for u in units:
            if u == cur_ut:
                continue                      # this unit already = catch-all
            base_row = {
                "bsn_code": code, "bsn_name": cur_nm or "",
                "all_units": "|".join(units), "per_unit_counts": per,
                "current_pid": cur_pid if cur_pid is not None else "",
                "current_unit_type": cur_ut, "current_name": cur_nm or "",
                "override_unit": "", "override_product_id": "",
                "override_name": "",
            }
            if u in BULK:
                cat, reason = "RATIO", f"{u} = bulk/packaging of same product"
            else:
                sib = None
                if base:
                    for s in conn.execute(
                        "SELECT id,product_name,unit_type FROM products "
                        "WHERE unit_type=? AND is_active=1 AND id<>?",
                            (u, cur_pid or -1)):
                        if _desuffix(s["product_name"]) == base:
                            sib = s
                            break
                if sib:
                    cat, reason = "SUGGEST", "same-name sibling in this unit"
                    base_row.update(
                        override_unit=u, override_product_id=sib["id"],
                        override_name=sib["product_name"])
                elif counts[u] <= 2 and dominant >= 5 * max(counts[u], 1):
                    cat, reason = "NOISE", f"{u}:{counts[u]} vs dominant {dominant}"
                else:
                    cat, reason = "MANUAL", "multi-unit, no clean sibling"
            base_row["category"] = cat
            base_row["reason"] = reason
            cat_n[cat] += 1
            out.append(base_row)
    return out, cat_n


def main(argv=None):
    db = DB_PATH
    if argv and len(argv) >= 2 and argv[0] == "--db":
        db = Path(argv[1])
    EXPORTS.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    rows, cat_n = classify(conn)
    conn.close()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = EXPORTS / f"multiunit_triaged_{ts}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    codes = len({r["bsn_code"] for r in rows})
    print(f"=== triage: {codes} codes, {len(rows)} code×unit decisions ===")
    for k in ("SUGGEST", "MANUAL", "RATIO", "NOISE"):
        print(f"  {k:8s}: {cat_n.get(k, 0)}")
    print(f"→ {out}")
    print("Detection-only since mig 112 (one product per bsn_code); handle "
          "real splits via /mapping + /unit-conversions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
