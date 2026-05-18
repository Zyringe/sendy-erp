"""Tests for scripts/rebuild_opening_balance_from_csv.py.

Verifies: script-made opening/loss ADJUSTs removed+archived; split/นับสต็อก/BSN
kept; back-solved opening @2024-01-03 makes Σ(qty ≤ 2026-03-03) == CSV Pieces;
products not in CSV get no opening; post-cutoff movements carry forward;
mapping/unit/sku untouched.
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import rebuild_opening_balance_from_csv as rb  # noqa: E402


def _prod(c, pid, sku):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,?,?,1)",
              (pid, sku, f"P{pid}", "ตัว", f"SK-{pid}"))


def _txn(c, pid, t, qty, note, when):
    c.execute("INSERT INTO transactions (product_id,txn_type,quantity_change,"
              "unit_mode,reference_no,note,created_at) VALUES (?,?,?,'unit',"
              "NULL,?,?)", (pid, t, qty, note, when))


def _csv(tmp_path, rows):
    p = tmp_path / "open.csv"
    cols = ["Date", "SKU (Order)", "Floor", "Number", "Floor-No", "รายการ",
            "หมายเหตุ", "ลัง", "เศษ", "บรรจุ/ลัง", "บรรจุ/กล่อง", "หน่วย",
            "Pieces", "Cost_per_Pieces", "Total_Value", "Remark"]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for sku, pieces in rows:
            row = {c: "" for c in cols}
            row["SKU (Order)"] = sku
            row["Pieces"] = pieces
            w.writerow([row[c] for c in cols])
    return str(p)


def test_rebuild_backsolve_and_preserve(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    # pidA (in CSV, sku 900101)
    _prod(conn, 900101, 900101)
    _txn(conn, 900101, "ADJUST", 100,
         "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)",
         "2024-01-03 00:00:00")                                  # REMOVE
    _txn(conn, 900101, "ADJUST", 7,
         "ยอดสูญหาย/ส่วนต่าง (stock loss — BSN IN > current + OUT)",
         "2026-04-29 23:59:59")                                  # REMOVE
    _txn(conn, 900101, "ADJUST", 5,
         "opening-balance split from old pid 1 (ตัว/แผง separation 2026-05-15)",
         "2024-01-03 00:00:00")                                  # KEEP
    _txn(conn, 900101, "ADJUST", 3, "นับสต็อก", "2026-04-07 09:00:00")  # KEEP
    _txn(conn, 900101, "OUT", -30, "BSN ขาย", "2025-06-01 00:00:00")    # KEEP, pre
    _txn(conn, 900101, "OUT", -10, "BSN ขาย", "2026-04-01 00:00:00")    # KEEP, post
    # pidB (NOT in CSV)
    _prod(conn, 900102, 900102)
    _txn(conn, 900102, "OUT", -20, "BSN ขาย", "2025-05-01 00:00:00")
    conn.commit(); conn.close()

    csvf = _csv(tmp_path, [("900101", "500")])   # 900102 absent on purpose
    assert rb.main([csvf, "--db", tmp_db, "--apply"]) == 0

    conn = sqlite3.connect(tmp_db)
    # tmp_db is a COPY of the live DB → scope every assertion to pid 900101.
    def n(sql, *a):
        return conn.execute(sql, a).fetchone()[0]
    # only the new back-solved opening remains under ยอดต้นปี% (seeded one + the
    # global live ones deleted); ยอดสูญหาย gone for this pid
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=900101 "
             "AND note LIKE 'ยอดต้นปี%'") == 1
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=900101 "
             "AND note LIKE 'ยอดสูญหาย%'") == 0
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=900101 "
             "AND note LIKE 'opening-balance split%'") == 1     # KEPT
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=900101 "
             "AND note='นับสต็อก'") == 1                          # KEPT
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=900101 "
             "AND note='BSN ขาย'") == 2                           # KEPT

    # back-solve: remaining(≤cutoff, excl removed) = split 5 + BSN -30 = -25
    # opening = 500 - (-25) = 525, dated 2024-01-03, OPENING_NOTE
    op = conn.execute(
        "SELECT quantity_change,created_at,note FROM transactions "
        "WHERE product_id=900101 AND note=? ", (rb.OPENING_NOTE,)).fetchone()
    assert op[0] == 525 and op[1] == "2024-01-03 00:00:00"
    cut = conn.execute(
        "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
        "WHERE product_id=900101 AND created_at<=?", (rb.CUTOFF,)).fetchone()[0]
    assert cut == 500                                   # == CSV pieces
    now = conn.execute("SELECT quantity FROM stock_levels WHERE product_id="
                       "900101").fetchone()[0]
    # cutoff stock 500 + post-cutoff: นับสต็อก +3 (2026-04-07) + BSN -10
    assert now == 500 + 3 - 10                           # = 493

    # pidB not in CSV → no opening row; stock = movements only
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE "
                        "product_id=900102 AND note=?",
                        (rb.OPENING_NOTE,)).fetchone()[0] == 0
    assert conn.execute("SELECT quantity FROM stock_levels WHERE product_id="
                        "900102").fetchone()[0] == -20

    # Put's edits untouched
    assert conn.execute("SELECT sku_code FROM products WHERE id=900101"
                        ).fetchone()[0] == "SK-900101"
    conn.close()


def test_dry_run_writes_nothing(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _prod(conn, 900201, 900201)
    _txn(conn, 900201, "ADJUST", 100,
         "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)",
         "2024-01-03 00:00:00")
    conn.commit(); conn.close()
    csvf = _csv(tmp_path, [("900201", "300")])
    assert rb.main([csvf, "--db", tmp_db]) == 0          # no --apply
    conn = sqlite3.connect(tmp_db)
    # removed note still present (dry-run did not delete)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE "
                        "product_id=900201 AND note LIKE 'ยอดต้นปี%'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE "
                        "product_id=900201 AND note=?",
                        (rb.OPENING_NOTE,)).fetchone()[0] == 1  # the seeded one
    conn.close()
