"""One-off, 2026-07-10.

Bucket-1 fix from Put's /grilling session on the settled-status matcher PR
(#281): re-sync marketplace_order_items.internal_product_id from the CURRENT
platform_skus mapping wherever they've drifted apart.

Root cause: internal_product_id is resolved from platform_skus at ORDER-IMPORT
time and frozen onto marketplace_order_items — it is never re-synced when
platform_skus.internal_product_id is corrected later (e.g. a sibling-SKU
mapping fix). So a listing mapping fixed after some orders already imported
leaves those older orders silently pointing at the old (wrong) product
forever, which in turn makes marketplace_match.py unable to product-match
them (the order's product never appears on any Express invoice).

This is zero-guessing: platform_skus is already-established ground truth
elsewhere in this DB; we are only re-deriving order_items from it, not
inventing any new mapping. Scope: rows where
  - platform_skus has a NON-NULL internal_product_id for the same
    (platform, variation_id), AND
  - marketplace_order_items.internal_product_id disagrees with it.
NULL internal_product_id on either side is left untouched (that's the
separate no-join-key / never-mapped bucket, not this fix).

Dry-run by default. --apply commits. Backs up first (sqlite3 .backup, not cp
— WAL).

CORRECTION (same day, before this ever touched the live DB): "disagrees with
the current platform_skus value" is NOT by itself a safe resync criterion.
Found via the mandatory dry-run: a Lazada rivet listing's real physical
product changed over time (DOME-head -> CSK-head rivets) under the SAME
variation_id — platform_skus was correctly updated for NEW orders, but 34
older orders genuinely WERE the old product (confirmed: the old pid has 34
real matching invoices covering exactly their date range; the new pid's
invoice history only starts the week the frozen order_items value itself
also flips to the new pid). Blindly resyncing to "whatever platform_skus
says now" would have broken those 34 already-correct historical matches.

Correct, narrower criterion: only resync when the OLD (frozen) pid has ZERO
invoice history ever on this customer_code — i.e. it was never correct at
any point, so there is no historical period it could legitimately belong to.
That is a strictly stronger/safer bar than "disagrees with platform_skus" and
excludes exactly the temporal-changeover case above while still catching the
genuine never-was-right mixups (confirmed clean for all rows this file
touches: the old pid's IV-history count is 0).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
BACKUP_DIR = ROOT / "data" / "backups"

_CUST_CODE = {"shopee": "Zหน้าร้าน", "lazada": "Lหน้าร้าน"}

_SELECT_CANDIDATES = """
    SELECT oi.id, oi.platform, oi.order_sn, oi.variation_id,
           oi.internal_product_id AS old_pid, ps.internal_product_id AS new_pid
    FROM marketplace_order_items oi
    JOIN platform_skus ps ON ps.platform = oi.platform AND ps.variation_id = oi.variation_id
    WHERE ps.internal_product_id IS NOT NULL
      AND oi.internal_product_id IS NOT NULL
      AND oi.internal_product_id != ps.internal_product_id
"""


def _old_pid_ever_invoiced(conn, customer_code, pid):
    row = conn.execute(
        "SELECT COUNT(*) c FROM sales_transactions "
        "WHERE customer_code=? AND product_id=? AND doc_base LIKE 'IV%'",
        (customer_code, pid),
    ).fetchone()
    return row["c"] > 0


def find_stale(conn):
    """Candidates that disagree with platform_skus AND whose OLD pid has never
    once been invoiced on this customer_code (see module docstring)."""
    out = []
    for r in conn.execute(_SELECT_CANDIDATES).fetchall():
        code = _CUST_CODE.get(r["platform"])
        if code and _old_pid_ever_invoiced(conn, code, r["old_pid"]):
            continue  # old pid has real history — leave it (temporal-changeover risk)
        out.append(r)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    stale = find_stale(conn)
    print(f"stale order_items: {len(stale)}")
    for r in stale:
        print(f"  order_sn={r['order_sn']} platform={r['platform']} "
              f"pid {r['old_pid']} -> {r['new_pid']}")

    if not args.apply:
        print("\nDRY RUN — no changes made. Re-run with --apply to commit.")
        conn.close()
        return

    if not stale:
        print("\nNothing to apply.")
        conn.close()
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"pre_resync_stale_pid_{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(args.db))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    src.close()
    dst.close()
    print(f"\nBackup written: {backup_path}")

    conn.execute("BEGIN IMMEDIATE")
    for r in stale:
        conn.execute(
            "UPDATE marketplace_order_items SET internal_product_id = ? WHERE id = ?",
            (r["new_pid"], r["id"]),
        )
    conn.commit()

    # Independent re-verify in the same run.
    remaining = find_stale(conn)
    print(f"\nApplied {len(stale)} updates. Remaining stale rows: {len(remaining)}")
    if remaining:
        raise SystemExit(f"FAILED verification: {len(remaining)} stale rows remain")
    conn.close()


if __name__ == "__main__":
    main()
