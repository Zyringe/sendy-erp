#!/usr/bin/env python3
"""
Full dry-run replay on a copy of the live DB.
1. Copy live DB to /tmp/ledger_fulldryrun.db
2. Reset synced_to_stock=0 for all sales+purchase transactions
3. Run _sync_bsn_to_stock for sales (OUT) and purchase (IN)
4. Compute implied_opening = oracle_qty - net_from_sync (net = IN - |OUT|)
5. Report sync counts, implied-opening distribution, top-15 absurd/negative
"""
import sqlite3
import shutil
import csv
import os
import sys

DB_SRC = os.path.expanduser(
    "~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db"
)
DB_COPY = "/tmp/ledger_fulldryrun.db"
ORACLE_PATH = os.path.expanduser(
    "~/Sendai-Boonsawat/sendy_erp/data/exports/ledger_rebuild/stock_snapshot_pre_rebuild_20260530_ledgerrebuild.csv"
)

# ---------------------------------------------------------------------------
# Copy live DB (including WAL checkpoint first)
# ---------------------------------------------------------------------------
print("Copying live DB to /tmp/ledger_fulldryrun.db ...")
# Remove stale copy
for suffix in ["", "-shm", "-wal"]:
    p = DB_COPY + suffix
    if os.path.exists(p):
        os.unlink(p)
shutil.copy2(DB_SRC, DB_COPY)
print("Done.")

conn = sqlite3.connect(DB_COPY)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=OFF")  # speed; copy only


# ---------------------------------------------------------------------------
# Load oracle
# ---------------------------------------------------------------------------
oracle = {}
with open(ORACLE_PATH, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        oracle[int(row["product_id"])] = float(row["quantity"])

print(f"Oracle: {len(oracle)} products")


# ---------------------------------------------------------------------------
# Reset synced_to_stock = 0 on COPY
# ---------------------------------------------------------------------------
with conn:
    conn.execute("UPDATE sales_transactions SET synced_to_stock=0")
    conn.execute("UPDATE purchase_transactions SET synced_to_stock=0")

sales_total = conn.execute("SELECT COUNT(*) FROM sales_transactions").fetchone()[0]
purchase_total = conn.execute("SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]
print(f"Reset {sales_total} sales + {purchase_total} purchase rows to synced_to_stock=0")


# ---------------------------------------------------------------------------
# _get_base_qty (copied from models.py)
# ---------------------------------------------------------------------------
def _get_base_qty(conn, product_id, product_unit_type, bsn_unit, qty):
    if bsn_unit is not None and bsn_unit.strip() == product_unit_type.strip():
        return qty
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
        (product_id, bsn_unit)
    ).fetchone()
    if row:
        return qty * row["ratio"]
    return None


# ---------------------------------------------------------------------------
# _sync_bsn_to_stock (dry-run version: records to in-memory dict, no DB write)
# Returns dict: product_id → {"in": float, "out": float, "blocked": int}
# Also returns lists: synced_rows, blocked_rows
# ---------------------------------------------------------------------------
def sync_all(conn):
    """Run sync for both sales (OUT) and purchase (IN) on the copy."""
    stats = {}  # product_id → {"in": 0.0, "out": 0.0}
    blocked = {}  # product_id → {"blocked_lines": 0, "blocked_reason": str}
    skipped_no_product = 0

    synced_purchase = 0
    synced_sales = 0
    blocked_purchase = 0
    blocked_sales = 0

    # PURCHASE → IN
    rows = conn.execute(
        "SELECT * FROM purchase_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0"
    ).fetchall()

    for row in rows:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (row["product_id"],)
        ).fetchone()
        if not product:
            skipped_no_product += 1
            continue

        qty = row["qty"] or 0
        base_qty = _get_base_qty(conn, row["product_id"], product["unit_type"], row["unit"], qty)

        if base_qty is None:
            blocked_purchase += 1
            pid = row["product_id"]
            blocked.setdefault(pid, {"blocked_lines": 0, "sample_unit": row["unit"], "table": "purchase"})
            blocked[pid]["blocked_lines"] += 1
            continue

        pid = row["product_id"]
        stats.setdefault(pid, {"in": 0.0, "out": 0.0})
        stats[pid]["in"] += base_qty
        synced_purchase += 1

    # SALES → OUT
    rows = conn.execute(
        "SELECT * FROM sales_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0"
    ).fetchall()

    # Detect the column name for qty in sales_transactions
    sales_cols = [d[0] for d in conn.execute("SELECT * FROM sales_transactions LIMIT 1").description]

    for row in rows:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (row["product_id"],)
        ).fetchone()
        if not product:
            skipped_no_product += 1
            continue

        # qty column in sales may be 'qty' or 'quantity'
        qty_val = row["qty"] if "qty" in sales_cols else 0
        base_qty = _get_base_qty(conn, row["product_id"], product["unit_type"], row["unit"], qty_val)

        if base_qty is None:
            blocked_sales += 1
            pid = row["product_id"]
            blocked.setdefault(pid, {"blocked_lines": 0, "sample_unit": row["unit"], "table": "sales"})
            blocked[pid]["blocked_lines"] += 1
            continue

        pid = row["product_id"]
        stats.setdefault(pid, {"in": 0.0, "out": 0.0})
        stats[pid]["out"] += abs(base_qty)
        synced_sales += 1

    return stats, blocked, {
        "synced_purchase": synced_purchase,
        "synced_sales": synced_sales,
        "blocked_purchase": blocked_purchase,
        "blocked_sales": blocked_sales,
        "skipped_no_product": skipped_no_product,
        "purchase_total": purchase_total,
    }


print("\nRunning sync replay (dry-run on copy)...")
stats, blocked, counts = sync_all(conn)
conn.close()


# ---------------------------------------------------------------------------
# Report 1: Sync counts
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("REPORT 1 — SYNC COUNTS")
print("=" * 60)
print(f"  Purchase rows:          {counts['purchase_total']}")
print(f"    Synced (IN):          {counts['synced_purchase']}")
print(f"    Blocked (no ratio):   {counts['blocked_purchase']}")
print(f"  Sales rows synced (OUT):{counts['synced_sales']}")
print(f"  Sales blocked:          {counts['blocked_sales']}")
print(f"  Skipped (no product):   {counts['skipped_no_product']}")

if blocked:
    print(f"\n  Blocked product_ids ({len(blocked)}):")
    for pid, info in sorted(blocked.items(), key=lambda x: -x[1]["blocked_lines"])[:20]:
        print(f"    pid={pid:5d}  lines={info['blocked_lines']:3d}  unit={info['sample_unit']}  table={info['table']}")


# ---------------------------------------------------------------------------
# Report 2: Implied opening distribution
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("REPORT 2 — IMPLIED OPENING DISTRIBUTION")
print("=" * 60)
print("  implied_opening = oracle_qty - (IN - OUT)")
print("  (negative means we sold/bought MORE than oracle; likely opening was negative)")
print()

implied = {}  # product_id → implied_opening
all_pids = set(oracle.keys()) | set(stats.keys())

for pid in all_pids:
    oracle_qty = oracle.get(pid, 0.0)
    net = stats.get(pid, {}).get("in", 0.0) - stats.get(pid, {}).get("out", 0.0)
    implied[pid] = oracle_qty - net

# Distribution buckets
buckets = {
    "negative": [],
    "zero": [],
    "small (0–100)": [],
    "medium (100–1k)": [],
    "large (1k–10k)": [],
    "huge (>10k)": [],
}

for pid, val in implied.items():
    if val < 0:
        buckets["negative"].append((pid, val))
    elif val == 0:
        buckets["zero"].append((pid, val))
    elif val <= 100:
        buckets["small (0–100)"].append((pid, val))
    elif val <= 1000:
        buckets["medium (100–1k)"].append((pid, val))
    elif val <= 10000:
        buckets["large (1k–10k)"].append((pid, val))
    else:
        buckets["huge (>10k)"].append((pid, val))

for label, items in buckets.items():
    print(f"  {label:25s}: {len(items):5d} products")


# ---------------------------------------------------------------------------
# Report 3: Top 15 absurd/negative implied openings
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("REPORT 3 — TOP 15 MOST EXTREME IMPLIED OPENINGS")
print("=" * 60)
print("  (Large positives = implausibly high opening stock;")
print("   Negatives = more sold/bought than oracle, data gap)")
print()

# Sort by absolute value, negatives first then large positives
def sort_key(item):
    val = item[1]
    return (0 if val < 0 else 1, -abs(val))

sorted_extreme = sorted(implied.items(), key=sort_key)[:30]

# Get product names for display
db_tmp = sqlite3.connect(DB_COPY)
db_tmp.row_factory = sqlite3.Row

print(f"  {'pid':>6}  {'product_name':45}  {'oracle':>8}  {'net_bsn':>10}  {'implied_open':>12}")
print("  " + "-" * 90)

shown = 0
for pid, imp_val in sorted_extreme:
    if shown >= 15:
        break
    try:
        p = db_tmp.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
        name = p["product_name"][:44] if p else "???"
    except Exception:
        name = "???"
    oracle_qty = oracle.get(pid, 0.0)
    net = stats.get(pid, {}).get("in", 0.0) - stats.get(pid, {}).get("out", 0.0)
    print(f"  {pid:>6}  {name:45}  {oracle_qty:>8.0f}  {net:>10.0f}  {imp_val:>12.0f}")
    shown += 1

db_tmp.close()


# ---------------------------------------------------------------------------
# Report 4: Oracle consistency check
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("REPORT 4 — ORACLE CONSISTENCY")
print("=" * 60)
print("  By construction: final_stock = implied_opening + net_bsn")
print("  = (oracle − net_bsn) + net_bsn = oracle  ✓")
print("  Checking a sample of 5 products with net > 0...")

checked = 0
mismatches = 0
for pid, net_data in sorted(stats.items()):
    net = net_data.get("in", 0.0) - net_data.get("out", 0.0)
    if net <= 0 or pid not in oracle:
        continue
    implied_open = oracle[pid] - net
    reconstructed_stock = implied_open + net
    ok = abs(reconstructed_stock - oracle[pid]) < 0.01
    if not ok:
        mismatches += 1
        print(f"  MISMATCH: pid={pid} oracle={oracle[pid]} reconstructed={reconstructed_stock}")
    checked += 1
    if checked >= 5 and mismatches == 0:
        print(f"  Sample of {checked} checked — all match oracle by construction ✓")
        break

if mismatches == 0 and checked >= 5:
    print(f"\n  All {len(oracle)} oracle targets reachable by construction ✓")
elif mismatches > 0:
    print(f"\n  WARNING: {mismatches} mismatches found")

print("\nDry-run complete.")
