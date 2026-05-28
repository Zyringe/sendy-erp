#!/usr/bin/env python3
"""Generate a review CSV proposing sub_category_short_code for products
that have a category but no subcat.

Approach: for each missing-subcat product, find sibling products in the
same category that DO have a subcat. Score each sibling by Thai-token
Jaccard similarity to the missing product's name. Propose the highest-
scoring sibling's subcat. Confidence tier from the top score:
  high   ≥ 0.55
  medium 0.35–0.54
  low    0.20–0.34
  unmatched < 0.20 OR category has zero subcat coverage

Output mirrors normalize_round1: Excel-reviewable CSV at
Operations/05_analysis-reports/subcat_round1_<date>.csv. User marks
approve="Y", then apply_subcat_coverage.py UPDATEs the DB.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "inventory_app" / "instance" / "inventory.db"
DEFAULT_OUT = REPO.parent / "Operations" / "05_analysis-reports" / \
              f"subcat_round1_{date.today().isoformat()}.csv"


# Stop tokens — common across categories, don't help disambiguate subcat.
# Includes brand short_codes, generic descriptors, units, color codes.
STOP_TOKENS = {
    # Brand english + Thai
    "sendai", "goldenlion", "golden", "lion", "a-spec", "aspec",
    "เซ็นได", "สิงห์ทอง", "s/d", "sd", "gl", "as",
    # Generic descriptors
    "สี", "ชนิด", "แบบ", "พร้อม", "ของ",
    # Color codes
    "ac", "gp", "nk", "ss", "bz", "ab", "cr", "pb", "sn", "slv", "gld",
    "blk", "wht", "red", "blu", "ylw", "grn", "brn", "org", "jbb",
    # Packaging
    "ตัว", "แผง", "ถุง", "ซอง", "แพ็ค", "โหล", "แพ็คหัว", "แพ็คถุง",
    "อัดแผง", "แบบหลอด",
    # Misc
    "ใบ", "ดอก", "ชุด",
}


_THAI_RANGE = "฀-๿"
_TOKEN_RE = re.compile(rf"[{_THAI_RANGE}]+|[A-Za-z]+")


def _tokenize(name: str) -> set[str]:
    """Tokenize product name into a set of useful tokens.
    Drops: short tokens (<2 chars), pure numeric, model codes (#xxx),
    sizes (Nin, Nmm, Ncm, Nxc), color-code-in-parens, stop tokens."""
    if not name:
        return set()
    # Strip (#model), (color), sizes, ranges, packaging suffixes
    s = name
    s = re.sub(r"#\S+", " ", s)  # model codes
    s = re.sub(r"\(\s*[^)]*\)", " ", s)  # all (parens) — color/packaging/condition
    s = re.sub(r"\d+(?:\.\d+)?\s*(?:in|cm|mm|m|kg|g|inch|นิ้ว|มิล|เซน)\b", " ", s, flags=re.I)
    s = re.sub(r"\d+[/xX×]\d+", " ", s)  # dimensions like 4x3 or 1/4
    s = re.sub(r"\b\d+\b", " ", s)  # bare numbers
    s = re.sub(r"['\"]", " ", s)

    tokens = set()
    for m in _TOKEN_RE.finditer(s):
        t = m.group(0).lower()
        if len(t) < 2:
            continue
        if t in STOP_TOKENS:
            continue
        tokens.add(t)
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _confidence(score: float, category_has_subcats: bool) -> str:
    if not category_has_subcats:
        return "unmatched_no_siblings"
    if score >= 0.55:
        return "high"
    if score >= 0.35:
        return "medium"
    if score >= 0.20:
        return "low"
    return "unmatched"


CSV_COLUMNS = [
    "product_id", "sku_int", "category_short", "current_subcat",
    "product_name",
    "proposed_subcat", "proposed_score", "proposed_confidence",
    "matched_sibling_pid", "matched_sibling_name",
    "alternate_subcat", "alternate_score", "alternate_sibling_name",
    "n_subcat_candidates_in_category",
    "approve",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--db", type=Path,
                    default=Path(os.environ.get("NORMALIZE_DB_PATH",
                                                 str(DEFAULT_DB))))
    ap.add_argument("--include-inactive", action="store_true",
                    help="Include is_active=0 products (default: active only)")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    # Load ALL products with category (siblings + targets) — limit by active flag
    active_filter = "" if args.include_inactive else "AND p.is_active = 1"
    rows = conn.execute(f"""
        SELECT p.id, p.sku, p.product_name,
               p.sub_category_short_code AS subcat,
               p.category_id,
               c.short_code AS cat_short
          FROM products p
          JOIN categories c ON c.id = p.category_id
         WHERE p.category_id IS NOT NULL
           {active_filter}
         ORDER BY c.short_code, p.id
    """).fetchall()

    # Index siblings by category_id → list of (subcat, name, tokens, pid)
    siblings_by_cat: dict = defaultdict(list)
    for r in rows:
        if r["subcat"]:
            siblings_by_cat[r["category_id"]].append({
                "pid": r["id"],
                "subcat": r["subcat"],
                "name": r["product_name"],
                "tokens": _tokenize(r["product_name"]),
            })

    # Find missing-subcat targets
    targets = [r for r in rows if not r["subcat"]]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()

        tier_counts: dict = defaultdict(int)

        for t in targets:
            cat_id = t["category_id"]
            cat_short = t["cat_short"]
            target_tokens = _tokenize(t["product_name"])
            siblings = siblings_by_cat.get(cat_id, [])
            n_candidates = len(siblings)

            scored = []
            for sib in siblings:
                score = _jaccard(target_tokens, sib["tokens"])
                if score > 0:
                    scored.append((score, sib))
            scored.sort(key=lambda x: -x[0])

            top_score, top = (scored[0] if scored else (0.0, None))
            alt_score, alt = (scored[1] if len(scored) > 1 else (0.0, None))

            conf = _confidence(top_score, n_candidates > 0)
            tier_counts[conf] += 1

            w.writerow({
                "product_id": t["id"],
                "sku_int": t["sku"],
                "category_short": cat_short,
                "current_subcat": "",
                "product_name": t["product_name"],
                "proposed_subcat": top["subcat"] if top else "",
                "proposed_score": f"{top_score:.2f}" if top else "",
                "proposed_confidence": conf,
                "matched_sibling_pid": top["pid"] if top else "",
                "matched_sibling_name": top["name"] if top else "",
                "alternate_subcat": alt["subcat"] if alt else "",
                "alternate_score": f"{alt_score:.2f}" if alt else "",
                "alternate_sibling_name": alt["name"] if alt else "",
                "n_subcat_candidates_in_category": n_candidates,
                "approve": "",
            })

    conn.close()
    print(f"Wrote {len(targets)} rows → {args.output}")
    print("Confidence tier breakdown:")
    for tier in ("high", "medium", "low", "unmatched", "unmatched_no_siblings"):
        if tier in tier_counts:
            print(f"  {tier:>25s}: {tier_counts[tier]}")


if __name__ == "__main__":
    main()
