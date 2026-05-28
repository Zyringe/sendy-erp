#!/usr/bin/env python3
"""Apply approved subcat assignments from subcat_round1 CSV.

Usage:
  python sendy_erp/scripts/apply_subcat_coverage.py --csv <path>           # dry-run
  python sendy_erp/scripts/apply_subcat_coverage.py --csv <path> --commit  # actually write

Safety mirrors apply_normalize_round1.py:
  - SAVEPOINT per row (failures don't abort batch)
  - Pre-flight DB backup to data/backups/inventory_pre_subcat_<ts>.db
  - import_log entry on commit
  - Honors sku_code_locked guard (skip; user can update via UI to override)

Approve gate: only rows with approve='Y' (case-insensitive, stripped) apply.
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


def _is_approved(row: dict) -> bool:
    return (row.get("approve") or "").strip().upper() == "Y"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--db", type=Path,
                    default=Path(os.environ.get("NORMALIZE_DB_PATH",
                                                 str(DEFAULT_DB))))
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")
    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"inventory_pre_subcat_{ts}.db"
    shutil.copy2(args.db, backup)
    print(f"DB backup → {backup}")

    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        all_rows = list(csv.DictReader(f))
    approved = [r for r in all_rows if _is_approved(r)]
    print(f"CSV rows: {len(all_rows)} total · {len(approved)} approved")

    if not approved:
        print("Nothing to apply. Exit.")
        return 0

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")

    n_updated = 0
    n_errors = 0
    n_skipped_locked = 0
    n_no_proposal = 0
    error_lines = []

    for r in approved:
        pid = int(r["product_id"])
        proposed = (r.get("proposed_subcat") or "").strip()
        if not proposed:
            n_no_proposal += 1
            continue
        sp = f"sp_{pid}"
        try:
            conn.execute(f"SAVEPOINT {sp}")
            cur = conn.execute(
                "SELECT sku_code_locked FROM products WHERE id = ?", (pid,)
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
            conn.execute(
                "UPDATE products SET sub_category_short_code = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (proposed, pid),
            )
            conn.execute(f"RELEASE {sp}")
            n_updated += 1
        except Exception as e:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            error_lines.append(f"  pid={pid}: {type(e).__name__}: {e}")
            n_errors += 1

    summary = (
        f"Subcat apply summary:\n"
        f"  updated:        {n_updated}\n"
        f"  skipped-locked: {n_skipped_locked}\n"
        f"  no-proposal:    {n_no_proposal}\n"
        f"  errors:         {n_errors}"
    )

    if args.commit:
        conn.execute(
            "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
            "VALUES (?, ?, ?, ?)",
            (
                f"subcat_coverage:{args.csv.name}",
                n_updated,
                n_skipped_locked + n_no_proposal + n_errors,
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

    conn.close()
    return 0 if n_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
