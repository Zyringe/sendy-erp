"""Tests for scripts/merge_product.py (synthetic pids).

- ledger / sales / mapping reassigned FROM→TO
- unit_conversions: shared unit deduped, unique unit moved
- TO stock recalculated, total conserved; FROM stock_levels gone, is_active=0
- dry-run writes nothing
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import merge_product as mp  # noqa: E402

SRC, DST = 903101, 903102


def _p(c, pid):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,'ดอก',?,1)", (pid, pid, f"P{pid}",
                                                      f"S{pid}"))


def _t(c, pid, q, when="2025-06-01 00:00:00"):
    c.execute("INSERT INTO transactions (product_id,txn_type,quantity_change,"
              "unit_mode,note,created_at) VALUES (?,'ADJUST',?,'unit','x',?)",
              (pid, q, when))


def _u(c, pid, unit, r=1.0):
    c.execute("INSERT INTO unit_conversions (product_id,bsn_unit,ratio) "
              "VALUES (?,?,?)", (pid, unit, r))


def _s(c, pid, unit):
    c.execute("INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
              "doc_base,product_id,bsn_code,product_name_raw,customer,"
              "customer_code,qty,unit,unit_price,vat_type,discount,total,"
              "net,synced_to_stock) VALUES ('t','2025-01-01','D','D',?,?,"
              "'r','C','C1',1,?,1,0,0,0,0,1)", (pid, f"C{pid}", unit))


def test_merge(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, DST); _t(conn, DST, 87)                       # TO: +87
    _u(conn, DST, "ดอก", 1); _u(conn, DST, "ใบ", 1)
    _p(conn, SRC)
    _t(conn, SRC, 50); _t(conn, SRC, -50)                  # FROM: nets 0
    _u(conn, SRC, "ดอก", 1)                                # dup → dropped
    _u(conn, SRC, "โหล", 12)                               # unique → moved
    _s(conn, SRC, "ดอก")
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES ('CSRC','n',?,0)", (SRC,))
    conn.commit()
    conn.close()

    assert mp.main(["--from", str(SRC), "--to", str(DST),
                    "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def one(sql, *a):
        return conn.execute(sql, a).fetchone()[0]

    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               DST) == 87                                  # 87 + 0 conserved
    assert one("SELECT COUNT(*) FROM stock_levels WHERE product_id=?",
               SRC) == 0
    assert one("SELECT is_active FROM products WHERE id=?", SRC) == 0
    assert one("SELECT COUNT(*) FROM transactions WHERE product_id=?",
               SRC) == 0
    assert one("SELECT COUNT(*) FROM transactions WHERE product_id=?",
               DST) == 3                                   # 1 + 2 moved
    assert one("SELECT COUNT(*) FROM sales_transactions WHERE product_id=?",
               DST) == 1
    assert one("SELECT product_id FROM product_code_mapping WHERE "
               "bsn_code='CSRC'") == DST
    units = {r[0] for r in conn.execute(
        "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (DST,))}
    assert units == {"ดอก", "ใบ", "โหล"}                    # dup ดอก dropped
    assert one("SELECT COUNT(*) FROM unit_conversions WHERE product_id=?",
               SRC) == 0
    conn.close()


def test_merge_dry_run_writes_nothing(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 903201); _p(conn, 903202)
    _t(conn, 903201, 5)
    conn.commit()
    conn.close()
    assert mp.main(["--from", "903201", "--to", "903202",
                    "--db", tmp_db]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT is_active FROM products WHERE id=903201"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE "
                        "product_id=903201").fetchone()[0] == 1
    conn.close()
