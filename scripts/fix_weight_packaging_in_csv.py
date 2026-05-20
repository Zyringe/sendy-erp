"""DEPRECATED: one-off from 2026-05-07. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Move weight values out of `packaging` column → `size` column.

Reads sku_name_rebuilt_2026-05-07.csv (or any rebuilt CSV).
For each row where `packaging` looks like a weight (kg / g / ขีด / kg):
  - If `size` is empty: set size = weight
  - If `size` already has a value: set size = "<existing_size>/<weight>"
  - Clear packaging
  - Color_th 'สีฝุ่น' weight goes here too

Output: <input>.weightfix.csv
Diff:   <input>.weightfix.diff.csv

Run build_name_from_columns.py on the output afterwards to regenerate
proposed_name with the relocated weight visible in the name.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

WEIGHT_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(kg|g|ขีด|กก|กรัม)\s*$", re.IGNORECASE)


def is_weight(s: str) -> bool:
    return bool(WEIGHT_RE.match(s.strip()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", type=Path, help="default: <input>.weightfix.csv")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"input not found: {args.input}")

    out = args.output or args.input.with_suffix(".weightfix.csv")
    diff = args.input.with_suffix(".weightfix.diff.csv")

    with args.input.open(encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        rows = list(rdr)
        fields = rdr.fieldnames

    diffs = []
    for r in rows:
        pkg = r["packaging"].strip()
        if not pkg or not is_weight(pkg):
            continue
        old_size = r["size"].strip()
        if old_size:
            new_size = f"{old_size}-{pkg}"
        else:
            new_size = pkg
        diffs.append({
            "sku": r["sku"],
            "old_size": old_size,
            "new_size": new_size,
            "old_packaging": pkg,
            "product_name": r["product_name"],
        })
        r["size"] = new_size
        r["packaging"] = ""

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    with diff.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "old_size", "new_size", "old_packaging", "product_name"])
        w.writeheader()
        w.writerows(diffs)

    print(f"Rows total:    {len(rows)}")
    print(f"Rows changed:  {len(diffs)} (weight moved packaging → size)")
    print(f"Output:        {out}")
    print(f"Diff:          {diff}")
    if diffs:
        print()
        print("First 8 changes:")
        for d in diffs[:8]:
            print(f"  sku={d['sku']:>5}  size: {d['old_size']!r:<10} → {d['new_size']!r}  (was pkg={d['old_packaging']!r})")


if __name__ == "__main__":
    main()
