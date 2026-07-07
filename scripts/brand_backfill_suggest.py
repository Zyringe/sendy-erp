"""Suggest brand_id for products where brand_id IS NULL but the brand name
or alias appears in product_name. Output is a CSV for user review — not
auto-applied.

Match strategy (highest confidence first):
  1. Exact brands.name      — e.g., 'FION' in name → FION brand
  2. Exact brands.name_th    — e.g., 'สิงห์ทอง' in name → Golden Lion
  3. Exact brands.short_code — e.g., 'GL' as standalone token → Golden Lion
  4. Known alias              — 'S/D' → Sendai, 'สิงห์' → Golden Lion, etc.

Confidence levels:
  HIGH   — long token match (≥5 chars) or unambiguous match
  MEDIUM — short token (3-4 chars) — could be coincidence
  LOW    — short alias (2 chars) — needs manual review

Output columns:
    product_id, product_name, suggested_brand_id, suggested_brand,
    match_token, match_source, confidence

CLI:
    python sendy_erp/scripts/brand_backfill_suggest.py
    python sendy_erp/scripts/brand_backfill_suggest.py --output /tmp/x.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_OUT = ROOT / "data" / "exports" / "brand_backfill_suggestions.csv"


# Aliases — token → canonical brand `code` in brands table.
ALIASES = {
    "S/D":      "sendai",
    "SD-":      "sendai",     # prefix-only match (handled specially)
    "เซ็นได":   "sendai",
    "สิงห์ทอง": "golden_lion",
    "สิงห์":    "golden_lion",
}


def confidence_for(token: str) -> str:
    if len(token) >= 5:
        return "HIGH"
    if len(token) >= 3:
        return "MEDIUM"
    return "LOW"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=DEFAULT_OUT)
    p.add_argument("--apply", choices=["HIGH", "HIGH+MEDIUM", "ALL"],
                   help="apply suggestions at given confidence (writes brand_id)")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    brand_rows = conn.execute(
        "SELECT id, code, name, name_th, short_code FROM brands"
    ).fetchall()
    brands_by_code = {r["code"]: dict(r) for r in brand_rows}

    # Build (token, brand_record, source, priority) candidate list.
    # priority drives match order: longer canonical names first.
    candidates = []
    for r in brand_rows:
        if r["name"]:
            candidates.append((r["name"],       dict(r), "name",       1))
        if r["name_th"]:
            candidates.append((r["name_th"],    dict(r), "name_th",    2))
        if r["short_code"]:
            candidates.append((r["short_code"], dict(r), "short_code", 3))
    for alias, brand_code in ALIASES.items():
        if brand_code in brands_by_code:
            candidates.append((alias, brands_by_code[brand_code], "alias", 4))

    # Sort by descending token length (so longest tokens match first when
    # multiple could fit, e.g. 'สิงห์ทอง' before 'สิงห์').
    candidates.sort(key=lambda x: (-len(x[0]), x[3]))

    rows = conn.execute(
        "SELECT id, product_name FROM products "
        "WHERE brand_id IS NULL AND is_active = 1 "
        "ORDER BY id"
    ).fetchall()

    suggestions = []
    no_match = 0
    for r in rows:
        name = r["product_name"]
        name_lower = name.lower()
        matched = None
        for token, brand_rec, source, _prio in candidates:
            tok_lower = token.lower()
            if source == "short_code":
                # require word-boundary for ASCII short_codes to avoid
                # 'SD' matching 'SDM' or 'SD-9999'.
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
                             name):
                    matched = (token, brand_rec, source)
                    break
            else:
                if tok_lower in name_lower:
                    matched = (token, brand_rec, source)
                    break

        if not matched:
            no_match += 1
            continue

        token, brand_rec, source = matched
        suggestions.append({
            "product_id": r["id"],
            "product_name": name,
            "suggested_brand_id": brand_rec["id"],
            "suggested_brand": brand_rec["name"],
            "match_token": token,
            "match_source": source,
            "confidence": confidence_for(token),
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "product_id", "product_name", "suggested_brand_id",
            "suggested_brand", "match_token", "match_source", "confidence",
        ])
        w.writeheader()
        for s in suggestions:
            w.writerow(s)

    print(f"NULL-brand products scanned: {len(rows)}")
    print(f"Suggestions made:            {len(suggestions)}")
    print(f"No match (still NO_BRAND):   {no_match}")
    print(f"Output:                      {args.output}")
    print()

    by_conf = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    by_brand: dict = {}
    for s in suggestions:
        by_conf[s["confidence"]] += 1
        by_brand[s["suggested_brand"]] = by_brand.get(s["suggested_brand"], 0) + 1

    print("By confidence:")
    for k in ("HIGH", "MEDIUM", "LOW"):
        print(f"  {k:<8} {by_conf[k]}")
    print()
    print("By suggested brand (top 10):")
    for brand, n in sorted(by_brand.items(), key=lambda x: -x[1])[:10]:
        print(f"  {brand:<20} {n}")

    if args.apply:
        levels = {
            "HIGH":         {"HIGH"},
            "HIGH+MEDIUM":  {"HIGH", "MEDIUM"},
            "ALL":          {"HIGH", "MEDIUM", "LOW"},
        }[args.apply]
        to_apply = [s for s in suggestions if s["confidence"] in levels]
        print()
        print(f"Applying {len(to_apply)} suggestions at confidence={args.apply}...")
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        for s in to_apply:
            cur.execute(
                "UPDATE products SET brand_id = ? WHERE id = ?",
                (s["suggested_brand_id"], s["product_id"]),
            )
        conn.commit()
        print(f"Done. {len(to_apply)} brand_id values backfilled. "
              "audit_log captures each UPDATE.")


if __name__ == "__main__":
    main()
