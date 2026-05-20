"""DEPRECATED: one-off from 2026-05-08. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Backfill products.color_code where it's NULL by parsing product_name and
matching detected color_th to color_finish_codes.name_th.

Useful after a new color_finish_codes migration adds basic colors (e.g. mig
038 BLK/WHT/RED..., mig 042 MIX/TRN/...). The mass-rename apply ran before
those migrations, so existing products with bare-color names like 'สีดำ'
were left with color_code=NULL even though BLK now exists.

Default mode is dry-run. Use --apply to commit.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
SCRIPTS_DIR = ROOT / "scripts"

# Make parse_sku_names importable
sys.path.insert(0, str(SCRIPTS_DIR))
import parse_sku_names as psn  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Load context for parser
    color_rows = conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY length(code) DESC"
    ).fetchall()
    color_codes = {r["code"]: r["name_th"] for r in color_rows}

    brand_rows = conn.execute(
        "SELECT id, code, name, name_th, short_code FROM brands"
    ).fetchall()
    all_brand_tokens = []
    token_to_brand = {}
    for r in brand_rows:
        for k in ("name", "name_th", "short_code"):
            v = r[k]
            if v:
                all_brand_tokens.append(v)
                token_to_brand[v] = r["name"]

    # Reverse map: name_th → code (for filling code from parsed color_th)
    name_to_code = {nm: code for code, nm in color_codes.items()}

    # Pull all products with NULL color_code
    rows = conn.execute("""
        SELECT id, sku, product_name
          FROM products
         WHERE color_code IS NULL OR color_code = ''
    """).fetchall()

    proposed = []
    skipped_no_color = 0
    skipped_no_match = 0

    for r in rows:
        parsed = psn.parse_name(
            r["product_name"], brand_rec=None,
            color_codes=color_codes,
            all_brand_tokens=all_brand_tokens,
            token_to_brand=token_to_brand,
        )
        # parser may already populate color_code via its own reverse-fill
        new_code = (parsed.get("color_code") or "").strip()
        if not new_code:
            ct = (parsed.get("color_th") or "").strip()
            if not ct:
                skipped_no_color += 1
                continue
            new_code = name_to_code.get(ct)
            if not new_code:
                skipped_no_match += 1
                continue
        proposed.append({
            "id": r["id"], "sku": r["sku"],
            "color_code": new_code,
            "product_name": r["product_name"],
        })

    print(f"Products with NULL color_code:    {len(rows)}")
    print(f"  no color_th detected by parser: {skipped_no_color}")
    print(f"  color_th detected but no code:  {skipped_no_match}")
    print(f"  → would update:                 {len(proposed)}")
    print()
    print("First 10 proposals:")
    for x in proposed[:10]:
        print(f"  sku={x['sku']:>5}  → color_code={x['color_code']:<5}  ({x['product_name'][:60]})")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    cur = conn.cursor()
    for x in proposed:
        cur.execute(
            "UPDATE products SET color_code = ? WHERE id = ?",
            (x["color_code"], x["id"])
        )
    conn.commit()
    print(f"\nApplied {len(proposed)} updates")


if __name__ == "__main__":
    main()
