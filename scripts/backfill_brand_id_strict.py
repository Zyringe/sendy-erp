"""DEPRECATED: one-off from 2026-05-08. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Strict brand backfill: SET products.brand_id WHERE NULL and brand name
appears in product_name as a standalone word (\\b token \\b regex).

Stricter than brand_backfill_suggest.py — uses word-boundary matching to
avoid false positives. Tries each brand's name → name_th → short_code in
order; first hit wins. Skips ambiguous short codes (≤2 chars).

Default mode is dry-run. Use --apply to commit.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # All brands sorted by token length DESC so longer/more-specific names
    # win over short prefixes (e.g. "Golden Lion" before "GL").
    brands = conn.execute(
        "SELECT id, name, name_th, short_code FROM brands"
    ).fetchall()

    # Build matchers — (token, brand_id) sorted longest first
    matchers = []
    for b in brands:
        for tok in (b['name'], b['name_th'], b['short_code']):
            if not tok or len(tok) < 3:
                continue
            matchers.append((tok, b['id']))
    matchers.sort(key=lambda m: -len(m[0]))

    products = conn.execute(
        "SELECT id, product_name FROM products WHERE brand_id IS NULL"
    ).fetchall()

    proposed = []
    for p_row in products:
        name = p_row['product_name']
        for tok, bid in matchers:
            # Word-boundary match for ASCII; substring for Thai (no \b for Thai)
            if tok.isascii():
                pattern = rf"\b{re.escape(tok)}\b"
                if re.search(pattern, name, re.IGNORECASE):
                    proposed.append((p_row['id'], bid, tok))
                    break
            else:
                if tok in name:
                    proposed.append((p_row['id'], bid, tok))
                    break

    print(f"Products with brand_id NULL: {len(products)}")
    print(f"  → would set brand_id:      {len(proposed)}")
    print()
    print("First 8 proposals:")
    by_brand = {}
    for pid, bid, tok in proposed:
        by_brand[bid] = by_brand.get(bid, 0) + 1
    for bid, n in sorted(by_brand.items(), key=lambda kv: -kv[1])[:10]:
        bname = next((b['name'] for b in brands if b['id'] == bid), '?')
        print(f"  brand_id={bid:>3}  ({bname:<15}) — {n} products")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    cur = conn.cursor()
    for pid, bid, tok in proposed:
        cur.execute("UPDATE products SET brand_id = ? WHERE id = ?", (bid, pid))
    conn.commit()
    print(f"\nApplied {len(proposed)} updates")
    conn.close()


if __name__ == "__main__":
    main()
