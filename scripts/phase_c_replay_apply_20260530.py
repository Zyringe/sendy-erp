#!/usr/bin/env python3
"""
Phase C — Stock-Ledger Rebuild (LIVE apply, Option A).
2026-05-30

Steps:
  0. Pre-fixes: ลูกรีเวท กล่อง ratio 50→1000 (pids 977-982),
                pid 96 batches 19/22 purchase rows → remap to 106
  1. Snapshot marketplace columns (products.shopee_stock/lazada_stock, platform_skus.stock)
  2. SAVEPOINT rebuild
  3. Wipe transactions; reset synced_to_stock=0; DELETE stock_levels
     (triggers will rebuild stock_levels as transactions are inserted)
  4. _sync_bsn_to_stock for sales (OUT) and purchase (IN)
  5. Restore marketplace columns (sync deducted them)
  6. Restore manual extras (non-BSN transactions from the backup DB)
  7. Compute opening per product (Option A):
       net = SUM(quantity_change so far)
       target = oracle[pid]
       opening = target - net
       if opening >= 0: INSERT ADJUST @ 2024-01-03 note "ยอดยกมา (back-solved)"
       if opening < 0:  INSERT ADJUST @ 2024-01-03 qty=0  (no negative opening)
                        INSERT ADJUST @ 2026-05-30 qty=opening (negative reconcile)
  8. WACC recalc for products whose purchases changed
  9. VERIFY: every product stock_levels == oracle (tol=0); no negative stock
     PASS → RELEASE + COMMIT; FAIL → ROLLBACK + ABORT

Reversible actions CSV: sendy_erp/data/exports/ledger_rebuild/phase_c_reversible_20260530.csv
"""

import csv
import os
import sqlite3
import sys
from collections import defaultdict

ERP_ROOT = os.path.expanduser("~/Sendai-Boonsawat/sendy_erp")
APP_DIR = os.path.join(ERP_ROOT, "inventory_app")
DB_PATH = os.path.join(APP_DIR, "instance/inventory.db")
ORACLE_CSV = os.path.join(
    ERP_ROOT,
    "data/exports/ledger_rebuild/oracle_pre_rebuild_20260530_140048.csv",
)
REVERSIBLE_CSV = os.path.join(
    ERP_ROOT,
    "data/exports/ledger_rebuild/phase_c_reversible_20260530.csv",
)

# The pre-rebuild backup DB path (contains manual extras we want to restore)
BACKUP_DB = os.path.join(
    ERP_ROOT,
    "data/backups/inventory-pre-replay-apply-20260530_140048.db",
)

OPENING_TS = "2024-01-03 00:00:00"
OPENING_NOTE = "ยอดยกมา (back-solved)"
RECONCILE_TS = "2026-05-30 00:00:00"
RECONCILE_NOTE = "ปรับปรุงยอด (reconcile ledger)"

# Notes that identify BSN-generated OR prior-rebuild-generated transactions
# (exclude from manual extras — only preserve genuine human entries)
BSN_NOTE_PATTERNS = ("BSN ", "ประวัติ", "ยอดยกมา", "ปรับปรุงยอด", "ยอดต้นปี", "ยอดสูญหาย")

# ลูกรีเวท pids with wrong กล่อง ratio
RIVET_PIDS = [977, 978, 979, 980, 981, 982]  # 1907 already correct

sys.path.insert(0, APP_DIR)
from models import _sync_bsn_to_stock, recalculate_product_wacc  # noqa: E402


def load_oracle(path):
    oracle = {}
    with open(path, encoding="utf-8") as f:
        # File may or may not have header; detect by trying to parse first field as int
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            try:
                oracle[int(row[0])] = float(row[1])
            except ValueError:
                # Header row (e.g. "product_id,quantity") — skip
                continue
    return oracle


def note_is_bsn(note):
    if note is None:
        return False
    for pat in BSN_NOTE_PATTERNS:
        if note.startswith(pat):
            return True
    return False


def main():
    print("=" * 70)
    print("PHASE C — STOCK LEDGER REBUILD (LIVE APPLY, OPTION A)")
    print("=" * 70)
    print(f"DB:     {DB_PATH}")
    print(f"Oracle: {ORACLE_CSV}")
    print(f"Backup: {BACKUP_DB}")
    print()

    # --- Verify backup exists ---
    if not os.path.exists(BACKUP_DB):
        print("ERROR: backup DB not found — aborting.")
        return 1

    oracle = load_oracle(ORACLE_CSV)
    print(f"[oracle] {len(oracle)} products loaded")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    reversible_rows = []

    # =========================================================
    # STEP 0 — Pre-fixes (outside SAVEPOINT; idempotent)
    # =========================================================
    print("\n--- STEP 0: Pre-fixes ---")

    # 0a. ลูกรีเวท กล่อง ratio 50 → 1000 for pids 977-982
    with conn:
        for pid in RIVET_PIDS:
            row = conn.execute(
                "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit='กล่อง'",
                (pid,),
            ).fetchone()
            if row and row["ratio"] == 50.0:
                conn.execute(
                    "UPDATE unit_conversions SET ratio=1000.0 WHERE product_id=? AND bsn_unit='กล่อง'",
                    (pid,),
                )
                reversible_rows.append({
                    "step": "0a",
                    "action": "UPDATE unit_conversions",
                    "key": f"pid={pid} bsn_unit=กล่อง",
                    "old_val": "ratio=50.0",
                    "new_val": "ratio=1000.0",
                    "reverse_sql": f"UPDATE unit_conversions SET ratio=50.0 WHERE product_id={pid} AND bsn_unit='กล่อง';",
                })
                print(f"  [0a] pid={pid} กล่อง ratio 50→1000")
            elif row and row["ratio"] == 1000.0:
                print(f"  [0a] pid={pid} กล่อง already 1000 — skip")
            else:
                ratio_val = row["ratio"] if row else "MISSING"
                print(f"  [0a] pid={pid} กล่อง ratio={ratio_val} — unexpected, check manually")

    # 0b. Remap pid=96 rows (batches 19,22) to pid=106 for bsn_code=030บ4000
    with conn:
        affected = conn.execute(
            "SELECT id, batch_id, doc_no FROM purchase_transactions "
            "WHERE bsn_code='030บ4000' AND product_id=96",
        ).fetchall()
        if affected:
            conn.execute(
                "UPDATE purchase_transactions SET product_id=106 "
                "WHERE bsn_code='030บ4000' AND product_id=96",
            )
            for r in affected:
                reversible_rows.append({
                    "step": "0b",
                    "action": "UPDATE purchase_transactions",
                    "key": f"id={r['id']} batch_id={r['batch_id']} doc_no={r['doc_no']}",
                    "old_val": "product_id=96",
                    "new_val": "product_id=106",
                    "reverse_sql": f"UPDATE purchase_transactions SET product_id=96 WHERE id={r['id']};",
                })
            print(f"  [0b] Remapped {len(affected)} purchase_transactions rows: pid 96→106")
        else:
            print("  [0b] No pid=96 rows for 030บ4000 — already clean")

    # =========================================================
    # STEP 1 — Snapshot marketplace columns
    # =========================================================
    print("\n--- STEP 1: Snapshot marketplace columns ---")
    snap_products = conn.execute(
        "SELECT id, shopee_stock, lazada_stock FROM products"
    ).fetchall()
    snap_platform = conn.execute(
        "SELECT id, stock FROM platform_skus"
    ).fetchall()
    print(f"  Snapped {len(snap_products)} products, {len(snap_platform)} platform_skus")

    # =========================================================
    # STEP 2 — Load manual extras from backup BEFORE SAVEPOINT
    # =========================================================
    print("\n--- STEP 2: Load manual extras from backup ---")
    bak = sqlite3.connect(BACKUP_DB)
    bak.row_factory = sqlite3.Row

    valid_pids_bak = {r[0] for r in bak.execute("SELECT id FROM products").fetchall()}
    extras = bak.execute(
        """
        SELECT product_id, txn_type, quantity_change, unit_mode,
               reference_no, note, created_at
        FROM transactions
        WHERE note NOT LIKE 'BSN %'
          AND note NOT LIKE 'ประวัติ%'
          AND note NOT LIKE 'ยอดยกมา%'
          AND note NOT LIKE 'ปรับปรุงยอด%'
          AND note NOT LIKE 'ยอดต้นปี%'
          AND note NOT LIKE 'ยอดสูญหาย%'
        ORDER BY created_at, id
        """
    ).fetchall()
    bak.close()

    # Filter to valid product_ids
    extras_clean = [r for r in extras if r["product_id"] in valid_pids_bak]
    print(f"  Loaded {len(extras_clean)} manual extra rows from backup "
          f"(filtered from {len(extras)} total non-BSN rows)")

    # =========================================================
    # STEP 3 — SAVEPOINT + REBUILD
    # =========================================================
    print("\n--- STEP 3: SAVEPOINT rebuild ---")
    conn.execute("SAVEPOINT rebuild")

    try:
        # 3a. Wipe ledger
        conn.execute("DELETE FROM transactions")
        conn.execute("UPDATE sales_transactions SET synced_to_stock=0")
        conn.execute("UPDATE purchase_transactions SET synced_to_stock=0")
        conn.execute("DELETE FROM stock_levels")
        print("  [3a] Wiped transactions, reset synced flags, cleared stock_levels")

        # 3b. BSN sync (sales OUT, then purchase IN)
        _sync_bsn_to_stock(conn, "sales_transactions", "sales")
        _sync_bsn_to_stock(conn, "purchase_transactions", "purchase")
        n_bsn = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE note LIKE 'BSN %'"
        ).fetchone()[0]
        n_prathai = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE note LIKE 'ประวัติ%'"
        ).fetchone()[0]
        n_synced_sales = conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE synced_to_stock=1"
        ).fetchone()[0]
        n_synced_purchase = conn.execute(
            "SELECT COUNT(*) FROM purchase_transactions WHERE synced_to_stock=1"
        ).fetchone()[0]
        print(f"  [3b] BSN sync: {n_bsn} BSN txns + {n_prathai} ประวัติขาย pairs")
        print(f"       sales synced={n_synced_sales}, purchase synced={n_synced_purchase}")

        # 3c. Restore marketplace columns
        for r in snap_products:
            conn.execute(
                "UPDATE products SET shopee_stock=?, lazada_stock=? WHERE id=?",
                (r["shopee_stock"], r["lazada_stock"], r["id"]),
            )
        for r in snap_platform:
            conn.execute(
                "UPDATE platform_skus SET stock=? WHERE id=?",
                (r["stock"], r["id"]),
            )
        print(f"  [3c] Restored marketplace columns")

        # 3d. Re-insert manual extras
        valid_pids_live = {r[0] for r in conn.execute("SELECT id FROM products").fetchall()}
        n_extras_inserted = 0
        by_type = defaultdict(int)
        for r in extras_clean:
            if r["product_id"] not in valid_pids_live:
                continue
            conn.execute(
                """
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["product_id"], r["txn_type"], r["quantity_change"],
                    r["unit_mode"], r["reference_no"], r["note"], r["created_at"],
                ),
            )
            n_extras_inserted += 1
            by_type[r["txn_type"]] += 1
        print(f"  [3d] Restored {n_extras_inserted} manual extras "
              f"(ADJUST={by_type['ADJUST']}, IN={by_type['IN']}, OUT={by_type['OUT']})")

        # 3e. Compute opening per product (Option A)
        sum_qty = dict(
            conn.execute(
                "SELECT product_id, SUM(quantity_change) FROM transactions GROUP BY product_id"
            ).fetchall()
        )

        # all_pids must cover oracle keys AND any pid that has transactions
        # (pids with 0 oracle stock but still have sales go negative without this)
        all_pids = set(oracle.keys()) | set(sum_qty.keys())
        n_pos_opening = 0
        n_floored = 0
        reconcile_total_units = 0.0

        for pid in all_pids:
            net = sum_qty.get(pid, 0.0)
            target = oracle.get(pid, 0.0)
            opening = target - net

            if opening >= 0:
                conn.execute(
                    """
                    INSERT INTO transactions
                        (product_id, txn_type, quantity_change, unit_mode,
                         reference_no, note, created_at)
                    VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
                    """,
                    (pid, opening, OPENING_NOTE, OPENING_TS),
                )
                n_pos_opening += 1
            else:
                # Floor opening to 0; reconcile remainder as a dated negative ADJUST
                conn.execute(
                    """
                    INSERT INTO transactions
                        (product_id, txn_type, quantity_change, unit_mode,
                         reference_no, note, created_at)
                    VALUES (?, 'ADJUST', 0, 'unit', NULL, ?, ?)
                    """,
                    (pid, OPENING_NOTE, OPENING_TS),
                )
                conn.execute(
                    """
                    INSERT INTO transactions
                        (product_id, txn_type, quantity_change, unit_mode,
                         reference_no, note, created_at)
                    VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
                    """,
                    (pid, opening, RECONCILE_NOTE, RECONCILE_TS),
                )
                n_floored += 1
                reconcile_total_units += opening  # opening is negative

        print(f"  [3e] Openings: {n_pos_opening} positive, "
              f"{n_floored} floored-to-0+reconcile")
        print(f"       Total reconcile-adjustment magnitude: "
              f"{reconcile_total_units:,.1f} units (negative = ledger deficit)")

        # 3f. WACC recalc for all products that have purchase_transactions
        #     (scope to products touched in this rebuild)
        pids_with_purchases = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT product_id FROM purchase_transactions WHERE synced_to_stock=1"
            ).fetchall()
        }
        print(f"  [3f] Running WACC recalc for {len(pids_with_purchases)} products...")
        for pid in pids_with_purchases:
            recalculate_product_wacc(pid, conn)
        print(f"  [3f] WACC recalc complete")

        # =========================================================
        # STEP 4 — VERIFY
        # =========================================================
        print("\n--- STEP 4: Verification ---")
        final_stock = dict(
            conn.execute(
                "SELECT product_id, quantity FROM stock_levels"
            ).fetchall()
        )

        mismatches = []
        for pid in all_pids:
            final = final_stock.get(pid, 0.0)
            target = oracle.get(pid, 0.0)
            if abs(final - target) > 0.001:
                mismatches.append((pid, target, final))

        neg_stock = [
            (pid, qty)
            for pid, qty in final_stock.items()
            if qty < -0.001
        ]

        if mismatches or neg_stock:
            print(f"\n  VERIFICATION FAILED")
            if mismatches:
                print(f"  {len(mismatches)} stock mismatches:")
                for pid, tgt, got in mismatches[:20]:
                    print(f"    pid={pid:5d}  target={tgt:.2f}  got={got:.2f}  diff={got-tgt:.4f}")
            if neg_stock:
                print(f"\n  {len(neg_stock)} negative-stock products:")
                for pid, qty in neg_stock[:10]:
                    print(f"    pid={pid:5d}  qty={qty:.2f}")
            conn.execute("ROLLBACK TO SAVEPOINT rebuild")
            conn.execute("RELEASE SAVEPOINT rebuild")
            conn.commit()
            _write_reversible_csv(reversible_rows)
            print("\n  ROLLED BACK. Reversible CSV saved.")
            return 1

        print(f"  All {len(oracle)} products match oracle (tolerance 0.001) — PASS")
        print(f"  Negative stock count: {len(neg_stock)} — PASS")

        # Commit
        conn.execute("RELEASE SAVEPOINT rebuild")
        conn.commit()
        print("\n  COMMITTED.")

        n_final_txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        print(f"\n  Final transactions count: {n_final_txns}")

    except Exception as exc:
        print(f"\n  ERROR: {exc}")
        conn.execute("ROLLBACK TO SAVEPOINT rebuild")
        conn.execute("RELEASE SAVEPOINT rebuild")
        conn.commit()
        _write_reversible_csv(reversible_rows)
        raise
    finally:
        conn.close()

    _write_reversible_csv(reversible_rows)
    print(f"\n  Reversible CSV: {REVERSIBLE_CSV}")

    return 0


def _write_reversible_csv(rows):
    fieldnames = ["step", "action", "key", "old_val", "new_val", "reverse_sql"]
    with open(REVERSIBLE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
