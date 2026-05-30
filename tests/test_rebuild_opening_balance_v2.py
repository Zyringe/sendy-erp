"""Tests for scripts/rebuild_opening_balance_v2.py (3-bucket + DEFER).

Verifies, scoped to synthetic pids (tmp_db is a COPY of the live DB):
  bucket A  CSV product   → Σ(qty ≤ 2026-03-03) == CSV Pieces; old
            ยอดต้นปี removed, split/นับสต็อก/BSN kept.
  bucket 1  non-CSV        → Σ(qty ≤ cutoff) == 0.
  bucket 3  non-CSV + 4/7  → Σ(qty ≤ cutoff) == the 2026-04-07 count;
            the stale 4/7 นับสต็อก row archived + removed (no double count).
  DEFER     ratio-broken   → COMPLETELY untouched (opening kept, 4/7 kept,
            stock unchanged, no new opening inserted).
  dry-run writes nothing; sku_code/product_name preserved.
"""
import csv
import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import rebuild_opening_balance_v2 as rb2  # noqa: E402

A, B1, B3, D = 900301, 900302, 900303, 900304


def _prod(c, pid):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,?,?,1)",
              (pid, pid, f"P{pid}", "ตัว", f"SK-{pid}"))


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


def _seed(conn):
    # A — CSV, Pieces 500
    _prod(conn, A)
    _txn(conn, A, "ADJUST", 100, rb2.OPENING_NOTE, "2024-01-03 00:00:00")  # rm
    _txn(conn, A, "ADJUST", 5,
         "opening-balance split from old pid 1 (ตัว/แผง separation)",
         "2024-01-03 00:00:00")                                            # keep
    _txn(conn, A, "ADJUST", 3, "นับสต็อก", "2026-04-07 09:00:00")  # keep (post)
    _txn(conn, A, "OUT", -30, "BSN ขาย", "2025-06-01 00:00:00")    # keep pre
    _txn(conn, A, "OUT", -10, "BSN ขาย", "2026-04-01 00:00:00")    # keep post
    # B1 — non-CSV, only a pre-cutoff BSN OUT
    _prod(conn, B1)
    _txn(conn, B1, "OUT", -20, "BSN ขาย", "2025-05-01 00:00:00")
    # B3 — non-CSV + 2026-04-07 count of 25 (+ a pre-cutoff BSN OUT)
    _prod(conn, B3)
    _txn(conn, B3, "OUT", -10, "BSN ขาย", "2025-06-01 00:00:00")
    _txn(conn, B3, "ADJUST", 25, "นับสต็อก", "2026-04-07 10:00:00")
    # D — DEFER (ratio-broken): old opening + 4/7 count + BSN, all kept
    _prod(conn, D)
    _txn(conn, D, "ADJUST", 100, rb2.OPENING_NOTE, "2024-01-03 00:00:00")
    _txn(conn, D, "ADJUST", -5, "นับสต็อก", "2026-04-07 11:00:00")
    _txn(conn, D, "OUT", -3, "BSN ขาย", "2025-06-01 00:00:00")
    conn.commit()


@pytest.mark.skip(
    reason=(
        "superseded by 2026-05-30 full ledger rebuild — script is deprecated "
        "('one-off from 2026-05-18'); running it on a post-rebuild DB clone "
        "produces float-precision cutoff mismatches (near-zero residuals from "
        "back-solved opening rows) plus post-rebuild negative-stock products, "
        "causing the script to return exit-code 1 (the exact negative count is "
        "snapshot-dependent and volatile, so it is intentionally not quoted "
        "here); the opening-balance state the script targeted is now baked into "
        "the rebuilt baseline"
    )
)
def test_rebuild_v2_buckets_and_defer(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(rb2, "DEFER_PIDS", {D})
    monkeypatch.setattr(rb2, "BUCKET3_PIDS", {B3})
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.close()

    csvf = _csv(tmp_path, [(str(A), "500")])    # only A in CSV
    assert rb2.main([csvf, "--db", tmp_db, "--apply"]) == 0

    conn = sqlite3.connect(tmp_db)

    def cut(pid):
        return conn.execute(
            "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            "WHERE product_id=? AND created_at<=?",
            (pid, rb2.CUTOFF)).fetchone()[0]

    def n(sql, *a):
        return conn.execute(sql, a).fetchone()[0]

    # bucket A: cutoff == CSV Pieces; old ยอดต้นปี gone (1 = new opening),
    # split + นับสต็อก + BSN kept
    assert cut(A) == 500
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note LIKE 'ยอดต้นปี%'", A) == 1
    op = conn.execute("SELECT quantity_change,created_at FROM transactions "
                       "WHERE product_id=? AND note=?",
                       (A, rb2.OPENING_NOTE)).fetchone()
    assert op[0] == 525 and op[1] == "2024-01-03 00:00:00"
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note LIKE 'opening-balance split%'", A) == 1
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note='นับสต็อก'", A) == 1
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note='BSN ขาย'", A) == 2
    assert n("SELECT quantity FROM stock_levels WHERE product_id=?",
             A) == 525 + 5 - 30 + 3 - 10                          # 493

    # bucket 1: non-CSV → cutoff 0
    assert cut(B1) == 0
    assert n("SELECT quantity FROM stock_levels WHERE product_id=?", B1) == 0

    # bucket 3: cutoff == 4/7 count (25); stale 4/7 นับสต็อก removed; opening
    # relocated so no double count → current == 25
    assert cut(B3) == 25
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note='นับสต็อก'", B3) == 0
    assert n("SELECT quantity FROM stock_levels WHERE product_id=?", B3) == 25

    # DEFER: untouched — opening kept (1), 4/7 kept, stock unchanged, no
    # second opening inserted
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note=?", D, rb2.OPENING_NOTE) == 1
    assert n("SELECT COUNT(*) FROM transactions WHERE product_id=? "
             "AND note='นับสต็อก'", D) == 1
    assert n("SELECT quantity FROM stock_levels WHERE product_id=?",
             D) == 100 - 5 - 3                                     # 92

    # Put's edits untouched
    assert n("SELECT sku_code FROM products WHERE id=?", A) == f"SK-{A}"

    # archive written and contains the removed bucket-3 4/7 row
    exp = os.path.join(REPO, "data", "exports")
    arch = sorted(f for f in os.listdir(exp)
                  if f.startswith("removed_opening_adjusts_v2_"))[-1]
    body = open(os.path.join(exp, arch)).read()
    assert f",{B3}," in body and "นับสต็อก" in body
    conn.close()


def test_v2_dry_run_writes_nothing(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(rb2, "DEFER_PIDS", set())
    monkeypatch.setattr(rb2, "BUCKET3_PIDS", set())
    conn = sqlite3.connect(tmp_db)
    _prod(conn, 900401)
    _txn(conn, 900401, "ADJUST", 100, rb2.OPENING_NOTE,
         "2024-01-03 00:00:00")
    conn.commit()
    conn.close()
    csvf = _csv(tmp_path, [("900401", "300")])
    assert rb2.main([csvf, "--db", tmp_db]) == 0          # no --apply
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE "
                        "product_id=900401 AND note LIKE 'ยอดต้นปี%'"
                        ).fetchone()[0] == 1               # not deleted
    conn.close()
