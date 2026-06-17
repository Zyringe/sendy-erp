#!/usr/bin/env python
"""One-time rollout for mig 111 (opening_cost decoupling).

Two steps, in order:
  1. Backfill products.opening_cost from the PRE-2026-06-17-sync backup's cost_price
     (the true opening basis: 0 for the 832 zero-synced products, real cost for the
     rest). The mig left opening_cost = current cost_price as a placeholder, which is
     the post-sync WACC for the synced products — seeding from that would compound.
  2. Recompute every active product so cost_price = live WACC seeded from the corrected
     opening_cost. Fixes the ~74 products whose cost_price still lagged their WACC.

Idempotent: re-running yields the same cost_price (opening_cost is the immutable seed).

Usage (point DATA_DIR at the DB to operate on — a COPY first, then live):
    DATA_DIR=/tmp/sim DBBACKUP=/abs/path/local-pre-costsync-20260617.db \
        SECRET_KEY=x ADMIN_PASSWORD=x ~/.virtualenvs/erp/bin/python \
        scripts/backfill_opening_cost_20260617.py --apply

Without --apply it only prints the plan (dry run).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'inventory_app'))

import models  # noqa: E402

APPLY = '--apply' in sys.argv
BACKUP = os.environ['DBBACKUP']


def main():
    conn = models.get_connection()
    conn.execute("PRAGMA foreign_keys = ON")

    # Sanity: the backup must be readable and have a products table.
    conn.execute("ATTACH ? AS bk", (BACKUP,))
    n_bk = conn.execute("SELECT COUNT(*) FROM bk.products").fetchone()[0]
    n_live = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    print(f"backup products={n_bk}  live products={n_live}")

    # How many opening_cost values will actually move off the mig placeholder?
    to_change = conn.execute(
        """SELECT COUNT(*) FROM products p JOIN bk.products b ON b.id=p.id
           WHERE ABS(COALESCE(p.opening_cost,0)-COALESCE(b.cost_price,0))>0.0001"""
    ).fetchone()[0]
    print(f"opening_cost rows to correct from backup: {to_change}")

    if not APPLY:
        print("DRY RUN — no writes. Re-run with --apply.")
        conn.close()
        return

    # Step 1: opening_cost = true pre-sync opening cost.
    # NOTE: bk also has a `products` table, so an unqualified `products.id` inside the
    # subquery binds to the INNER (bk) table and silently de-correlates — returning one
    # constant for every row. Alias the inner table (b) and qualify the outer (main.)
    # so the correlation is unambiguous.
    conn.execute(
        """UPDATE main.products
              SET opening_cost = (SELECT b.cost_price FROM bk.products b WHERE b.id = main.products.id)
            WHERE id IN (SELECT id FROM bk.products)"""
    )
    conn.commit()
    print("opening_cost backfilled from backup.")

    # Step 2: recompute every active product (cost_price <- live WACC).
    pids = [r[0] for r in conn.execute("SELECT id FROM products WHERE is_active=1 ORDER BY id")]
    for i, pid in enumerate(pids, 1):
        models.recalculate_product_wacc(pid, conn)
        if i % 400 == 0:
            print(f"  recomputed {i}/{len(pids)}")
    conn.commit()
    print(f"recomputed {len(pids)} active products.")
    conn.close()


if __name__ == '__main__':
    main()
