"""Compare two Sendy DB files (prod vs local) table-by-table BEFORE a sync.

Read-only. Nothing is ever written. This is the "see before you push" tool
from references/sops/local-prod-db-sync.md — it shows the delta between a
downloaded prod DB and your local DB, and (most importantly) flags the
PROTECTED team-on-prod tables you must NEVER overwrite from local.

Usage:
    python scripts/compare_local_prod.py <prod.db> <local.db>

Output groups:
  MASTER       local is the source of truth; the master-only upload pushes
               these. Shows the row-count delta (what an upload would change).
  FILE-IMPORT  source of truth is the import FILE. For the high-value tables
               we diff by business key: local-only / prod-only / differing.
               prod-only = rows you'd MISS if you only push local — fix by
               re-importing the right file on prod, never by file-replace.
  PROTECTED    team-typed on prod, NO source file (call logs, deposit
               reconcile, manual stock ADJUST). NEVER push local over these.
               Shown loudly with prod-only counts.

Exit code is always 0 — it's a report.

NOTE: MASTER_TABLES mirrors app.py::_MASTER_TABLES. Keep in sync if that
changes (this script intentionally does NOT import app.py to avoid Flask
side-effects). PROTECTED_TABLES must grow whenever a new team-on-prod write
feature ships.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# ── Table classification ─────────────────────────────────────────────────────

# Mirror of app.py::_MASTER_TABLES (2026-06-16). Keep in sync.
MASTER_TABLES = (
    'applied_migrations',
    'products', 'product_families', 'product_images',
    'product_locations', 'product_barcodes', 'product_price_tiers',
    'brands', 'categories', 'color_finish_codes',
    'product_code_mapping', 'unit_conversions',
    'conversion_formulas', 'conversion_formula_inputs',
    'regions', 'customer_regions', 'expense_categories', 'promotions',
    'platform_skus', 'platform_products', 'ecommerce_listings', 'listing_bundles',
    'po_sequences', 'salespersons',
    'commission_tiers', 'commission_assignments', 'commission_overrides',
    'ar_writeoffs',
    'suppliers', 'supplier_catalogue_items', 'supplier_catalogue_versions',
    'supplier_catalogue_price_history', 'supplier_product_mapping',
)

# File-import tables with a STABLE business key (stable across DBs, unlike the
# autoincrement id / per-run batch_id). Diffed key-by-key. Signal columns are
# the fields whose change between two rows of the same key counts as "differing"
# (floats rounded to kill IEEE-754 display noise — see verification-discipline).
KEYED_TABLES = {
    'marketplace_orders':    (('platform', 'order_sn'),
                              ('status', 'payout', 'actual_payout', 'settled_at')),
    'sales_transactions':    (('doc_no', 'bsn_code'),
                              ('qty', 'unit_price', 'net', 'product_id')),
    'purchase_transactions': (('doc_no', 'bsn_code', 'line_seq'),
                              ('qty', 'unit_price', 'net', 'product_id')),
}

# File-import tables without a cross-DB-stable key (derived ledger, per-run
# batch ids). Row-count only + a note; not key-diffable meaningfully.
OTHER_FILE_IMPORT = (
    'marketplace_order_items', 'transactions', 'stock_levels',
    'express_sales', 'express_ar_outstanding', 'express_ap_outstanding',
    'express_credit_notes', 'express_payments_in', 'express_payments_out',
)

# Team-typed on PROD, no source file. NEVER push local over these.
PROTECTED_TABLES = (
    'customer_call_log', 'customer_crm', 'payout_batches', 'audit_log', 'users',
)


# ── DB helpers ───────────────────────────────────────────────────────────────

def connect_ro(path):
    """Open read-only so we can never accidentally write a snapshot."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: file not found: {path}")
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def count_rows(conn, table, where=""):
    if not table_exists(conn, table):
        return None
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql).fetchone()[0]


def _fp(values):
    """Fingerprint a row's signal columns; round floats to 2dp."""
    out = []
    for v in values:
        if isinstance(v, float):
            out.append(round(v, 2))
        else:
            out.append(v)
    return tuple(out)


def keyed_map(conn, table, key_cols, signal_cols):
    """Return {key_tuple: signal_fingerprint} for a keyed table, or None."""
    if not table_exists(conn, table):
        return None
    cols = list(key_cols) + list(signal_cols)
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    n_key = len(key_cols)
    return {tuple(r[:n_key]): _fp(r[n_key:]) for r in rows}


def diff_keyed(prod_map, local_map):
    """Return (local_only, prod_only, differing, shared) counts."""
    pk, lk = set(prod_map), set(local_map)
    shared = pk & lk
    differing = sum(1 for k in shared if prod_map[k] != local_map[k])
    return len(lk - pk), len(pk - lk), differing, len(shared)


# ── Report ───────────────────────────────────────────────────────────────────

def run(prod_path, local_path, out=print):
    prod = connect_ro(prod_path)
    local = connect_ro(local_path)

    out("=" * 70)
    out(f"  Sendy DB compare   prod={prod_path}")
    out(f"                     local={local_path}")
    out("=" * 70)

    # MASTER ------------------------------------------------------------------
    out("\n## MASTER  (local = source of truth; pushed by master-only upload)")
    out(f"  {'table':<34}{'prod':>8}{'local':>8}  delta")
    for t in MASTER_TABLES:
        p, l = count_rows(prod, t), count_rows(local, t)
        if p is None and l is None:
            continue
        p, l = p or 0, l or 0
        flag = "" if p == l else f"  Δ {l - p:+d}"
        out(f"  {t:<34}{p:>8}{l:>8}{flag}")

    # FILE-IMPORT (keyed) -----------------------------------------------------
    out("\n## FILE-IMPORT  (source of truth = the import file; push by RE-IMPORT)")
    out(f"  {'table (by business key)':<30}{'local-only':>11}{'prod-only':>10}{'differ':>8}{'shared':>8}")
    for t, (key_cols, signal_cols) in KEYED_TABLES.items():
        pm = keyed_map(prod, t, key_cols, signal_cols)
        lm = keyed_map(local, t, key_cols, signal_cols)
        if pm is None and lm is None:
            continue
        lo, po, diff, sh = diff_keyed(pm or {}, lm or {})
        warn = "  <- prod-only rows you'd MISS on local-only push" if po else ""
        out(f"  {t:<30}{lo:>11}{po:>10}{diff:>8}{sh:>8}{warn}")

    out("\n   (other file-import tables — row counts only; derived/per-run keys)")
    for t in OTHER_FILE_IMPORT:
        p, l = count_rows(prod, t), count_rows(local, t)
        if p is None and l is None:
            continue
        out(f"   {t:<33}{p or 0:>8}{l or 0:>8}")

    # PROTECTED ---------------------------------------------------------------
    out("\n" + "!" * 70)
    out("!! PROTECTED — team-typed on PROD, no source file. NEVER push local over these.")
    out("!" * 70)
    any_risk = False
    for t in PROTECTED_TABLES:
        p, l = count_rows(prod, t), count_rows(local, t)
        if p is None and l is None:
            continue
        p, l = p or 0, l or 0
        prod_only = max(p - l, 0)
        mark = ""
        if prod_only > 0:
            mark = f"  *** prod has {prod_only} row(s) local lacks — a full-replace would ERASE them"
            any_risk = True
        out(f"  {t:<34}{'prod=':>1}{p:<7}{'local=':>1}{l:<7}{mark}")

    # special-case columns that live ON file-import tables but are team-written
    pb = count_rows(prod, 'marketplace_orders', "payout_batch_id IS NOT NULL")
    lb = count_rows(local, 'marketplace_orders', "payout_batch_id IS NOT NULL")
    if pb is not None:
        mark = ""
        if (pb or 0) > (lb or 0):
            mark = "  *** team bank-deposit links on prod; a marketplace file-replace would drop them"
            any_risk = True
        out(f"  {'marketplace_orders.payout_batch_id':<34}{'prod=':>1}{pb or 0:<7}{'local=':>1}{lb or 0:<7}{mark}")

    adj = "txn_type='ADJUST' AND COALESCE(note,'') NOT LIKE 'BSN%'"
    pa = count_rows(prod, 'transactions', adj)
    la = count_rows(local, 'transactions', adj)
    if pa is not None:
        mark = ""
        if (pa or 0) > (la or 0):
            mark = "  *** manual stock adjustments on prod (no source file) — re-import won't recreate"
            any_risk = True
        out(f"  {'transactions (manual ADJUST)':<34}{'prod=':>1}{pa or 0:<7}{'local=':>1}{la or 0:<7}{mark}")

    if not any_risk:
        out("  (no protected prod-only rows detected this comparison)")

    out("\n" + "=" * 70)
    out("  Reminder:  prod->local = full .db file replace (local is disposable).")
    out("             local->prod = master-only upload + RE-IMPORT files. NEVER replace.")
    out("=" * 70)

    prod.close()
    local.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare prod vs local Sendy DBs (read-only).")
    ap.add_argument("prod_db", help="path to the downloaded prod inventory.db")
    ap.add_argument("local_db", help="path to the local inventory.db")
    args = ap.parse_args(argv)
    run(args.prod_db, args.local_db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
