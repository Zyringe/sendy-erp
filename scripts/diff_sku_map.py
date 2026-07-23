"""Diffs a pre-apply DB snapshot against the live (post-apply) DB to emit the
old->new sku_code map that `rename_sku_folders.py --map` consumes
(product-naming-round2 Phase 3 fix ซ, item 1). This component did not exist
before — `rename_sku_folders.py`'s own docstring referenced it as "the
by-product of apply_product_naming.py's sku-regen sweep", but nothing
actually produced that file. This script is that producer.

Usage: take a `.backup` snapshot of the DB BEFORE running
apply_product_naming.py --apply (round-2's sku-regen), apply the renames,
then diff the snapshot against the now-live DB:

    python diff_sku_map.py --backup pre_apply_snapshot.db \\
        --live /path/to/inventory.db --out sku_map.csv

Output columns are EXACTLY rename_sku_folders.py's `--map` contract:
product_id,old_sku,new_sku. A product whose sku_code changed between the two
DBs (join key: products.id, which a rename never touches — only sku_code
does) appears as one row. Either side may be NULL/missing (represented as an
empty CSV field) — a product that gained or lost a sku_code entirely still
counts as a change and is NOT filtered out. A product that exists in only
one of the two DBs (added/deleted between the snapshot and now) is NOT a
rename and is excluded.

Both DBs are opened strictly READ-ONLY for the actual diff query — this
script never writes to product data in either DB. The ONE exception: a
`.backup`-taken (or `sqlite3.Connection.backup()`-taken — same underlying
online-backup API) snapshot inherits its journal_mode flag from whatever
mode the SOURCE db was in (this repo's sendy DB runs WAL) but starts with
NO accompanying -wal/-shm files. Opening a WAL-flagged db with no -wal/-shm
companions via `mode=ro` fails outright ("unable to open database file") —
SQLite needs write access to create the missing shared-memory index and
mode=ro forbids that. Verified directly: a fresh `.backup` of a WAL-mode
source fails mode=ro immediately; the LIVE db (already running normally,
with its -wal/-shm companions already present from the app) opens mode=ro
with no special handling, which is why only the --backup path gets this
treatment. The fix is a one-time HEADER-ONLY flip — open the backup file
normally (writable) and switch it to DELETE (rollback) journal mode; this
never touches a single table row, and is idempotent (a no-op if the file
happens to already be off WAL mode).

CLI:
    python diff_sku_map.py --backup pre_apply_snapshot.db --live inventory.db --out sku_map.csv
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


def normalize_backup_journal_mode(backup_path) -> None:
    """One-time, data-untouching fix for the WAL-inheritance gotcha
    described in the module docstring. Only ever call this on a `--backup`
    snapshot path — never on the live DB (which needs no such treatment and
    must stay strictly read-only end to end)."""
    conn = sqlite3.connect(str(backup_path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.commit()
    finally:
        conn.close()


def diff_sku_map(backup_path, live_path) -> list:
    """Read-only SELECTs on both DBs. Returns a list of
    {product_id, old_sku, new_sku} dicts, sorted by product_id, for every
    product present in BOTH DBs whose sku_code differs between them. NULL
    sku_code is represented as ''."""
    backup_conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    live_conn = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
    try:
        old_skus = dict(backup_conn.execute("SELECT id, sku_code FROM products").fetchall())
        new_skus = dict(live_conn.execute("SELECT id, sku_code FROM products").fetchall())
    finally:
        backup_conn.close()
        live_conn.close()

    common_ids = sorted(set(old_skus) & set(new_skus))
    rows = []
    for pid in common_ids:
        old, new = old_skus[pid], new_skus[pid]
        if old != new:
            rows.append({"product_id": str(pid), "old_sku": old or "", "new_sku": new or ""})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", required=True, type=Path, help="pre-apply DB snapshot (via `.backup`)")
    ap.add_argument("--live", required=True, type=Path, help="post-apply (current) DB")
    ap.add_argument("--out", required=True, type=Path, help="output CSV: product_id,old_sku,new_sku")
    args = ap.parse_args(argv)

    if args.backup.resolve() == args.live.resolve():
        print(f"ERROR: --backup and --live resolve to the SAME file ({args.backup.resolve()}) — "
              f"refusing to diff a database against itself.", file=sys.stderr)
        return 1

    normalize_backup_journal_mode(args.backup)
    rows = diff_sku_map(args.backup, args.live)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "old_sku", "new_sku"])
        w.writeheader()
        w.writerows(rows)

    if not rows:
        print(f"No sku_code changes found between {args.backup} and {args.live} — nothing to map. "
              f"Empty map (header only) written to {args.out}.")
    else:
        print(f"{len(rows)} sku_code change(s) found -> {args.out}")
        for r in rows:
            print(f"  pid {r['product_id']}: {r['old_sku'] or '(NULL)'} -> {r['new_sku'] or '(NULL)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
