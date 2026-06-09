#!/usr/bin/env python3
"""Backup products.material values before mig 087 drops the column.

Run BEFORE deploying mig 087 (the runner auto-applies on next sendy-up):
    python sendy_erp/scripts/backup_material_pre_mig087.py

Writes a timestamped CSV to sendy_erp/data/backups/ with (id,
product_name, material) for every row where material IS NOT NULL.

Idempotent: re-runs append a new timestamped file. Skips with a clear
message if the material column is already gone (mig 087 already applied).
"""
import csv
import os
import sqlite3
import sys
from datetime import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(REPO, "inventory_app", "instance", "inventory.db")
BACKUP_DIR = os.path.join(REPO, "data", "backups")


def main():
    if not os.path.exists(DB):
        print(f"ERROR: DB not found at {DB}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BACKUP_DIR, exist_ok=True)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    if "material" not in cols:
        print("products.material column is already gone — mig 087 already applied.")
        print("Existing backups (if any):")
        for f in sorted(os.listdir(BACKUP_DIR)):
            if f.startswith("material_values_pre_mig087_"):
                print(f"  {os.path.join(BACKUP_DIR, f)}")
        sys.exit(0)

    rows = conn.execute(
        "SELECT id, product_name, material FROM products "
        "WHERE material IS NOT NULL AND material != '' "
        "ORDER BY id"
    ).fetchall()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(BACKUP_DIR, f"material_values_pre_mig087_{ts}.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "product_name", "material"])
        for r in rows:
            w.writerow([r["id"], r["product_name"], r["material"]])

    print(f"Backed up {len(rows)} material values → {out}")
    conn.close()


if __name__ == "__main__":
    main()
