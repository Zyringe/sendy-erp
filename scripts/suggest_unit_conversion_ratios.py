"""Read-only: export the stock_mapping CSV + a last column suggesting the
correct unit-conversion ratio, for Put to eyeball.

Logic (Put: ~90% are already right; price-implied is biased LOW by
bulk-buy discounts so it is a HINT, not the authority):

  0. ANCHOR: if a row's bsn_unit == the product's Sendy unit_type, that
     conversion is 1 by definition (suggest 1). The price BASE for the
     product is that same-unit row's median price; everything else is
     implied RELATIVE to it (so a unit smaller than the base, e.g. ตัว
     when unit_type=แผง, can correctly come out 0.5). Only if no
     same-unit row exists do we fall back to the cheapest priced unit.
  1. Semantic prior from the unit NAME is a strong signal:
       โหลคู่ → 24   โหล → 12   คู่/คู → 2
     (a dozen is 12 by definition regardless of a discounted โหล price).
  2. Otherwise price-implied: implied = median_price(unit) /
     median_price(base unit); then SNAP to the allowed set
     {0.5,1,2,5,6,10,12,24,25,30,50,100,1000}.
  3. Note1 override: บานพับเหล็ก KPS 2.5in + โหลคู่ = 24 ตัว.
  4. Suggestion only differs from "OK" when stored ≠ suggested AND there
     is real evidence; confidence HIGH/MED/LOW is shown so Put can triage.

NO database writes. Output: data/exports/stock_mapping_suggested_<date>.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
import sqlite3  # noqa: E402

ALLOWED = [0.5, 1, 2, 5, 6, 10, 12, 24, 25, 30, 50, 100, 1000]


def snap(x):
    """nearest allowed value in log space (ratios span 0.5 .. 1000)."""
    import math
    if x <= 0:
        return 1
    return min(ALLOWED, key=lambda v: abs(math.log(v) - math.log(x)))


def semantic(unit):
    u = (unit or "")
    if "โหลคู่" in u:
        return 24
    if u == "โหล" or u == "โหลคู":          # โหล (not โหลคู่)
        return 12
    if u in ("คู่", "คู"):
        return 2
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    a = ap.parse_args(argv)
    conn = sqlite3.connect(str(a.db))
    conn.row_factory = sqlite3.Row

    # median price per (product, unit) from both ledgers, price>0
    px = {}
    for r in conn.execute(
            "SELECT product_id pid, unit, unit_price p FROM ("
            " SELECT product_id,unit,unit_price FROM sales_transactions "
            "  WHERE unit_price>0"
            " UNION ALL SELECT product_id,unit,unit_price FROM "
            "  purchase_transactions WHERE unit_price>0)"):
        px.setdefault((r["pid"], r["unit"]), []).append(r["p"])
    med = {k: median(v) for k, v in px.items()}
    unit_type = {r["id"]: r["unit_type"] for r in conn.execute(
        "SELECT id, unit_type FROM products")}
    # BASE = the priced row whose bsn_unit == the product's Sendy unit_type
    # (that conversion is 1 by definition). Fallback: cheapest priced unit.
    anchored = {}                                  # pid -> (unit, price)
    cheapest = {}                                  # pid -> (unit, price)
    for (pid, unit), m in med.items():
        if unit == unit_type.get(pid):
            anchored[pid] = (unit, m)
        if pid not in cheapest or m < cheapest[pid][1]:
            cheapest[pid] = (unit, m)
    base_price = {pid: anchored.get(pid, cheapest.get(pid))
                  for pid in set(anchored) | set(cheapest)}

    def suggest(pid, unit, stored, pname):
        # 0. same unit as Sendy's unit_type → conversion is 1 by definition
        if unit is not None and unit == unit_type.get(pid):
            if stored is None:
                return "SET 1? [HIGH(bsn_unit == Sendy unit)]"
            return ("OK (1)" if abs(stored - 1) < 1e-9 else
                    f"SUGGEST 1  (current {stored:g}) "
                    f"[HIGH(bsn_unit == Sendy unit)]")
        sem = semantic(unit)
        samples = len(px.get((pid, unit), []))
        bp = base_price.get(pid)
        implied = (med[(pid, unit)] / bp[1]) if (bp and bp[1] and
                   (pid, unit) in med and bp[0] != unit) else None
        # Note1 explicit
        if "โหลคู่" in (unit or "") and "บานพับเหล็ก" in pname \
                and "2.5in" in pname:
            sug, conf = 24, "HIGH(Note1 KPS 2.5in)"
        elif sem is not None:
            sug = sem
            conf = ("HIGH(semantic; price≈%.0fx)" % implied
                    if implied else "HIGH(semantic)")
        elif implied is not None and samples >= 1:
            sug = snap(implied)
            conf = ("MED" if samples >= 3 else "LOW") + \
                "(price-implied %.1f, n=%d)" % (implied, samples)
        else:
            return "OK (no cross-unit price evidence)"
        if stored is None:
            return f"SET {sug}? [{conf}]"
        if abs(stored - sug) < 1e-9:
            return f"OK ({sug})"
        return f"SUGGEST {sug}  (current {stored:g}) [{conf}]"

    rows = conn.execute("""
        SELECT p.id pid,p.sku,p.sku_code,p.product_name,p.unit_type,
               COALESCE(sl.quantity,0) stock,
               m.bsn_code,m.bsn_name,u.bsn_unit,u.ratio
        FROM products p
        LEFT JOIN stock_levels sl ON sl.product_id=p.id
        LEFT JOIN product_code_mapping m
               ON m.product_id=p.id AND COALESCE(m.is_ignored,0)=0
        LEFT JOIN unit_conversions u ON u.product_id=p.id
        WHERE p.is_active=1
        ORDER BY p.id,m.bsn_code,u.bsn_unit""").fetchall()

    out = EXPORTS / ("stock_mapping_suggested_%s.csv"
                     % datetime.date.today().strftime("%Y%m%d"))
    EXPORTS.mkdir(parents=True, exist_ok=True)
    n_sug = 0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "sku", "sku_code", "product_name",
                    "unit", "number_of_stock", "bsn_code", "bsn_name",
                    "bsn_unit", "stock_conversion_ratio",
                    "ratio_suggestion"])
        for r in rows:
            if r["bsn_unit"] is None:
                sg = ""
            else:
                sg = suggest(r["pid"], r["bsn_unit"], r["ratio"],
                             r["product_name"] or "")
                if sg.startswith("SUGGEST") or sg.startswith("SET"):
                    n_sug += 1
            w.writerow([r["pid"], r["sku"], r["sku_code"],
                        r["product_name"], r["unit_type"], r["stock"],
                        r["bsn_code"] or "", r["bsn_name"] or "",
                        r["bsn_unit"] or "", r["ratio"]
                        if r["ratio"] is not None else "", sg])
    print(f"{out}  ({len(rows)} rows)")
    print(f"  rows flagged SUGGEST/SET (review): {n_sug}")
    print(f"  rows OK / no-evidence: {len(rows) - n_sug}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
