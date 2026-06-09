"""Auto-fix mechanical SKU naming issues identified by audit_sku_naming.py.

Handles 5 issue types where the fix is purely textual and safe:

  1. INCH_QUOTE   `3"` → `3นิ้ว`
  2. INCH_SPACED  `3 นิ้ว` → `3นิ้ว`
  3. INCH_FRAC    `1 1/2` → `1.5`  (and other common fractions)
  4. P_LEGACY     `(P)` → `(แผง)`
  5. DOUBLE_SPACE `  ` → ` ` (and trim trailing spaces)

NOT handled (need manual review):
  - NO_BRAND    — depends on which brand to assign
  - BARE_COLOR  — depends on which color the bare code maps to and where to
                  insert the Thai prefix
  - NO_HASH     — depends on which token is the model
  - NEW_COLOR   — needs human judgement on whether it's a real color

Default mode is DRY-RUN: writes proposed (before, after) pairs to a CSV
for review. Use --apply to write changes back to the DB.

CLI:
    python sendy_erp/scripts/autofix_sku_naming.py
    python sendy_erp/scripts/autofix_sku_naming.py --apply
    python sendy_erp/scripts/autofix_sku_naming.py --output /tmp/preview.csv
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
DEFAULT_PREVIEW = ROOT / "data" / "exports" / "sku_naming_autofix_preview.csv"


# --- transformations --------------------------------------------------------

# Common fraction → decimal map. Only fixes 'whole space fraction' patterns
# like '1 1/2' (NOT bare '1/2' which is often a thread spec like '11/32').
_FRAC_MAP = {
    "1/2":  "0.5",
    "1/4":  "0.25",
    "3/4":  "0.75",
    "1/8":  "0.125",
    "3/8":  "0.375",
    "5/8":  "0.625",
    "7/8":  "0.875",
    "1/3":  "0.33",
    "2/3":  "0.67",
}


def fix_inch_frac(name: str) -> str:
    """`1 1/2` → `1.5`. Only when fraction is in _FRAC_MAP."""
    def repl(m: re.Match) -> str:
        whole = int(m.group(1))
        frac = m.group(2)
        if frac in _FRAC_MAP:
            decimal = whole + float(_FRAC_MAP[frac])
            # format: drop trailing zeros, keep e.g. '1.5' not '1.50'
            return f"{decimal:g}"
        return m.group(0)
    return re.sub(r"(\d+)\s+(\d/\d)", repl, name)


def fix_inch_quote(name: str) -> str:
    """`3"` or `3 "` → `3นิ้ว`. Allows optional whitespace between the digit
    and the inch mark; matches straight + curly variants (" ” ″)."""
    return re.sub(r"(\d)\s*[\"”″]", r"\1นิ้ว", name)


def fix_inch_spaced(name: str) -> str:
    """`3 นิ้ว` → `3นิ้ว`. Removes whitespace between digit and 'นิ้ว'."""
    return re.sub(r"(\d)\s+นิ้ว", r"\1นิ้ว", name)


def fix_p_legacy(name: str) -> str:
    """`(P)` or `(p)` → `(แผง)`."""
    return re.sub(r"\(\s*[Pp]\s*\)", "(แผง)", name)


def fix_double_space(name: str) -> str:
    """Collapse runs of whitespace to a single space, trim ends."""
    return re.sub(r"\s+", " ", name).strip()


def autofix(name: str) -> str:
    """Run all fixes in order. INCH_FRAC must run before INCH_QUOTE so
    `1 1/2"` becomes `1.5นิ้ว`, not `1 1/2นิ้ว`."""
    out = name
    out = fix_inch_frac(out)
    out = fix_inch_quote(out)
    out = fix_inch_spaced(out)
    out = fix_p_legacy(out)
    out = fix_double_space(out)
    return out


# --- main -------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="write changes to DB (default: dry-run)")
    p.add_argument("--only-active", action="store_true", default=True,
                   help="only process is_active=1 (default true)")
    p.add_argument("--all", action="store_true",
                   help="process all products including is_active=0")
    p.add_argument("--output", type=Path, default=DEFAULT_PREVIEW,
                   help=f"preview CSV path (default: {DEFAULT_PREVIEW})")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, product_name FROM products"
    if not args.all:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    rows = conn.execute(sql).fetchall()

    changes = []
    for r in rows:
        before = r["product_name"]
        after = autofix(before)
        if before != after:
            changes.append((r["id"], before, after))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "before", "after"])
        for row in changes:
            w.writerow(row)

    print(f"Total products scanned: {len(rows)}")
    print(f"Changes proposed:       {len(changes)}")
    print(f"Preview CSV:            {args.output}")
    print()

    if not changes:
        print("Nothing to do.")
        return

    print("First 10 changes:")
    for pid, before, after in changes[:10]:
        print(f"  [{pid}]")
        print(f"    - {before}")
        print(f"    + {after}")

    if args.apply:
        print()
        print("Applying changes to DB...")
        # PRAGMA foreign_keys is per-connection; not strictly needed for
        # an UPDATE that doesn't touch FK columns, but enable for safety.
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        for pid, _before, after in changes:
            cur.execute(
                "UPDATE products SET product_name = ? WHERE id = ?",
                (after, pid),
            )
        conn.commit()
        print(f"Updated {len(changes)} rows. audit_log captures each UPDATE.")
    else:
        print()
        print("DRY-RUN — no DB changes made. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
