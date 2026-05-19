"""
Apply mapping CSV → product_barcodes table.

Reads:
  ERP/data/exports/barcode_mapping_matched.csv  (auto-applied)
Writes:
  product_barcodes rows (idempotent on barcode UNIQUE)

Run with --review to also import the review.csv tier (manual confidence).
"""
import argparse
import csv
import os
import sqlite3

DB_PATH  = os.path.expanduser("~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db")
EXPORTS  = os.path.expanduser("~/Sendai-Boonsawat/sendy_erp/data/exports")

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_barcodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  barcode     TEXT    NOT NULL UNIQUE,
  is_primary  INTEGER NOT NULL DEFAULT 0,
  source      TEXT,
  note        TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_product_barcodes_product ON product_barcodes(product_id);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def import_csv(conn, path, source):
    if not os.path.exists(path):
        print(f"  skip (not found): {path}")
        return 0, 0
    inserted = skipped = 0
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            barcode = (row.get("barcode") or "").strip()
            pid_raw = (row.get("product_id") or "").strip()
            if not barcode or not pid_raw:
                skipped += 1
                continue
            try:
                pid = int(pid_raw)
            except ValueError:
                skipped += 1
                continue
            try:
                conn.execute(
                    "INSERT INTO product_barcodes (product_id, barcode, source, note) "
                    "VALUES (?, ?, ?, ?)",
                    (pid, barcode, source, row.get("match_reason") or "")
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
    conn.commit()
    return inserted, skipped


def mark_primaries(conn):
    """For each product, mark the lowest-id barcode as primary."""
    conn.execute(
        "UPDATE product_barcodes SET is_primary=0 WHERE is_primary=1"
    )
    conn.execute("""
        UPDATE product_barcodes
           SET is_primary = 1
         WHERE id IN (
             SELECT MIN(id) FROM product_barcodes GROUP BY product_id
         )
    """)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", action="store_true",
                    help="Also import review-tier matches")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    print("Importing matched...")
    ins, skip = import_csv(conn, os.path.join(EXPORTS, "barcode_mapping_matched.csv"), "xls_matched")
    print(f"  inserted={ins} skipped={skip}")

    if args.review:
        print("Importing review...")
        ins2, skip2 = import_csv(conn, os.path.join(EXPORTS, "barcode_mapping_review.csv"), "xls_review")
        print(f"  inserted={ins2} skipped={skip2}")

    mark_primaries(conn)

    n = conn.execute("SELECT COUNT(*) FROM product_barcodes").fetchone()[0]
    np = conn.execute("SELECT COUNT(DISTINCT product_id) FROM product_barcodes").fetchone()[0]
    print(f"\nproduct_barcodes: {n} rows, {np} distinct products")
    conn.close()


if __name__ == "__main__":
    main()
