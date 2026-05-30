#!/usr/bin/env python3
"""
Phase C (De-dup + Re-Replay) — Stock-Ledger Rebuild.
2026-05-30

PROBLEM:
  Phase C replay double-counted purchases because purchase_transactions
  batch 37 (authoritative full-history load, ฿8,515,493.49) duplicates:
    - batch 19 (prior history import — full subset of batch 37)
    - weekly purchase batches that batch 37 already includes
  3,970 of 4,018 batch-37 rows have a twin in another batch (same doc_no+bsn_code).
  1 row (batch 9, doc RR6900057 / 556ล1140) is genuinely unique to another batch.

STEPS:
  STEP 1 — Fresh timestamped backup + integrity check (done externally before run).
  STEP 2 — DE-DUP purchase_transactions:
            DELETE batch_id != 37 WHERE (doc_no, bsn_code) IN batch-37.
            Record deleted rows to reversible CSV.
            Verify: batch-37 intact at 4018; no (doc_no+bsn_code) in >1 batch.
  STEP 3 — RE-RUN Phase C Option-A replay on de-duped data:
            SAVEPOINT; wipe transactions; reset synced_to_stock=0; clear stock_levels.
            _sync_bsn_to_stock sales+purchase; restore marketplace cols; restore 332 manual extras.
            Per-product opening = oracle − net:
              >=0 → ADJUST @2024-01-03 "ยอดยกมา (back-solved)"
              <0  → ADJUST @2024-01-03 qty=0  + ADJUST @2026-05-30 qty=opening "ปรับปรุงยอด (reconcile ledger)"
            WACC recalc for all products with synced purchases.
            VERIFY stock_levels == oracle (tol 0) + 0 negative stock.
            PASS → COMMIT; FAIL → ROLLBACK + abort.
  STEP 4 — REPORT improvement vs prior run.

Reversible CSV: sendy_erp/data/exports/ledger_rebuild/phase_c_dedup_reversible_20260530.csv
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
    "data/exports/ledger_rebuild/phase_c_dedup_reversible_20260530.csv",
)

# Manual-extras source: the DB snapshot taken before the original Phase C replay
# (contains the 332 genuine manual ADJUST/แถม entries before any rebuild)
BACKUP_DB = os.path.join(
    ERP_ROOT,
    "data/backups/inventory-pre-replay-apply-20260530_140048.db",
)

OPENING_TS = "2024-01-03 00:00:00"
OPENING_NOTE = "ยอดยกมา (back-solved)"
RECONCILE_TS = "2026-05-30 00:00:00"
RECONCILE_NOTE = "ปรับปรุงยอด (reconcile ledger)"

BSN_NOTE_PATTERNS = ("BSN ", "ประวัติ", "ยอดยกมา", "ปรับปรุงยอด", "ยอดต้นปี", "ยอดสูญหาย")

# ลูกรีเวท pids with wrong กล่อง ratio (step 0 pre-fix, idempotent)
RIVET_PIDS = [977, 978, 979, 980, 981, 982]

sys.path.insert(0, APP_DIR)
from models import _sync_bsn_to_stock, recalculate_product_wacc  # noqa: E402


def load_oracle(path):
    oracle = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            try:
                oracle[int(row[0])] = float(row[1])
            except ValueError:
                continue
    return oracle


def main():
    print("=" * 70)
    print("PHASE C DE-DUP + RE-REPLAY — STOCK LEDGER REBUILD")
    print("=" * 70)
    print(f"DB:     {DB_PATH}")
    print(f"Oracle: {ORACLE_CSV}")
    print(f"Backup: {BACKUP_DB}")
    print()

    if not os.path.exists(BACKUP_DB):
        print("ERROR: backup DB (manual-extras source) not found — aborting.")
        return 1

    oracle = load_oracle(ORACLE_CSV)
    print(f"[oracle] {len(oracle)} products loaded from CSV")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    reversible_rows = []

    # =========================================================
    # STEP 0 — Pre-fixes (outside SAVEPOINT; idempotent)
    # =========================================================
    print("\n--- STEP 0: Pre-fixes ---")

    # 0a. ลูกรีเวท กล่อง ratio 50 → 1000 for pids 977-982 (idempotent)
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

    # 0b. (OBSOLETE — removed 2026-05-30) The old hardcoded 96→106 remap for
    # bsn_code=030บ4000 is gone: Put confirmed via the mapping-conflict worksheet
    # that 030บ4000 = pid 96 (บานพับผีเสื้อ), NOT 106 (ไม่มีแหวน). The authoritative
    # product_code_mapping + the repoint now own this; a hardcoded remap here
    # would silently override that business decision. No-op.
    print("  [0b] skipped (obsolete — 030บ4000 owned by mapping, Put confirmed pid 96)")

    # =========================================================
    # STEP 2 — DE-DUP purchase_transactions (keep batch 37)
    # =========================================================
    print("\n--- STEP 2: De-dup purchase_transactions ---")

    # Snapshot pre-dedup counts
    pre_total = conn.execute("SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]
    pre_b37 = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions WHERE batch_id=37"
    ).fetchone()[0]
    print(f"  Pre-dedup total: {pre_total}  batch-37: {pre_b37}")

    # Identify rows to delete: non-batch-37 where (doc_no, bsn_code) is in batch-37
    rows_to_delete = conn.execute(
        """
        SELECT id, batch_id, doc_no, bsn_code, product_id, qty, unit,
               date_iso, supplier, net, synced_to_stock
        FROM purchase_transactions
        WHERE batch_id != 37
          AND (doc_no, bsn_code) IN (
              SELECT doc_no, bsn_code FROM purchase_transactions WHERE batch_id=37
          )
        ORDER BY batch_id, id
        """
    ).fetchall()

    print(f"  Rows to delete (overlap with batch-37): {len(rows_to_delete)}")

    # Save to reversible CSV before deleting
    for r in rows_to_delete:
        reversible_rows.append({
            "step": "2",
            "action": "DELETE purchase_transactions",
            "key": f"id={r['id']} batch_id={r['batch_id']} doc_no={r['doc_no']} bsn_code={r['bsn_code']}",
            "old_val": (f"product_id={r['product_id']} qty={r['qty']} unit={r['unit']} "
                        f"date_iso={r['date_iso']} net={r['net']} synced={r['synced_to_stock']}"),
            "new_val": "DELETED",
            "reverse_sql": (
                f"INSERT INTO purchase_transactions "
                f"(id, batch_id, doc_no, bsn_code, product_id, qty, unit, date_iso, supplier, net, synced_to_stock) "
                f"VALUES ({r['id']}, {r['batch_id']}, '{r['doc_no']}', '{r['bsn_code']}', "
                f"{r['product_id']}, {r['qty']}, '{r['unit']}', '{r['date_iso']}', "
                f"'{r['supplier']}', {r['net']}, {r['synced_to_stock']});"
            ),
        })

    # Write reversible CSV now (before destructive delete, so we have it even if something fails)
    _write_reversible_csv(reversible_rows)
    print(f"  Reversible CSV written: {len(reversible_rows)} rows so far")

    # Execute delete
    ids_to_delete = [r["id"] for r in rows_to_delete]

    # Delete in batches to avoid SQLite variable limit
    CHUNK = 500
    deleted_total = 0
    with conn:
        for i in range(0, len(ids_to_delete), CHUNK):
            chunk = ids_to_delete[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM purchase_transactions WHERE id IN ({placeholders})",
                chunk,
            )
            deleted_total += len(chunk)

    print(f"  Deleted: {deleted_total} rows")

    # Post-dedup verification
    post_total = conn.execute("SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]
    post_b37 = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions WHERE batch_id=37"
    ).fetchone()[0]
    print(f"  Post-dedup total: {post_total}  batch-37: {post_b37}")

    if post_b37 != 4018:
        print(f"  ERROR: batch-37 should still be 4018 but got {post_b37} — aborting.")
        _write_reversible_csv(reversible_rows)
        return 1

    # Check for any remaining (doc_no, bsn_code) duplicates across batches
    dup_check = conn.execute(
        """
        SELECT doc_no, bsn_code, COUNT(DISTINCT batch_id) as n_batches
        FROM purchase_transactions
        GROUP BY doc_no, bsn_code
        HAVING COUNT(DISTINCT batch_id) > 1
        """
    ).fetchall()
    if dup_check:
        print(f"  WARNING: {len(dup_check)} (doc_no, bsn_code) pairs still appear in >1 batch:")
        for r in dup_check[:10]:
            print(f"    doc_no={r['doc_no']} bsn_code={r['bsn_code']} batches={r['n_batches']}")
    else:
        print(f"  No (doc_no+bsn_code) duplicates across batches — PASS")

    # Breakdown of what remains by batch
    batch_counts = conn.execute(
        "SELECT batch_id, COUNT(*) as rows FROM purchase_transactions GROUP BY batch_id ORDER BY batch_id"
    ).fetchall()
    print(f"  Post-dedup batch distribution:")
    for r in batch_counts:
        print(f"    batch {r['batch_id']}: {r['rows']} rows")

    # =========================================================
    # STEP 1 — Snapshot marketplace columns (after dedup, before rebuild)
    # =========================================================
    print("\n--- STEP 1 (marketplace snapshot, before SAVEPOINT) ---")
    snap_products = conn.execute(
        "SELECT id, shopee_stock, lazada_stock FROM products"
    ).fetchall()
    snap_platform = conn.execute(
        "SELECT id, stock FROM platform_skus"
    ).fetchall()
    print(f"  Snapped {len(snap_products)} products, {len(snap_platform)} platform_skus")

    # =========================================================
    # Load manual extras from backup BEFORE SAVEPOINT
    # =========================================================
    print("\n--- Loading manual extras from backup ---")
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

    extras_clean = [r for r in extras if r["product_id"] in valid_pids_bak]
    print(f"  Loaded {len(extras_clean)} manual extras from backup "
          f"(from {len(extras)} non-BSN rows)")

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
                # Floor to 0; reconcile remainder as dated negative ADJUST
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

        print(f"  [3e] Openings: {n_pos_opening} positive, {n_floored} floored-to-0+reconcile")
        print(f"       Reconcile-adjustment magnitude: {reconcile_total_units:,.1f} units "
              f"(negative = ledger deficit)")

        # 3f. WACC recalc for products with synced purchases
        pids_with_purchases = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT product_id FROM purchase_transactions WHERE synced_to_stock=1"
            ).fetchall()
        }
        print(f"  [3f] WACC recalc for {len(pids_with_purchases)} products...")
        for pid in pids_with_purchases:
            recalculate_product_wacc(pid, conn)
        print(f"  [3f] WACC recalc complete")

        # =========================================================
        # STEP 4 — VERIFY
        # =========================================================
        print("\n--- STEP 4: Verification ---")
        final_stock = dict(
            conn.execute("SELECT product_id, quantity FROM stock_levels").fetchall()
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
            print("\n  ROLLED BACK. Reversible CSV saved (dedup still applied).")
            return 1

        print(f"  All {len(all_pids)} products match oracle (tol 0.001) — PASS")
        print(f"  Negative stock count: {len(neg_stock)} — PASS")

        # Commit
        conn.execute("RELEASE SAVEPOINT rebuild")
        conn.commit()
        print("\n  COMMITTED.")

        n_final_txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        print(f"\n  Final transactions count: {n_final_txns}")

        # =========================================================
        # STEP 4 REPORT — improvement vs prior run
        # =========================================================
        print("\n--- STEP 4 REPORT: Improvement vs prior Phase C run ---")
        print(f"  Purchase rows BEFORE dedup:  {pre_total}")
        print(f"  Purchase rows deleted (dup):  {deleted_total}")
        print(f"  Purchase rows AFTER dedup:   {post_total}")
        print(f"  Purchase synced (prior):      7985")
        print(f"  Purchase synced (this run):   {n_synced_purchase}")
        print(f"  Sales synced (unchanged):     {n_synced_sales}")
        print(f"  Reconcile-adj magnitude now:  {reconcile_total_units:,.1f} units")
        print(f"  Reconcile-adj magnitude prior: −503,712.0 units")
        print(f"  Positive openings: {n_pos_opening}  |  Floored: {n_floored}")
        print(f"  Stock == oracle: PASS  |  Negative stock: 0 — PASS")

    except Exception as exc:
        print(f"\n  ERROR during rebuild: {exc}")
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
    os.makedirs(os.path.dirname(REVERSIBLE_CSV), exist_ok=True)
    fieldnames = ["step", "action", "key", "old_val", "new_val", "reverse_sql"]
    with open(REVERSIBLE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
