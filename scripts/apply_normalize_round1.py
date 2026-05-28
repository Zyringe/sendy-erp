#!/usr/bin/env python3
"""Apply approved rows from the round-1 normalize CSV to products.

Workflow:
  1. Generate review CSV: `scripts/normalize_products_round1.py`
  2. Open in Excel, mark `approve="Y"` on rows to apply, save back
  3. Dry-run: `scripts/apply_normalize_round1.py --csv <path>`
  4. Apply: `scripts/apply_normalize_round1.py --csv <path> --commit`

Safety:
  - Pre-flight: copies inventory.db to sendy_erp/data/backups/inventory_pre_normalize_<ts>.db
  - SAVEPOINT per row; on error rolls back THAT row and continues
  - Respects `sku_code_locked=0` guard — won't overwrite locked sku_codes
  - Logs each apply to `import_log` (one row per apply session)

For Railway prod: run via `railway ssh` after the deploy that lands the
mig + code-scrub changes — applying locally only updates the local DB.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "inventory_app" / "instance" / "inventory.db"
BACKUP_DIR = REPO / "data" / "backups"


# Fields that get UPDATEd from CSV "_new" columns.
# Pairs: (csv_column, db_column)
APPLY_FIELDS = [
    ("series_new",          "series"),
    ("model_new",           "model"),
    ("size_new",            "size"),
    ("color_code_new",      "color_code"),
    ("packaging_th_new",    "packaging_th"),
    ("packaging_short_new", "packaging_short"),
    ("condition_new",       "condition"),
    ("pack_variant_new",    "pack_variant"),
]


def _is_approved(row: dict) -> bool:
    """Strict equality on 'Y' (case-insensitive, stripped)."""
    return (row.get("approve") or "").strip().upper() == "Y"


def _to_db_value(v: str | None):
    """CSV empty string → SQL NULL; otherwise pass through."""
    if v is None:
        return None
    s = v.strip()
    return s if s else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True,
                    help="Path to the reviewed normalize_round1_*.csv")
    ap.add_argument("--db", type=Path,
                    default=Path(os.environ.get("NORMALIZE_DB_PATH",
                                                 str(DEFAULT_DB))))
    ap.add_argument("--commit", action="store_true",
                    help="Actually apply (default: dry-run prints diff + rolls back)")
    args = ap.parse_args()

    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")
    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    # Pre-flight backup
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"inventory_pre_normalize_{ts}.db"
    shutil.copy2(args.db, backup)
    print(f"DB backup → {backup}")

    # Read CSV
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        all_rows = list(csv.DictReader(f))
    approved = [r for r in all_rows if _is_approved(r)]
    print(f"CSV rows: {len(all_rows)} total · {len(approved)} approved")

    if not approved:
        print("Nothing to apply (no rows with approve='Y'). Exit.")
        return 0

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # we manage txn manually

    conn.execute("BEGIN IMMEDIATE")

    n_updated = 0
    n_skipped_locked = 0
    n_errors = 0
    n_unchanged = 0
    error_lines = []

    for r in approved:
        pid = int(r["product_id"])
        proposed_name = r.get("proposed_name") or ""
        proposed_sku = r.get("proposed_sku_code") or ""
        sp = f"sp_{pid}"
        try:
            conn.execute(f"SAVEPOINT {sp}")

            cur = conn.execute(
                "SELECT id, sku_code_locked, product_name, sku_code FROM products WHERE id = ?",
                (pid,)
            ).fetchone()
            if cur is None:
                conn.execute(f"RELEASE {sp}")
                error_lines.append(f"  pid={pid}: not found")
                n_errors += 1
                continue
            if cur["sku_code_locked"]:
                conn.execute(f"RELEASE {sp}")
                n_skipped_locked += 1
                continue
            no_change = (
                cur["product_name"] == proposed_name and
                cur["sku_code"] == proposed_sku
            )
            if no_change:
                conn.execute(f"RELEASE {sp}")
                n_unchanged += 1
                continue

            # Build SET clause from approved structured columns + product_name + sku_code
            set_parts = ["product_name = ?", "sku_code = ?",
                         "updated_at = datetime('now','localtime')"]
            params = [proposed_name, proposed_sku]
            for csv_col, db_col in APPLY_FIELDS:
                set_parts.append(f"{db_col} = ?")
                params.append(_to_db_value(r.get(csv_col)))
            params.append(pid)

            conn.execute(
                f"UPDATE products SET {', '.join(set_parts)} WHERE id = ?",
                params,
            )
            conn.execute(f"RELEASE {sp}")
            n_updated += 1
        except Exception as e:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            error_lines.append(f"  pid={pid}: {type(e).__name__}: {e}")
            n_errors += 1

    summary = (
        f"Apply summary:\n"
        f"  updated:        {n_updated}\n"
        f"  skipped-locked: {n_skipped_locked}\n"
        f"  no-change:      {n_unchanged}\n"
        f"  errors:         {n_errors}"
    )

    if args.commit:
        # Log to import_log BEFORE commit
        conn.execute(
            "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
            "VALUES (?, ?, ?, ?)",
            (
                f"normalize_round1:{args.csv.name}",
                n_updated,
                n_skipped_locked + n_unchanged + n_errors,
                f"backup={backup.name}; errors={n_errors}",
            )
        )
        conn.execute("COMMIT")
        print(f"\n✓ COMMITTED. {summary}")
    else:
        conn.execute("ROLLBACK")
        print(f"\n(dry-run — rolled back) {summary}")

    if error_lines:
        print("\nErrors:")
        for line in error_lines[:20]:
            print(line)
        if len(error_lines) > 20:
            print(f"  ... and {len(error_lines) - 20} more")

    conn.close()
    return 0 if n_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
