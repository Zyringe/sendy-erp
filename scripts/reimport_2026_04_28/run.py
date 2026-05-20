"""
DEPRECATED: one-off from 2026-04-28. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

One-off re-import + stock baseline reset.

Inputs (read-only):
  /Volumes/ZYRINGE/express_data/ขาย_67-27.4.69.csv   — full sales 2024-01-03 → 2026-04-28
  /Volumes/ZYRINGE/express_data/ซื้อ_67-27.4.69.csv  — full purchase, same range

Goal:
  1. Re-import sales + purchases via inline UPSERT logic mirroring
     models.import_weekly() — preserves DB-only rows (SR + historical IV-without-suffix),
     fixes ~695 corrupt net values, adds ~3,613 missing purchase rows.
  2. Reset stock ledger such that FINAL stock_levels = CURRENT stock_levels
     (preserved unchanged). Method: snapshot current stock as `target` per
     product → wipe ledger → re-sync post-baseline BSN activity from new data
     → insert one ADJUST per product at 2026-03-03 23:59:59 with value
     `target - post_march_net_change`. Final = ADJUST + post_march_net = target.
     Rationale: user asserts current stock is physically correct; new purchase
     data must not inflate stock since old purchases were never properly synced.

Run modes:
  python run.py            # dry-run, rollback at end (DEFAULT)
  python run.py --commit   # commit changes

Backup taken at /Users/putty/.../instance/inventory.db.bak.20260428-112118
"""
import argparse
import json
import os
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Make inventory_app importable
ERP_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ERP_ROOT / "inventory_app"
sys.path.insert(0, str(APP_DIR))

from parse_weekly import parse_sales, parse_purchases  # noqa: E402

DB_PATH = str(APP_DIR / "instance" / "inventory.db")
SALES_FILE = "/Volumes/ZYRINGE/express_data/ขาย_67-27.4.69.csv"
PURCH_FILE = "/Volumes/ZYRINGE/express_data/ซื้อ_67-27.4.69.csv"

BASELINE_CUTOFF = "2026-03-04 00:00:00"   # transactions < this are pre-baseline
BASELINE_TIMESTAMP = "2026-03-03 23:59:59"  # ADJUST entry timestamp
BASELINE_NOTE = "Baseline (back-solved: target current stock − post-March net, reimport 2026-04-28)"

REPORT_DIR = Path(__file__).parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


# ── Snapshot ─────────────────────────────────────────────────────────────────

def snapshot(conn, label):
    """Capture key counts for before/after diff."""
    return {
        "label": label,
        "sales_rows": conn.execute("SELECT COUNT(*) FROM sales_transactions").fetchone()[0],
        "sales_corrupt_total_ne_net": conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE total != net AND total > 1 AND net < 100"
        ).fetchone()[0],
        "purchase_rows": conn.execute("SELECT COUNT(*) FROM purchase_transactions").fetchone()[0],
        "transactions_total": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "transactions_pre_baseline": conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE created_at < ?", (BASELINE_CUTOFF,)
        ).fetchone()[0],
        "transactions_at_baseline_ts": conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE created_at = ?", (BASELINE_TIMESTAMP,)
        ).fetchone()[0],
        "transactions_post_baseline": conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE created_at >= ?", (BASELINE_CUTOFF,)
        ).fetchone()[0],
        "transactions_bsn_synced": conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE note LIKE 'BSN %'"
        ).fetchone()[0],
        "stock_levels_rows": conn.execute("SELECT COUNT(*) FROM stock_levels").fetchone()[0],
        "stock_levels_total_qty": conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM stock_levels"
        ).fetchone()[0],
        "product_code_mapping_rows": conn.execute(
            "SELECT COUNT(*) FROM product_code_mapping"
        ).fetchone()[0],
    }


def snapshot_target_stock(conn):
    """Per-product CURRENT stock_levels — the value we must preserve after re-import."""
    rows = conn.execute(
        "SELECT product_id, quantity FROM stock_levels"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ── Phase 1: Data layer (UPSERT sales + purchases) ───────────────────────────

def upsert_table(conn, entries, file_type):
    """
    Inline copy of models.import_weekly()'s data-layer logic. Uses our conn so
    the whole script runs in one txn that we can rollback.

    Does NOT call _sync_bsn_to_stock — that happens in Phase 2 selectively.
    Does NOT recalculate WACC — it can be re-run later if needed.

    All inserted rows get synced_to_stock=0; Phase 2 sets the flag based on date.
    """
    assert file_type in ("sales", "purchase")
    table = "sales_transactions" if file_type == "sales" else "purchase_transactions"
    party_col = "customer" if file_type == "sales" else "supplier"
    party_code_col = "customer_code" if file_type == "sales" else "supplier_code"

    # Single batch_id for the whole reimport
    cur = conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) VALUES (?,0,0,?)",
        (f"reimport_2026_04_28_{file_type}", f"reimport-{file_type}"),
    )
    batch_id = cur.lastrowid

    inserted = overwritten_rows = skipped_ignored = 0
    new_bsn_codes = {}

    for e in entries:
        doc_no = e["doc_no"]
        doc_base = doc_no.rsplit("-", 1)[0] if "-" in doc_no else doc_no
        is_weekly = (doc_no == doc_base)

        # Mirror models.import_weekly() match logic
        if is_weekly:
            old_rows = conn.execute(
                f"SELECT id FROM {table}"
                f" WHERE doc_base = ? AND bsn_code = ? AND unit_price = ?",
                (doc_base, e["product_code_raw"], e["unit_price"]),
            ).fetchall()
        else:
            old_rows = conn.execute(
                f"SELECT id FROM {table}"
                f" WHERE bsn_code = ? AND (doc_no = ? OR doc_no = ?)",
                (e["product_code_raw"], doc_no, doc_base),
            ).fetchall()

        if old_rows:
            ids = [r["id"] for r in old_rows]
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(
                f"DELETE FROM {table} WHERE id IN ({placeholders})", ids
            )
            overwritten_rows += len(ids)

        # Resolve product_id via mapping
        mapping = conn.execute(
            "SELECT product_id, is_ignored FROM product_code_mapping WHERE bsn_code = ?",
            (e["product_code_raw"],),
        ).fetchone()
        product_id = mapping["product_id"] if mapping else None
        is_ignored = mapping["is_ignored"] if mapping else 0

        if is_ignored:
            skipped_ignored += 1
            continue

        if not mapping and e["product_code_raw"]:
            new_bsn_codes[e["product_code_raw"]] = e["product_name_raw"]

        conn.execute(
            f"""
            INSERT INTO {table}
                (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
                 {party_col}, {party_code_col}, qty, unit, unit_price,
                 vat_type, discount, total, net, synced_to_stock)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                batch_id, e["date_iso"], doc_no, doc_base, product_id,
                e["product_code_raw"], e["product_name_raw"],
                e["party"], e["party_code"],
                e["qty"], e["unit"], e["unit_price"],
                e["vat_type"], e["discount"], e["total"], e["net"],
            ),
        )
        inserted += 1

    # Register new BSN codes
    new_codes_inserted = 0
    for code, name in new_bsn_codes.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO product_code_mapping (bsn_code, bsn_name) VALUES (?, ?)",
            (code, name),
        )
        if cur.rowcount:
            new_codes_inserted += 1

    conn.execute(
        "UPDATE import_log SET rows_imported=?, rows_skipped=? WHERE id=?",
        (inserted, skipped_ignored, batch_id),
    )

    return {
        "file_type": file_type,
        "parsed": len(entries),
        "inserted": inserted,
        "overwritten_old_rows": overwritten_rows,
        "skipped_ignored": skipped_ignored,
        "new_bsn_codes_total": len(new_bsn_codes),
        "new_bsn_codes_inserted": new_codes_inserted,
    }


# ── Phase 2: Stock ledger reset ──────────────────────────────────────────────

def get_base_qty(conn, product_id, product_unit_type, bsn_unit, qty):
    """Mirror models._get_base_qty(). Returns float or None."""
    if bsn_unit is not None and bsn_unit.strip() == (product_unit_type or "").strip():
        return qty
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, bsn_unit),
    ).fetchone()
    if row:
        return qty * row["ratio"]
    return None


def sync_table_post_baseline(conn, table, file_type):
    """
    Mirror models._sync_bsn_to_stock() but only for rows date_iso >= '2026-03-04'
    AND synced_to_stock = 0 AND product_id IS NOT NULL.

    Skips rows with no unit_conversion (safe default — does NOT mark synced,
    so user can resolve later via /unit-conversions UI).

    Skips Phase 1 side-effects: no platform stock deduction, no history_import
    paired-IN handling. We're rebuilding the post-baseline ledger to mirror
    BSN-source reality. Platform stock + history_import logic only matters
    going forward, not for retroactive snapshot rebuilding.
    """
    txn_type = "IN" if file_type == "purchase" else "OUT"
    label = "ซื้อ" if file_type == "purchase" else "ขาย"

    rows = conn.execute(
        f"""
        SELECT * FROM {table}
        WHERE product_id IS NOT NULL
          AND synced_to_stock = 0
          AND date_iso >= '2026-03-04'
        """
    ).fetchall()

    inserted = skipped_no_product = skipped_no_ratio = skipped_zero_qty = 0
    by_month = defaultdict(int)

    for row in rows:
        product = conn.execute(
            "SELECT id, unit_type FROM products WHERE id = ?", (row["product_id"],)
        ).fetchone()
        if not product:
            conn.execute(f"UPDATE {table} SET synced_to_stock=1 WHERE id=?", (row["id"],))
            skipped_no_product += 1
            continue

        qty = row["qty"] or 0
        base_qty = get_base_qty(conn, row["product_id"], product["unit_type"], row["unit"], qty)
        if base_qty is None:
            skipped_no_ratio += 1
            continue
        if base_qty <= 0:
            conn.execute(f"UPDATE {table} SET synced_to_stock=1 WHERE id=?", (row["id"],))
            skipped_zero_qty += 1
            continue

        change = base_qty if txn_type == "IN" else -base_qty
        created_at = row["date_iso"] + " 00:00:00"
        conn.execute(
            """
            INSERT INTO transactions
                (product_id, txn_type, quantity_change, unit_mode,
                 reference_no, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["product_id"], txn_type, change, "unit",
             row["doc_no"], f"BSN {label}", created_at),
        )
        conn.execute(f"UPDATE {table} SET synced_to_stock=1 WHERE id=?", (row["id"],))
        inserted += 1
        by_month[row["date_iso"][:7]] += 1

    return {
        "file_type": file_type,
        "scanned": len(rows),
        "ledger_inserted": inserted,
        "skipped_no_product": skipped_no_product,
        "skipped_no_ratio": skipped_no_ratio,
        "skipped_zero_qty": skipped_zero_qty,
        "by_month": dict(by_month),
    }


def reset_ledger(conn, target):
    """
    Phase 2 (back-solve baseline so final stock = target):
      1. Wipe stock_levels + transactions (clean slate)
      2. Reset synced_to_stock flags (pre-baseline=1 skip, post-baseline=0 resync)
      3. Run BSN sync for post-baseline rows → ledger has only post-March IN/OUT
      4. For each product P in (target ∪ products with post-March activity):
           current_qty[P] = SUM(quantity_change) WHERE product_id=P (= post-March net)
           adjust[P] = target.get(P, 0) - current_qty[P]
         Insert ADJUST per product with adjust[P] != 0 at BASELINE_TIMESTAMP.
      5. Recompute stock_levels — guaranteed to equal target per product.
    """
    # 1. Wipe both
    conn.execute("DELETE FROM stock_levels")
    rows_deleted = conn.execute("DELETE FROM transactions").rowcount

    # 2. Selective re-sync flags
    for table in ("sales_transactions", "purchase_transactions"):
        conn.execute(
            f"UPDATE {table} SET synced_to_stock=1 WHERE date_iso < '2026-03-04'"
        )
        conn.execute(
            f"UPDATE {table} SET synced_to_stock=0"
            f" WHERE date_iso >= '2026-03-04' AND product_id IS NOT NULL"
        )

    # 3. Sync post-March BSN → ledger now has only post-March IN/OUT rows
    sync_sales = sync_table_post_baseline(conn, "sales_transactions", "sales")
    sync_purchase = sync_table_post_baseline(conn, "purchase_transactions", "purchase")

    # 4. Compute post-March net per product (from current ledger after sync)
    post_march_net = {
        r["product_id"]: r["q"]
        for r in conn.execute(
            "SELECT product_id, COALESCE(SUM(quantity_change), 0) AS q"
            " FROM transactions GROUP BY product_id"
        ).fetchall()
    }

    # Union of products needing reconciliation
    all_pids = set(target.keys()) | set(post_march_net.keys())

    adjust_inserted = 0
    adjust_skipped_zero = 0
    adjust_pos = adjust_neg = 0
    adjust_total = 0
    for pid in all_pids:
        desired = target.get(pid, 0)
        current = post_march_net.get(pid, 0)
        adjust = desired - current
        if adjust == 0:
            adjust_skipped_zero += 1
            continue
        conn.execute(
            """
            INSERT INTO transactions
                (product_id, txn_type, quantity_change, unit_mode,
                 reference_no, note, created_at)
            VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """,
            (pid, adjust, BASELINE_NOTE, BASELINE_TIMESTAMP),
        )
        adjust_inserted += 1
        adjust_total += adjust
        if adjust > 0:
            adjust_pos += 1
        else:
            adjust_neg += 1

    # 5. Recompute stock_levels (canonical DELETE+INSERT)
    conn.execute("DELETE FROM stock_levels")
    conn.execute(
        """
        INSERT INTO stock_levels (product_id, quantity)
        SELECT product_id, COALESCE(SUM(quantity_change), 0)
        FROM transactions
        GROUP BY product_id
        """
    )

    return {
        "old_ledger_deleted": rows_deleted,
        "target_products": len(target),
        "products_with_post_march_activity": len(post_march_net),
        "products_needing_reconciliation": len(all_pids),
        "adjust_inserted": adjust_inserted,
        "adjust_skipped_zero": adjust_skipped_zero,
        "adjust_positive": adjust_pos,
        "adjust_negative": adjust_neg,
        "adjust_total_qty": adjust_total,
        "sync_sales": sync_sales,
        "sync_purchase": sync_purchase,
    }


# ── Spot-check: stock_levels diff for random products ────────────────────────

def spot_check_stock(before_qtys, conn, n=5):
    """Return list of (product_id, name, before, after, diff) for n random products."""
    pids = list(before_qtys.keys())
    random.seed(20260428)
    sample = random.sample(pids, min(n, len(pids)))
    out = []
    for pid in sample:
        row = conn.execute(
            "SELECT p.product_name, COALESCE(s.quantity, 0) AS q"
            " FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id"
            " WHERE p.id = ?",
            (pid,),
        ).fetchone()
        before = before_qtys.get(pid, 0)
        after = row["q"] if row else 0
        name = row["product_name"] if row else "(?)"
        out.append((pid, name, before, after, after - before))
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Commit changes (default: dry-run, rollback at end)")
    args = ap.parse_args()

    mode = "COMMIT" if args.commit else "DRY-RUN (rollback at end)"
    print(f"=== Re-import + Baseline Reset [{mode}] ===")
    print(f"DB:        {DB_PATH}")
    print(f"Sales:     {SALES_FILE}")
    print(f"Purchase:  {PURCH_FILE}")
    print(f"Baseline:  {BASELINE_TIMESTAMP}  (cutoff <{BASELINE_CUTOFF})")
    print()

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # NOTE: sqlite3 auto-begins on first DML; we control commit/rollback below.

    try:
        # ── BEFORE snapshot
        before = snapshot(conn, "before")
        print("BEFORE:")
        print(json.dumps(before, indent=2, default=str))
        print()

        # ── Capture target stock = current stock_levels (must be preserved)
        target = snapshot_target_stock(conn)
        before_stock = dict(target)  # copy for spot-check
        print(
            f"Target stock snapshot: {len(target):,} products in stock_levels"
        )
        if target:
            qtys = list(target.values())
            print(f"  Products w/ qty > 0:   {sum(1 for q in qtys if q > 0):,}")
            print(f"  Products w/ qty < 0:   {sum(1 for q in qtys if q < 0):,}")
            print(f"  Products w/ qty == 0:  {sum(1 for q in qtys if q == 0):,}")
            print(f"  Total qty:             {sum(qtys):,}")
            print(f"  Min / Max:             {min(qtys)} / {max(qtys)}")
        print()

        target_file = REPORT_DIR / f"target_stock_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        target_file.write_text(json.dumps(target, indent=2))
        print(f"Target stock saved → {target_file}")
        print()

        # ── Phase 1: parse + upsert
        print("─── Phase 1: parse + upsert ───")
        sales_entries = parse_sales(SALES_FILE)
        print(f"Parsed {len(sales_entries):,} sales entries")
        purch_entries = parse_purchases(PURCH_FILE)
        print(f"Parsed {len(purch_entries):,} purchase entries")
        print()

        sales_metrics = upsert_table(conn, sales_entries, "sales")
        print(f"Sales upsert:    {json.dumps(sales_metrics, ensure_ascii=False)}")
        purch_metrics = upsert_table(conn, purch_entries, "purchase")
        print(f"Purchase upsert: {json.dumps(purch_metrics, ensure_ascii=False)}")
        print()

        # ── Phase 2: ledger reset (back-solve baseline)
        print("─── Phase 2: ledger reset (back-solve so final = target) ───")
        phase2 = reset_ledger(conn, target)
        print(f"Old ledger deleted:                  {phase2['old_ledger_deleted']:,}")
        print(f"Target products (current stock):     {phase2['target_products']:,}")
        print(f"Products w/ post-March activity:     {phase2['products_with_post_march_activity']:,}")
        print(f"Products needing reconciliation:     {phase2['products_needing_reconciliation']:,}")
        print(f"  ADJUST inserted:                   {phase2['adjust_inserted']:,}")
        print(f"    positive (target > post-March):  {phase2['adjust_positive']:,}")
        print(f"    negative (target < post-March):  {phase2['adjust_negative']:,}")
        print(f"  Skipped (no adjust needed):        {phase2['adjust_skipped_zero']:,}")
        print(f"  Total ADJUST qty:                  {phase2['adjust_total_qty']:,}")
        print()
        print(f"Sales sync:    {json.dumps(phase2['sync_sales'], ensure_ascii=False)}")
        print(f"Purchase sync: {json.dumps(phase2['sync_purchase'], ensure_ascii=False)}")
        print()

        # ── AFTER snapshot
        after = snapshot(conn, "after")
        print("AFTER:")
        print(json.dumps(after, indent=2, default=str))
        print()

        # ── Diff
        print("─── DIFF (after - before) ───")
        for k in before:
            if k == "label":
                continue
            b, a = before[k], after[k]
            if isinstance(b, (int, float)) and isinstance(a, (int, float)):
                diff = a - b
                arrow = "" if diff == 0 else (" ↑" if diff > 0 else " ↓")
                print(f"  {k:32s}  {b:>15,}  →  {a:>15,}   ({diff:+,}){arrow}")
        print()

        # ── Spot check
        print("─── Spot-check (5 random products with prior stock) ───")
        if before_stock:
            checks = spot_check_stock(before_stock, conn, n=5)
            print(f"  {'pid':>6}  {'before':>10}  {'after':>10}  {'diff':>10}  name")
            for pid, name, b, a, d in checks:
                print(f"  {pid:>6}  {b:>10,}  {a:>10,}  {d:>+10,}  {name[:60]}")
        else:
            print("  (no prior stock_levels rows to sample)")
        print()

        # ── Date-bucket histogram of post-baseline BSN syncs
        print("─── Post-baseline BSN sync histogram (by month) ───")
        merged = defaultdict(lambda: [0, 0])  # month → [sales, purchase]
        for m, n in phase2["sync_sales"]["by_month"].items():
            merged[m][0] += n
        for m, n in phase2["sync_purchase"]["by_month"].items():
            merged[m][1] += n
        print(f"  {'month':>8}  {'sales':>8}  {'purch':>8}  {'total':>8}")
        for m in sorted(merged.keys()):
            s, p = merged[m]
            print(f"  {m:>8}  {s:>8,}  {p:>8,}  {s+p:>8,}")
        print()

        # ── Commit / rollback
        if args.commit:
            conn.commit()
            print("✅ COMMITTED")
        else:
            conn.rollback()
            print("↩️  ROLLED BACK (dry-run)")
    except Exception:
        conn.rollback()
        print("❌ EXCEPTION — rolled back")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
