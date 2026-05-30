#!/usr/bin/env python3
"""
Apply Put's filled worksheets (WS1 + WS2) to the live DB.
Writes product_code_mapping, unit_conversions, backfills batch-37 product_id,
applies 806/807 unit_type change + ×12 stock scaling.
Records everything to a reversible CSV.

Run from workspace root or sendy_erp/.
Requires confirmation before any write.
"""
import sqlite3
import csv
import datetime
import os
import sys

DB_PATH = os.path.expanduser(
    "~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db"
)
OUT_DIR = os.path.expanduser(
    "~/Sendai-Boonsawat/sendy_erp/data/exports/ledger_rebuild"
)
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_PATH = os.path.join(OUT_DIR, f"applied_worksheet_{TS}.csv")

os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Data structures
# Each change is: (category, action, table, key_desc, old_val, new_val, sql, params)
# ---------------------------------------------------------------------------
changes = []  # list of dicts


def plan_insert_unit_conversion(product_id, bsn_unit, ratio, reason, *, skip_if_exists=False):
    """Plan an INSERT OR IGNORE into unit_conversions."""
    changes.append({
        "category": reason,
        "action": "INSERT_UC",
        "table": "unit_conversions",
        "key": f"pid={product_id} bsn_unit={bsn_unit}",
        "old_val": "MISSING",
        "new_val": f"ratio={ratio}",
        "sql": "INSERT OR IGNORE INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
        "params": (product_id, bsn_unit, ratio),
        "skip_if_exists": skip_if_exists,
    })


def plan_insert_mapping(bsn_code, product_id, reason):
    """Plan an INSERT OR IGNORE into product_code_mapping."""
    changes.append({
        "category": reason,
        "action": "INSERT_MAPPING",
        "table": "product_code_mapping",
        "key": f"bsn_code={bsn_code}",
        "old_val": "MISSING",
        "new_val": f"product_id={product_id}",
        "sql": "INSERT OR IGNORE INTO product_code_mapping (bsn_code, product_id) VALUES (?,?)",
        "params": (bsn_code, product_id),
    })


def plan_update(table, key_desc, old_val, new_val, sql, params, reason):
    changes.append({
        "category": reason,
        "action": "UPDATE",
        "table": table,
        "key": key_desc,
        "old_val": str(old_val),
        "new_val": str(new_val),
        "sql": sql,
        "params": params,
    })


# ===========================================================================
# WS1 — 6 bag codes
# Mappings already exist; batch-37 product_id = NULL (needs backfill)
# unit_conversions: bsn_unit='กก', ratio=10 (MISSING; existing entries use กิโลกรัม)
# ===========================================================================
BAG_CODES = {
    "168ถ0040": 1374,
    "169ถ0030": 1372,
    "168ถ0030": 1373,
    "169ถ0050": 1369,
    "169ถ0010": 1370,
    "169ถ0020": 1371,
}

for bsn_code, pid in BAG_CODES.items():
    # unit_conversion: กก→ratio=10
    plan_insert_unit_conversion(pid, "กก", 10.0, "WS1-bag-กก-ratio10")
    # backfill batch-37 product_id (done in bulk UPDATE below)

# Backfill batch-37 product_id for bag codes (6 codes, 14 rows)
for bsn_code, pid in BAG_CODES.items():
    plan_update(
        "purchase_transactions",
        f"batch_id=37 bsn_code={bsn_code}",
        "NULL",
        pid,
        "UPDATE purchase_transactions SET product_id=? WHERE batch_id=37 AND bsn_code=? AND product_id IS NULL",
        (pid, bsn_code),
        "WS1-bag-backfill-product_id",
    )

# ===========================================================================
# Backfill batch-37 product_id for OTHER codes with NULL product_id
# (637ด3214→2008, 637ด3216→2009, 914ป1138→2010, 914ป1140→2011)
# These have mappings already. Raw units: หล (ดจ.ปูน), อน (ประแจ)
# ===========================================================================
OTHER_NULL_CODES = {
    "637ด3214": 2008,
    "637ด3216": 2009,
    "914ป1138": 2010,
    "914ป1140": 2011,
}
for bsn_code, pid in OTHER_NULL_CODES.items():
    plan_update(
        "purchase_transactions",
        f"batch_id=37 bsn_code={bsn_code}",
        "NULL",
        pid,
        "UPDATE purchase_transactions SET product_id=? WHERE batch_id=37 AND bsn_code=? AND product_id IS NULL",
        (pid, bsn_code),
        "backfill-product_id-other-mapped-codes",
    )

# ===========================================================================
# Add unit_conversions for ดจ.ปูน 2008/2009: raw=หล, ratio=12
# (existing: โหล=12, but batch-37 raw is หล)
# ===========================================================================
plan_insert_unit_conversion(2008, "หล", 12.0, "WS2-ดจ.ปูน-หล-ratio12")
plan_insert_unit_conversion(2009, "หล", 12.0, "WS2-ดจ.ปูน-หล-ratio12")

# Add unit_conversions for ประแจ 2010/2011: raw=อน, ratio=1 (อน≈อัน)
plan_insert_unit_conversion(2010, "อน", 1.0, "WS1-ref-ประแจ-อน-ratio1")
plan_insert_unit_conversion(2011, "อน", 1.0, "WS1-ref-ประแจ-อน-ratio1")

# ===========================================================================
# WS2 — unit_conversions (all rows except 96, 98, 806, 807)
# Key: use RAW unit from batch-37, not decoded name from WS2
#
# Mapping: WS2 decoded → batch-37 raw
#   ลัง → ลง
#   กล่อง → กล
#   ชุด → ชด
#   แผ่น → ผน
#   ปื้น → ปน
#   กิโลกรัม (กส) → กส
#   กิโลกรัม → กก  (for products where unit_type != กิโลกรัม)
#   แพ็ค → แพ
#   ตัว → ตว  (for products where unit_type=แผง/ตัว buying in ตัว)
# ===========================================================================

# ตะปูคอนกรีต กล่องเล็ก group — buy=กล่อง (raw=กล), ratio=1
TAPUU_PIDS = [664, 665, 666, 667, 668, 669, 670, 672, 673, 674]
for pid in TAPUU_PIDS:
    plan_insert_unit_conversion(pid, "กล", 1.0, "WS2-ตะปู-กล-ratio1")

# โซดาไฟ 1723 — unit_type=กก, buy=ลัง (raw=ลง), ratio=20
plan_insert_unit_conversion(1723, "ลง", 20.0, "WS2-โซดาไฟ-ลง-ratio20")

# ผงปูนยิบซั่ม 1438 — unit_type=ตัว, buy=ลัง (raw=ลง), ratio=1
plan_insert_unit_conversion(1438, "ลง", 1.0, "WS2-ผงปูน-ลง-ratio1")

# ตะปูคอนกรีต META 25x2.0 สีขาว 674 already above
# ใบมีดคัตเตอร์ 1025 — unit_type=ตัว, buy=กล่อง (raw=กล), ratio=1
plan_insert_unit_conversion(1025, "กล", 1.0, "WS2-ใบมีด-กล-ratio1")

# บานพับ ใบโพธิ์ทอง 112,113,114,115,116,117 — unit_type=แผง, buy=ชุด (raw=ชด), ratio=1
for pid in [112, 113, 114, 115, 116, 117]:
    plan_insert_unit_conversion(pid, "ชด", 1.0, "WS2-บานพับใบโพธิ์-ชด-ratio1")

# ใบเลื่อยคันธนู 720 — unit_type=ใบ, buy=ปื้น (raw=ปน), ratio=1
plan_insert_unit_conversion(720, "ปน", 1.0, "WS2-ใบเลื่อย-ปน-ratio1")

# บานพับแหวนท.ล 1393 — unit_type=แผง, buy=ตัว (raw=ตว), ratio=1
plan_insert_unit_conversion(1393, "ตว", 1.0, "WS2-1393-ตว-ratio1")

# ลูกบิดหัวกลม 777 — unit_type=แผง, buy=ชุด (raw=ชด), ratio=1
plan_insert_unit_conversion(777, "ชด", 1.0, "WS2-ลูกบิด-ชด-ratio1")

# จารบี group — unit_type=กระป๋อง, buy=ลัง (raw=ลง), various ratios
JARABI_LNG = {
    612: 1.0,   # TOA 306-5kg  ★=1
    613: 40.0,  # TOA 406-0.5kg ★=40
    614: 12.0,  # TOA 406-1kg ★=12
    617: 12.0,  # เทรน 2kg ★=12
    618: 4.0,   # เทรน 5kg ★=4
}
for pid, ratio in JARABI_LNG.items():
    plan_insert_unit_conversion(pid, "ลง", ratio, f"WS2-จารบี-ลง-ratio{int(ratio)}")

# ลวดเชื่อม 1519 — unit_type=ตัว, buy=ลัง (raw=ลง), ratio=1
plan_insert_unit_conversion(1519, "ลง", 1.0, "WS2-ลวดเชื่อม-ลง-ratio1")

# ผ้าเทปยิปซั่ม 1451 — unit_type=ตัว, buy=แพ็ค (raw=แพ), ratio=1
plan_insert_unit_conversion(1451, "แพ", 1.0, "WS2-ผ้าเทป-แพ-ratio1")

# บานพับสแตนเลส 3.5in ตัว pid 105 — batch-37 raw=ตว, ratio=1
plan_insert_unit_conversion(105, "ตว", 1.0, "WS2-บานพับ105-ตว-ratio1")

# เกลียวเหล็ก 1908 — unit_type=ตัว, buy=กล่อง (raw=กล), ratio=1
plan_insert_unit_conversion(1908, "กล", 1.0, "WS2-เกลียว-กล-ratio1")

# ตะปูตอกสังกะสี 1341 — unit_type=ตัว, buy=ลัง (raw=ลง), ratio=1
plan_insert_unit_conversion(1341, "ลง", 1.0, "WS2-ตะปูตอก-ลง-ratio1")

# ดจ.ปูน 2008/2009 already added above (หล=12)

# บานพับ ใบโพธิ์ทอง 112-117 already above

# จารบี เทรน 5kg 618 already above

# ทราย Golden Lion 343,345,346 — unit_type=ใบ, buy=แผ่น (raw=ผน), ratio=1
for pid in [343, 345, 346]:
    plan_insert_unit_conversion(pid, "ผน", 1.0, "WS2-ทราย-ผน-ratio1")

# จารบี เทรน 2kg 617 already above

# พุกพลาสติก — unit_type=กิโลกรัม, buy=กิโลกรัม(กส) (raw=กส), ratio=20
# 912: พุกพลาสติก Sendai #6
# 415: พุกพลาสติก #8
# 1887: พุกพลาสติก #5
for pid in [912, 415, 1887]:
    plan_insert_unit_conversion(pid, "กส", 20.0, f"WS2-พุก-กส-ratio20")

# อุปกรณ์ปรับระดับ 1635 — unit_type=ตัว, buy=แพ็ค (raw=แพ), ratio=1
plan_insert_unit_conversion(1635, "แพ", 1.0, "WS2-ปรับระดับ-แพ-ratio1")

# จารบี Trane 1225 — unit_type=ตัว, buy=ลัง (raw=ลง), ratio=30
plan_insert_unit_conversion(1225, "ลง", 30.0, "WS2-จารบีTrane-ลง-ratio30")

# จารบี TOA 406-1kg 614 already above

# จารบี จระเข้ 1223,1224 — unit_type=ตัว, buy=ลัง (raw=ลง)
plan_insert_unit_conversion(1223, "ลง", 40.0, "WS2-จารบีจระเข้0.5-ลง-ratio40")
plan_insert_unit_conversion(1224, "ลง", 12.0, "WS2-จารบีจระเข้2-ลง-ratio12")

# เทปใสแกนใหญ่ 1935 — unit_type=ตัว, buy=กล่อง (raw=กล), ratio=1
plan_insert_unit_conversion(1935, "กล", 1.0, "WS2-เทปใส-กล-ratio1")

# ตะปูคอนกรีตผอม 1340 — unit_type=ตัว, buy=กิโลกรัม (raw=กก), ratio=1
plan_insert_unit_conversion(1340, "กก", 1.0, "WS2-ตะปูผอม-กก-ratio1")

# ตะปูยิงฝ้า 974 — unit_type=ตัว, buy=กล่อง (raw=กล), ratio=1
plan_insert_unit_conversion(974, "กล", 1.0, "WS2-ตะปูยิงฝ้า-กล-ratio1")

# ตะปูคอนกรีต #9-1.5 1339 — unit_type=ตัว, buy=กิโลกรัม (raw=กก), ratio=1
plan_insert_unit_conversion(1339, "กก", 1.0, "WS2-ตะปู9-กก-ratio1")

# ลูกรีเวท 977,978,979,980,981,982,1907 — unit_type=ดอก, buy=กล่อง (raw=กล), ratio=1000
# (existing กล่อง=50 for 977-982 is wrong per WS2, but we only add the raw กล key)
for pid in [977, 978, 979, 980, 981, 982, 1907]:
    plan_insert_unit_conversion(pid, "กล", 1000.0, "WS2-ลูกรีเวท-กล-ratio1000")

# ผงปูนปลาสเตอร์ 1437 — unit_type=ตัว, buy=ลัง (raw=ลง), ratio=1
plan_insert_unit_conversion(1437, "ลง", 1.0, "WS2-ผงปูนปลาสเตอร์-ลง-ratio1")

# เลื่อยลันดา 1658 — unit_type=ตัว, buy=ปื้น (raw=ปน), ratio=1
plan_insert_unit_conversion(1658, "ปน", 1.0, "WS2-เลื่อยลันดา-ปน-ratio1")

# เสื้อโปโล 1661 — unit_type=ตัว, buy=กล่อง (raw=กล), ratio=1
plan_insert_unit_conversion(1661, "กล", 1.0, "WS2-เสื้อโปโล-กล-ratio1")

# ลูกกลิ้ง 785 — unit_type=อัน, buy=กิโลกรัม (raw=กก), ratio=1
# (existing: กก NOT in unit_conversions; has กิโลกรัม=1, อน=1, อัน=1)
plan_insert_unit_conversion(785, "กก", 1.0, "WS2-ลูกกลิ้ง-กก-ratio1")

# ผงปูนยิบซั่ม นกอินทรีย์ 1438 — already added above (ลง=1)

# ===========================================================================
# SPECIAL: pid 96 and 98 remap
# 96: bsn_code=030บ4000, batch-37 rows product_id=96→106
#     mapping: UPDATE product_code_mapping set product_id=106 for bsn_code=030บ4000
#     sibling 106 unit_type=ตัว; batch-37 raw=ตว → add unit_conversion (106, ตว, 1.0)
# 98: batch-37 rows product_id=98→105
#     mapping already has 030บ5135→105; just update batch-37 rows
#     sibling 105 unit_type=ตัว; batch-37 raw=ตว → add unit_conversion (105, ตว, 1.0)
#     (already added above)
# ===========================================================================
# Remap 030บ4000 in product_code_mapping from 96 to 106
plan_update(
    "product_code_mapping",
    "bsn_code=030บ4000",
    "product_id=96",
    "product_id=106",
    "UPDATE product_code_mapping SET product_id=106 WHERE bsn_code='030บ4000'",
    (),
    "SPECIAL-remap-96→106-mapping",
)

# Remap batch-37 purchase_transactions: product_id 96→106 for bsn_code=030บ4000
plan_update(
    "purchase_transactions",
    "batch_id=37 bsn_code=030บ4000 product_id=96",
    "product_id=96",
    "product_id=106",
    "UPDATE purchase_transactions SET product_id=106 WHERE batch_id=37 AND bsn_code='030บ4000' AND product_id=96",
    (),
    "SPECIAL-remap-96→106-batch37-rows",
)

# Add unit_conversion for pid 106: raw=ตว, ratio=1.0
plan_insert_unit_conversion(106, "ตว", 1.0, "SPECIAL-106-ตว-ratio1")

# Remap batch-37 purchase_transactions: product_id 98→105 for bsn_code=030บ5135
plan_update(
    "purchase_transactions",
    "batch_id=37 bsn_code=030บ5135 product_id=98",
    "product_id=98",
    "product_id=105",
    "UPDATE purchase_transactions SET product_id=105 WHERE batch_id=37 AND bsn_code='030บ5135' AND product_id=98",
    (),
    "SPECIAL-remap-98→105-batch37-rows",
)
# 105 unit_conversion ตว=1.0 already planned above

# ===========================================================================
# SPECIAL: pid 806, 807 — change unit_type โหล→อัน, ×12 scaling
# Current stock: 806=4, 807=4 (โหล units in stock_levels + transactions)
# After change: stock_levels 806=48, 807=48 (×12)
# Also multiply every transactions.quantity_change ×12 for 806/807
# NOTE: This uses direct SQL; the after_transaction_update trigger will fire
#       on each UPDATE to transactions, which would double-count.
#       Therefore we must:
#       1. UPDATE products SET unit_type='อัน'
#       2. Manually update stock_levels directly (bypass trigger)
#       3. UPDATE transactions.quantity_change ×12 — but this triggers stock
#          recalc via after_transaction_update trigger.
#       SAFE APPROACH: disable the trigger effect by deleting stock_levels first,
#       then updating transactions (trigger fires but stock_levels starts at 0),
#       then rebuild stock_levels from scratch.
#       Actually simpler: UPDATE stock_levels directly after updating transactions
#       by recalculating from transactions sum.
# ===========================================================================

# These are recorded but executed specially in the main block below
PIDS_806_807 = [806, 807]

# ===========================================================================
# Collect 806/807 old values for CSV
# ===========================================================================

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    print("\n=== PLAN SUMMARY ===")
    categories = {}
    for c in changes:
        cat = c["category"]
        categories.setdefault(cat, []).append(c)
    for cat, items in categories.items():
        print(f"  {cat}: {len(items)} changes")
    print(f"\nTotal planned changes: {len(changes)}")

    # --- Gather 806/807 current state ---
    rows_806_807 = {}
    for pid in PIDS_806_807:
        sl = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
        prod = conn.execute("SELECT unit_type, product_name FROM products WHERE id=?", (pid,)).fetchone()
        txns = conn.execute("SELECT id, quantity_change FROM transactions WHERE product_id=?", (pid,)).fetchall()
        rows_806_807[pid] = {
            "product_name": prod["product_name"],
            "old_unit_type": prod["unit_type"],
            "old_stock": sl["quantity"] if sl else 0,
            "txns": [(t["id"], t["quantity_change"]) for t in txns],
        }
        new_stock = (sl["quantity"] if sl else 0) * 12
        print(f"\n  pid {pid} ({prod['product_name']})")
        print(f"    unit_type: {prod['unit_type']} → อัน")
        print(f"    stock: {sl['quantity'] if sl else 0} → {new_stock}")
        print(f"    transactions: {len(txns)} rows, each qty_change ×12")

    print("\n" + "=" * 60)
    print("Review the plan above.")
    answer = input("Apply all changes to live DB? (yes/no): ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        conn.close()
        sys.exit(0)

    csv_rows = []
    applied_count = {"INSERT_UC": 0, "INSERT_MAPPING": 0, "UPDATE": 0, "SKIPPED": 0}

    with conn:
        # --- Apply planned changes ---
        for c in changes:
            action = c["action"]
            sql = c["sql"]
            params = c["params"]

            if action == "INSERT_UC" or action == "INSERT_MAPPING":
                # Check if already exists
                if action == "INSERT_UC":
                    row = conn.execute(
                        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
                        (params[0], params[1])
                    ).fetchone()
                    if row:
                        applied_count["SKIPPED"] += 1
                        csv_rows.append({
                            "category": c["category"],
                            "action": "SKIP_EXISTS",
                            "table": c["table"],
                            "key": c["key"],
                            "old_val": f"ratio={row['ratio']}",
                            "new_val": c["new_val"],
                        })
                        continue
                conn.execute(sql, params)
                applied_count[action] += 1
                csv_rows.append({
                    "category": c["category"],
                    "action": action,
                    "table": c["table"],
                    "key": c["key"],
                    "old_val": c["old_val"],
                    "new_val": c["new_val"],
                })

            elif action == "UPDATE":
                conn.execute(sql, params)
                applied_count["UPDATE"] += 1
                csv_rows.append({
                    "category": c["category"],
                    "action": action,
                    "table": c["table"],
                    "key": c["key"],
                    "old_val": c["old_val"],
                    "new_val": c["new_val"],
                })

        # --- 806/807 unit_type change + ×12 scaling ---
        # Step 1: UPDATE products.unit_type
        for pid in PIDS_806_807:
            conn.execute("UPDATE products SET unit_type='อัน' WHERE id=?", (pid,))
            csv_rows.append({
                "category": "SPECIAL-806807-unit_type",
                "action": "UPDATE",
                "table": "products",
                "key": f"id={pid}",
                "old_val": "unit_type=โหล",
                "new_val": "unit_type=อัน",
            })

        # Step 2: Update transactions.quantity_change ×12 for 806/807
        # The after_transaction_update trigger will fire and update stock_levels
        # So we first zero out stock_levels for 806/807 to avoid double-counting,
        # then update each transaction one by one (trigger fires, stock_levels rebuilds),
        # but that's complex. Instead:
        # - DELETE from stock_levels for 806/807 first
        # - UPDATE all transactions quantity_change ×12
        #   (trigger fires on each UPDATE: "reverse OLD effect, apply NEW effect"
        #    but stock_levels row was deleted, so INSERT 0 + apply NEW = correct)
        # Actually the trigger does:
        #   UPDATE stock_levels SET quantity = quantity - OLD.quantity_change WHERE product_id=OLD.product_id
        #   INSERT INTO stock_levels(product_id, quantity) VALUES (NEW.product_id, 0) ON CONFLICT DO NOTHING
        #   UPDATE stock_levels SET quantity = quantity + NEW.quantity_change WHERE product_id=NEW.product_id
        # If we delete stock_levels first, the first UPDATE is a no-op (no row),
        # INSERT creates 0 row, then second UPDATE adds NEW.quantity_change.
        # But if we update multiple transactions, each subsequent trigger will see
        # the accumulated stock_levels from previous triggers. That's correct behavior.
        # HOWEVER: the first transaction triggers INSERT of stock_levels(pid, 0),
        # then the second trigger fires: "UPDATE ... SET qty = qty - OLD.qty" (wrong: OLD.qty is the
        # ORIGINAL, not post-first-update). Each trigger sees the original OLD/NEW for that row.
        # So after deleting stock_levels and updating all txns, stock_levels = SUM(new qty_changes).
        # That is what we want.

        for pid in PIDS_806_807:
            old_info = rows_806_807[pid]
            # Delete stock_levels so trigger rebuilds cleanly
            conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
            csv_rows.append({
                "category": "SPECIAL-806807-stock-scale",
                "action": "DELETE_STOCK_LEVELS",
                "table": "stock_levels",
                "key": f"product_id={pid}",
                "old_val": str(old_info["old_stock"]),
                "new_val": "deleted (will rebuild via trigger)",
            })

            # Update each transaction ×12
            for txn_id, old_qty in old_info["txns"]:
                new_qty = old_qty * 12
                conn.execute(
                    "UPDATE transactions SET quantity_change=? WHERE id=?",
                    (new_qty, txn_id)
                )
                csv_rows.append({
                    "category": "SPECIAL-806807-txn-scale",
                    "action": "UPDATE",
                    "table": "transactions",
                    "key": f"id={txn_id} product_id={pid}",
                    "old_val": str(old_qty),
                    "new_val": str(new_qty),
                })

        # --- Verify 806/807 final stock ---
        for pid in PIDS_806_807:
            sl = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
            old_stock = rows_806_807[pid]["old_stock"]
            expected = old_stock * 12
            actual = sl["quantity"] if sl else 0
            status = "OK" if actual == expected else f"MISMATCH (expected {expected})"
            print(f"\n  pid {pid}: stock {old_stock} → {actual} [{status}]")

        # --- Also update existing unit_conversions for 806/807 ---
        # Existing: (หล,1.0) and (โหล,1.0) — after unit_type=อัน, bought อัน (raw=อน from batch-37)
        # The instruction says "bought-in-อัน → ratio 1 (อัน==unit_type)"
        # batch-37 for 806: has อน(1 row) and หล(lots) — but now unit_type=อัน
        # Need unit_conversion: อน=1
        # หล=1 already exists but is now wrong (1 โหล ≠ 1 อัน) — BUT since we scaled all
        # existing stock and transactions, the หล rows are already scaled.
        # For NEW batch-37 purchases (which are already in transactions), they are scaled.
        # But synced_to_stock=0 rows in batch-37 still need to sync:
        # 806 batch-37 rows: หล(qty=20,20,20,10) and อน(qty=1)
        # After unit_type=อัน: หล should map at 12:1 (1 โหล = 12 อัน)
        # But the instruction says: "Then bought-in-อัน → ratio 1 (อัน==unit_type)"
        # So the อัน (raw=อน) purchases → ratio=1 (correct)
        # The หล purchases (raw=หล) → these are PAST purchases that were in โหล unit
        # Since we scaled the existing synced transactions ×12, and now unit_type=อัน,
        # for the unsynced batch-37 หล rows we need a conversion: หล=12 (1 โหล = 12 อัน)
        # The existing unit_conversions หล=1.0 is WRONG for the new unit_type
        # We need to UPDATE หล from 1.0 to 12.0 for 806/807
        for pid in PIDS_806_807:
            old_uc = conn.execute(
                "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit='หล'",
                (pid,)
            ).fetchone()
            if old_uc:
                old_ratio = old_uc["ratio"]
                conn.execute(
                    "UPDATE unit_conversions SET ratio=12.0 WHERE product_id=? AND bsn_unit='หล'",
                    (pid,)
                )
                csv_rows.append({
                    "category": "SPECIAL-806807-UC-หล",
                    "action": "UPDATE",
                    "table": "unit_conversions",
                    "key": f"product_id={pid} bsn_unit=หล",
                    "old_val": f"ratio={old_ratio}",
                    "new_val": "ratio=12.0",
                })

            # Add อน=1 for batch-37 อัน purchases (raw=อน)
            conn.execute(
                "INSERT OR IGNORE INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                (pid, "อน", 1.0)
            )
            csv_rows.append({
                "category": "SPECIAL-806807-UC-อน",
                "action": "INSERT_UC",
                "table": "unit_conversions",
                "key": f"product_id={pid} bsn_unit=อน",
                "old_val": "MISSING",
                "new_val": "ratio=1.0",
            })

    # --- Write reversible CSV ---
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "action", "table", "key", "old_val", "new_val"])
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"\n=== APPLIED ===")
    print(f"  INSERT_UC:      {applied_count['INSERT_UC']}")
    print(f"  INSERT_MAPPING: {applied_count['INSERT_MAPPING']}")
    print(f"  UPDATE:         {applied_count['UPDATE']}")
    print(f"  SKIPPED:        {applied_count['SKIPPED']}")
    print(f"\nReversible CSV: {CSV_PATH}")

    conn.close()


if __name__ == "__main__":
    main()
