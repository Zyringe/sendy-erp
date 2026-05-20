"""DEPRECATED: one-off from 2026-05-08. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Apply broad-category mapping from subcategory_mapping_review.csv to
products.category_id, plus add any user-proposed new broad categories.

Resolution rules per CSV row (one row per distinct sub_category):
  1. If user_override_code present → use it (Put's override)
  2. Else if proposed_category_code present → use it (auto-match)
  3. Else if new_broad_category_code+name_th present → INSERT new category, use it
  4. Else → skip (category_id stays NULL)

Default mode is dry-run. Use --apply to commit.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_CSV = ROOT / "data" / "exports" / "subcategory_mapping_review.csv"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    with open(args.csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Existing code → id
    code_to_id = {c[0]: c[1] for c in conn.execute("SELECT code, id FROM categories")}

    new_cats_to_add = []
    sub_to_id = {}      # resolved sub_category → category_id
    skipped_no_resolve = []

    for r in rows:
        sub = r["sub_category"]
        # priority: user_override > new_broad > proposed
        chosen_code = (r.get("user_override_code") or "").strip()
        if not chosen_code:
            new_code = (r.get("new_broad_category_code") or "").strip()
            new_name = (r.get("new_broad_category_name_th") or "").strip()
            if new_code and new_name:
                new_cats_to_add.append((new_code, new_name))
                chosen_code = new_code
        if not chosen_code:
            chosen_code = (r.get("proposed_category_code") or "").strip()
        if not chosen_code:
            skipped_no_resolve.append(sub)
            continue
        sub_to_id[sub] = chosen_code  # store as code for now; resolve to id after INSERT

    # Insert any user-proposed new categories (deduped)
    seen = set()
    new_inserts = 0
    for code, name_th in new_cats_to_add:
        if code in code_to_id or code in seen:
            continue
        seen.add(code)
        if args.apply:
            cur.execute(
                "INSERT INTO categories (code, name_th, sort_order) VALUES (?, ?, 130)",
                (code, name_th)
            )
            code_to_id[code] = cur.lastrowid
        new_inserts += 1

    # Now bulk UPDATE products.category_id
    n_updated = 0
    n_no_target = 0
    for sub, code in sub_to_id.items():
        cat_id = code_to_id.get(code)
        if cat_id is None:
            n_no_target += 1
            continue
        if args.apply:
            cur.execute(
                "UPDATE products SET category_id = ? WHERE sub_category = ?",
                (cat_id, sub)
            )
            n_updated += cur.rowcount
        else:
            n_updated += conn.execute(
                "SELECT COUNT(*) FROM products WHERE sub_category = ?", (sub,)
            ).fetchone()[0]

    if args.apply:
        conn.commit()

    print(f"Sub_categories resolved:    {len(sub_to_id)}")
    print(f"  → New broad categories inserted: {new_inserts}")
    print(f"  → Sub_categories skipped (no proposal/override): {len(skipped_no_resolve)}")
    print(f"  → Sub_categories with unresolved code (typo?): {n_no_target}")
    print()
    print(f"products.category_id rows {'updated' if args.apply else 'would update'}: {n_updated}")
    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    # Sanity stats after apply
    print()
    coverage = conn.execute(
        "SELECT COUNT(*) FROM products WHERE category_id IS NOT NULL"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    print(f"Final coverage: {coverage}/{total} ({coverage*100/total:.1f}%) products have category_id")
    conn.close()


if __name__ == "__main__":
    main()
