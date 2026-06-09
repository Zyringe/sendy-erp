"""Build candidate catalog SKU list from slide_top_match.csv + ERP DB.

Reads:
  Design/Catalog/2026_extract/slide_top_match.csv  (top match per slide)
  Design/Catalog/2026_extract/slide_sku_matches.csv (top-8 per slide, optional family-expand)
  sendy_erp/inventory_app/instance/inventory.db    (current product master)

Writes:
  sendy_erp/data/exports/catalog_candidates_2026.csv

Goal: produce a single review CSV the user can mark "include / skip" before catalog rename + render.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

import os

# sendy_erp root (this file lives in sendy_erp/scripts/)
SENDY_ROOT = Path(__file__).resolve().parents[1]
# Workspace root (Sendai-Boonsawat/) — assumed to be sendy_erp's parent.
# Override with SENDY_WORKSPACE_ROOT if the worktree layout differs.
WORKSPACE_ROOT = Path(os.environ.get("SENDY_WORKSPACE_ROOT",
                                     str(SENDY_ROOT.parent)))
DB = SENDY_ROOT / "inventory_app/instance/inventory.db"
TOP_MATCH = WORKSPACE_ROOT / "Design/Catalog/2026_extract/slide_top_match.csv"
ALL_MATCH = WORKSPACE_ROOT / "Design/Catalog/2026_extract/slide_sku_matches.csv"
OUT = SENDY_ROOT / "data/exports/catalog_candidates_2026.csv"


def load_top_matches():
    """slide -> {product_id, name, score, mode}"""
    rows = []
    with TOP_MATCH.open() as f:
        for r in csv.DictReader(f):
            pid = r["top_product_id"].strip()
            if not pid:
                continue
            score = int(r["top_score"] or 0)
            if score == 0:
                continue
            rows.append({
                "slide": int(r["slide"]),
                "product_id": int(pid),
                "score": score,
                "mode": r["mode"],
            })
    return rows


def load_all_matches():
    """product_id -> [slide, ...] (top-8 list, includes secondary matches)"""
    pid_to_slides = defaultdict(set)
    with ALL_MATCH.open() as f:
        for r in csv.DictReader(f):
            pid = r.get("product_id", "").strip()
            if not pid:
                continue
            pid_to_slides[int(pid)].add(int(r["slide"]))
    return pid_to_slides


def fetch_products(con, pids):
    """Pull current product master + brand + stock for these pids."""
    placeholders = ",".join("?" * len(pids))
    sql = f"""
    SELECT p.id, p.product_name, p.is_active, p.hard_to_sell,
           p.base_sell_price, p.cost_price,
           COALESCE(b.name, '') AS brand,
           COALESCE(b.is_own_brand, 0) AS is_own,
           COALESCE(s.quantity, 0) AS stock,
           p.family_id, p.color_code
    FROM products p
    LEFT JOIN brands b ON b.id = p.brand_id
    LEFT JOIN stock_levels s ON s.product_id = p.id
    WHERE p.id IN ({placeholders})
    """
    return {row["id"]: dict(row) for row in con.execute(sql, list(pids))}


def main():
    top = load_top_matches()
    all_matches = load_all_matches()
    pids = sorted({r["product_id"] for r in top})

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    products = fetch_products(con, pids)
    con.close()

    # Aggregate: pid -> { primary_slides:set, all_slides:set, best_mode, best_score }
    agg = defaultdict(lambda: {
        "primary_slides": set(),
        "best_mode": "KEYWORD",
        "best_score": 0,
    })
    for r in top:
        a = agg[r["product_id"]]
        a["primary_slides"].add(r["slide"])
        if r["mode"] == "MODEL" and a["best_mode"] != "MODEL":
            a["best_mode"] = "MODEL"
        if r["score"] > a["best_score"]:
            a["best_score"] = r["score"]

    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for pid in pids:
        p = products.get(pid)
        if not p:
            continue
        a = agg[pid]
        all_slides = sorted(all_matches.get(pid, set()) | a["primary_slides"])
        primary_slides = sorted(a["primary_slides"])

        # rec score (higher = better catalog candidate)
        rec = 0
        if a["best_mode"] == "MODEL":
            rec += 100
        if p["is_own"]:
            rec += 30
        if p["stock"] > 0:
            rec += 20
        if p["is_active"]:
            rec += 10
        if p["hard_to_sell"]:
            rec -= 15

        rows.append({
            "include": "",  # user fills Y / N / ?
            "product_id": pid,
            "product_name": p["product_name"],
            "brand": p["brand"],
            "is_own_brand": p["is_own"],
            "stock": p["stock"],
            "is_active": p["is_active"],
            "hard_to_sell": p["hard_to_sell"],
            "base_sell_price": p["base_sell_price"],
            "family_id": p["family_id"] or "",
            "match_mode": a["best_mode"],
            "match_score": a["best_score"],
            "primary_slide_count": len(primary_slides),
            "primary_slides": ",".join(map(str, primary_slides)),
            "all_slides": ",".join(map(str, all_slides)),
            "rec_score": rec,
            "notes": "",
        })

    # Sort: rec_score desc, then brand, then product_id
    rows.sort(key=lambda r: (-r["rec_score"], r["brand"], r["product_id"]))

    fieldnames = list(rows[0].keys())
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # Summary
    by_mode = defaultdict(int)
    by_brand = defaultdict(int)
    in_stock = 0
    inactive = 0
    own_brand = 0
    for r in rows:
        by_mode[r["match_mode"]] += 1
        by_brand[r["brand"] or "(no brand)"] += 1
        if r["stock"] > 0:
            in_stock += 1
        if not r["is_active"]:
            inactive += 1
        if r["is_own_brand"]:
            own_brand += 1

    print(f"Wrote {OUT.relative_to(ROOT)}: {len(rows)} candidate SKUs")
    print(f"\nMatch mode: {dict(by_mode)}")
    print(f"In-stock: {in_stock}/{len(rows)} | inactive: {inactive} | own-brand: {own_brand}")
    print(f"\nTop brands:")
    for brand, n in sorted(by_brand.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {brand or '(no brand)':<20} {n}")


if __name__ == "__main__":
    main()
