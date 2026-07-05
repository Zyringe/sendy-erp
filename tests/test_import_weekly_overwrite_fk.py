"""Regression: models.import_weekly overwrite branch must not create a
NULL/orphan stock_levels row (and must not FK-fail under
PRAGMA foreign_keys=ON) when re-importing a CHANGED doc whose product's only
transaction is the one being deleted.

History: the old per-row overwrite did "INSERT INTO stock_levels SELECT
product_id, SUM(...)" which returned (NULL,0) on an empty set → orphan row
(source of 130 orphans). PR2 replaced that with a diff-based 2-pass that
relies on the mig-080 triggers for stock_levels (no manual stock surgery), so
the orphan path is gone — but this test still guards that the new overwrite
(triggered only by a REAL change; an identical re-import is now a no-op) leaves
no orphan and re-syncs to the corrected quantity.
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

MIG_124 = os.path.join(REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")

PID = 906001


def _migrate124(conn):
    with open(MIG_124, encoding="utf-8") as f:
        conn.executescript(f.read())
CODE = "ZZTESTFK"
DOC = "RRTESTFK1"
PRICE = 55.0


def _entry():
    # qty 3 (the seeded row is qty 2) → a REAL change that triggers the
    # overwrite path (an identical re-import is now a no-op).
    return {
        "date_iso": "2026-05-09", "doc_no": DOC, "line_seq": 1,
        "product_code_raw": CODE, "product_name_raw": "TEST PRODUCT",
        "party": "S", "party_code": "S1", "qty": 3.0, "unit": "อัน",
        "unit_price": PRICE, "vat_type": 0, "discount": 0,
        "total": 165.0, "net": 165.0,
    }


def test_overwrite_no_orphan_no_fkfail(tmp_db, monkeypatch, patch_models_conn):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate124(conn)
    conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, ?, ?, 1)", (PID, "TEST", "อัน", f"SK{PID}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES (?,?,?,0)",
                 (CODE, "TEST", PID))
    bid = conn.execute(
        "INSERT INTO import_log (filename,rows_imported,rows_skipped,notes)"
        " VALUES ('seed',0,0,'purchase')").lastrowid
    # an existing synced purchase row that the re-import will overwrite
    conn.execute(
        "INSERT INTO purchase_transactions (batch_id,date_iso,doc_no,"
        "doc_base,product_id,bsn_code,product_name_raw,supplier,"
        "supplier_code,qty,unit,unit_price,vat_type,discount,total,net,"
        "synced_to_stock) VALUES (?,'2026-05-09',?,?,?,?,'T','S','S1',"
        "2,'อัน',?,0,0,110,110,1)", (bid, DOC, DOC, PID, CODE, PRICE))
    # its ONLY transaction (the BSN sync) — overwrite deletes it, leaving
    # zero transactions for PID
    conn.execute("INSERT INTO transactions (product_id,txn_type,"
                 "quantity_change,unit_mode,reference_no,note,created_at) "
                 "VALUES (?,'IN',2,'unit',?,?,'2026-05-09 00:00:00')",
                 (PID, DOC, "BSN ซื้อ"))
    conn.execute("INSERT OR REPLACE INTO stock_levels (product_id,quantity)"
                 " VALUES (?,2)", (PID,))
    conn.commit()
    orphans_before = conn.execute(
        "SELECT COUNT(*) FROM stock_levels WHERE product_id NOT IN "
        "(SELECT id FROM products)").fetchone()[0]
    conn.close()

    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    tconn.execute("PRAGMA foreign_keys = ON")
    patch_models_conn(lambda: tconn)

    # must NOT raise sqlite3.IntegrityError (the bug)
    stats = models.import_weekly([_entry()], "purchase", "test.csv")
    assert stats["overwritten"] >= 1

    c2 = sqlite3.connect(tmp_db)
    orphans_after = c2.execute(
        "SELECT COUNT(*) FROM stock_levels WHERE product_id NOT IN "
        "(SELECT id FROM products)").fetchone()[0]
    assert orphans_after == orphans_before          # no new orphan/NULL row
    assert c2.execute("SELECT 1 FROM products WHERE id=?",
                      (PID,)).fetchone() is not None
    sl = c2.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                    (PID,)).fetchone()
    # old lone txn deleted then the re-imported (changed) entry re-synced → qty 3
    # (the point: a real product_id row, never NULL/orphan)
    assert sl is not None and sl[0] == 3
    c2.close()
