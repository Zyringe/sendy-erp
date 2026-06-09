"""Image coverage report — cross-reference products vs the photo tree.

Scans every .png/.jpg under Design/Catalog/photos/products/<cat>/{single,pack}/
(skipping _pending*, _unmatched, family/ — those still need Put's triage)
and reports which active products have at least one photo file mapped via
sku_code prefix and which have none.

Outputs two CSVs to data/exports/:
  - image_coverage_uncovered.csv — active products with zero photos.
    Triage list for the next photo-sourcing batch.
  - image_coverage_summary.csv — per-category counts (total / covered /
    uncovered / coverage %).

Usage:
    python sendy_erp/scripts/image_coverage_report.py
"""
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "inventory_app"))
os.environ.setdefault("SECRET_KEY", "dev")
os.environ.setdefault("ADMIN_PASSWORD", "dev")

PHOTOS_ROOT = Path.home() / "Sendai-Boonsawat/Design/Catalog/photos/products"
DB_PATH = REPO / "inventory_app/instance/inventory.db"
EXPORTS = REPO / "data/exports"

# Photo files are named like "DSC-CUT-AS-14in-GRN-EXP0727_01.png" — strip
# the extension and any trailing "_NN" counter to recover the sku_code.
# Don't try to constrain the character set: sku_codes contain mixed case
# (4in), digits, #, -, and occasional ".
TRAILING_COUNTER_RE = re.compile(r"_\d+$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _sku_from_filename(name: str) -> str:
    stem = os.path.splitext(name)[0]
    return TRAILING_COUNTER_RE.sub("", stem)


def collect_covered_skus() -> dict:
    """Walk single/ and pack/ subdirs of each category. Return
    {sku_code: count_of_photos}. Skip _pending*, _unmatched, family/."""
    covered = defaultdict(int)
    if not PHOTOS_ROOT.exists():
        print(f"WARNING: photo tree not found at {PHOTOS_ROOT}")
        return covered
    for cat_dir in PHOTOS_ROOT.iterdir():
        if not cat_dir.is_dir() or cat_dir.name.startswith("_"):
            continue
        for kind in ("single", "pack"):
            sub = cat_dir / kind
            if not sub.exists():
                continue
            for f in sub.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue
                covered[_sku_from_filename(f.name)] += 1
    return covered


def main():
    covered = collect_covered_skus()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id, p.sku_code, p.product_name, p.brand_id,
                  b.short_code AS brand_code, c.code AS cat_code
             FROM products p
             LEFT JOIN brands b ON b.id = p.brand_id
             LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.is_active = 1
            ORDER BY c.code, p.sku_code"""
    ).fetchall()

    uncovered_rows = []
    by_cat_total = defaultdict(int)
    by_cat_covered = defaultdict(int)
    for r in rows:
        cat = r["cat_code"] or "(no-cat)"
        by_cat_total[cat] += 1
        sku_code = r["sku_code"]
        if sku_code and sku_code in covered:
            by_cat_covered[cat] += 1
        else:
            uncovered_rows.append({
                "id": r["id"],
                "sku_code": sku_code or "",
                "product_name": r["product_name"],
                "brand_code": r["brand_code"] or "",
                "cat_code": cat,
            })

    EXPORTS.mkdir(parents=True, exist_ok=True)
    out_uncov = EXPORTS / "image_coverage_uncovered.csv"
    with open(out_uncov, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "id", "sku_code", "product_name", "brand_code", "cat_code",
        ])
        w.writeheader()
        w.writerows(uncovered_rows)

    out_sum = EXPORTS / "image_coverage_summary.csv"
    with open(out_sum, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["category", "total", "covered", "uncovered", "coverage_pct"])
        for cat in sorted(by_cat_total.keys()):
            tot = by_cat_total[cat]
            cov = by_cat_covered[cat]
            pct = (cov / tot * 100) if tot else 0
            w.writerow([cat, tot, cov, tot - cov, f"{pct:.1f}"])
        grand_tot = sum(by_cat_total.values())
        grand_cov = sum(by_cat_covered.values())
        grand_pct = (grand_cov / grand_tot * 100) if grand_tot else 0
        w.writerow(["TOTAL", grand_tot, grand_cov,
                    grand_tot - grand_cov, f"{grand_pct:.1f}"])

    print(f"covered SKUs across photo tree: {len(covered)}")
    print(f"active products: {len(rows)}")
    print(f"uncovered: {len(uncovered_rows)} → {out_uncov}")
    print(f"summary  : {out_sum}")


if __name__ == "__main__":
    main()
