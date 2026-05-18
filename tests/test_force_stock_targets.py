"""Tests for scripts/force_stock_targets.py (scoped to synthetic pids).

DEFER pid → current stock == reference-backup value; stale opening +
2026-04-07 นับสต็อก removed+archived. A negative non-DEFER pid → current 0.
A non-negative non-DEFER pid is left alone. Dry-run writes nothing.
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import force_stock_targets as ft  # noqa: E402

DEF, NEG, OK = 900501, 900502, 900503


def _prod(c, pid):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,?,?,1)",
              (pid, pid, f"P{pid}", "ดอก", f"SK-{pid}"))


def _txn(c, pid, t, qty, note, when="2025-06-01 00:00:00"):
    c.execute("INSERT INTO transactions (product_id,txn_type,quantity_change,"
              "unit_mode,reference_no,note,created_at) VALUES (?,?,?,'unit',"
              "NULL,?,?)", (pid, t, qty, note, when))


def _mk_backup(tmp_path, pid_val):
    p = tmp_path / "ref.db"
    b = sqlite3.connect(p)
    b.execute("CREATE TABLE stock_levels (product_id INT, quantity REAL)")
    for pid, v in pid_val.items():
        b.execute("INSERT INTO stock_levels VALUES (?,?)", (pid, v))
    b.commit()
    b.close()
    return str(p)


def test_force_targets(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "DEFER_PIDS", [DEF])
    conn = sqlite3.connect(tmp_db)
    # DEF: stale opening + stale 4/7 นับ + real BSN; backup says 818690
    _prod(conn, DEF)
    _txn(conn, DEF, "ADJUST", 37252,
         "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)",
         "2024-01-03 00:00:00")
    _txn(conn, DEF, "ADJUST", -178200, "นับสต็อก", "2026-04-07 10:00:00")
    _txn(conn, DEF, "OUT", -7050, "BSN ขาย", "2026-04-16 00:00:00")
    # NEG: negative non-DEFER -> 0
    _prod(conn, NEG)
    _txn(conn, NEG, "OUT", -761, "BSN ขาย")
    # OK: positive non-DEFER -> untouched
    _prod(conn, OK)
    _txn(conn, OK, "IN", 40, "BSN ซื้อ")
    conn.commit()
    conn.close()

    bk = _mk_backup(tmp_path, {DEF: 818690})
    assert ft.main(["--db", tmp_db, "--backup", bk, "--apply"]) == 0

    conn = sqlite3.connect(tmp_db)

    def q(pid):
        return conn.execute("SELECT quantity FROM stock_levels "
                            "WHERE product_id=?", (pid,)).fetchone()[0]

    def n(sql, *a):
        return conn.execute(sql, a).fetchone()[0]

    assert q(DEF) == 818690                       # forced to backup value
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note='นับสต็อก'", DEF) == 0      # stale 4/7 removed
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note LIKE 'ยอดต้นปี%'", DEF) == 1   # one clean opening
    op = conn.execute("SELECT quantity_change,created_at FROM transactions "
                       "WHERE product_id=? AND note=?",
                       (DEF, ft.OPENING_NOTE)).fetchone()
    # remaining after removal = BSN -7050 ; opening = 818690 - (-7050)
    assert op[0] == 818690 + 7050 and op[1] == "2024-01-03 00:00:00"

    assert q(NEG) == 0                             # negative -> 0
    assert q(OK) == 40                             # untouched

    exp = os.path.join(REPO, "data", "exports")
    a = sorted(f for f in os.listdir(exp)
               if f.startswith("removed_force_targets_"))[-1]
    body = open(os.path.join(exp, a)).read()
    assert f",{DEF}," in body and "นับสต็อก" in body
    conn.close()


def test_force_dry_run_writes_nothing(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "DEFER_PIDS", [])
    conn = sqlite3.connect(tmp_db)
    _prod(conn, 900601)
    _txn(conn, 900601, "OUT", -5, "BSN ขาย")
    conn.commit()
    conn.close()
    bk = _mk_backup(tmp_path, {})
    assert ft.main(["--db", tmp_db, "--backup", bk]) == 0   # no --apply
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT quantity FROM stock_levels WHERE "
                        "product_id=900601").fetchone()[0] == -5
    conn.close()
