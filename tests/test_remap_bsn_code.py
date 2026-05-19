"""Tests for scripts/remap_bsn_code.py (synthetic pids).

- mapping product_id repointed old→new for the code
- every sales/purchase row of that code moved to new product
- both products stay active (NOT merged); stock recalced for both
- code with no existing mapping → mapping row created
- dry-run writes nothing
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import remap_bsn_code as rb  # noqa: E402

OLD, NEW = 903301, 903302
CODE = "ZZ903CODE"


def _p(c, pid):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,'แผง',?,1)", (pid, pid, f"P{pid}",
                                                      f"S{pid}"))


def _t(c, pid, q):
    c.execute("INSERT INTO transactions (product_id,txn_type,quantity_change,"
              "unit_mode,note,created_at) VALUES (?,'ADJUST',?,'unit','x',"
              "'2025-01-01 00:00:00')", (pid, q))


def _s(c, pid, code):
    c.execute("INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
              "doc_base,product_id,bsn_code,product_name_raw,customer,"
              "customer_code,qty,unit,unit_price,vat_type,discount,total,"
              "net,synced_to_stock) VALUES ('t','2025-01-01','D','D',?,?,"
              "'r','C','C1',1,'แผง',1,0,0,0,0,1)", (pid, code))


def test_remap(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, NEW); _t(conn, NEW, 4704)             # แผง orphan, real stock
    _s(conn, NEW, CODE)                            # 1 historical bill on NEW
    _p(conn, OLD); _t(conn, OLD, 0)                # ตัว target, empty
    _s(conn, OLD, CODE); _s(conn, OLD, CODE)       # 2 bills mis-attributed
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES (?,?,?,0)", (CODE, "n", OLD))
    conn.commit()
    conn.close()

    assert rb.main(["--code", CODE, "--to", str(NEW),
                    "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def one(sql, *a):
        return conn.execute(sql, a).fetchone()[0]

    assert one("SELECT product_id FROM product_code_mapping WHERE "
               "bsn_code=?", CODE) == NEW
    assert one("SELECT COUNT(*) FROM sales_transactions WHERE bsn_code=? "
               "AND product_id<>?", CODE, NEW) == 0          # all moved
    assert one("SELECT COUNT(*) FROM sales_transactions WHERE product_id=?",
               NEW) == 3                                      # 1 + 2 moved
    # both still active (NOT merged)
    assert one("SELECT is_active FROM products WHERE id=?", OLD) == 1
    assert one("SELECT is_active FROM products WHERE id=?", NEW) == 1
    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               NEW) == 4704
    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               OLD) == 0
    conn.close()


def test_remap_creates_mapping_when_absent(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 903401)
    _s(conn, 903401, "ZZNOMAP")                    # no mapping row exists
    conn.commit()
    conn.close()
    assert rb.main(["--code", "ZZNOMAP", "--to", "903401",
                    "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT product_id FROM product_code_mapping WHERE "
                        "bsn_code='ZZNOMAP'").fetchone()[0] == 903401
    conn.close()


def test_remap_dry_run_writes_nothing(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 903501); _p(conn, 903502)
    _s(conn, 903501, "ZZDRY")
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES ('ZZDRY','n',903501,0)")
    conn.commit()
    conn.close()
    assert rb.main(["--code", "ZZDRY", "--to", "903502", "--db",
                    tmp_db]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT product_id FROM product_code_mapping WHERE "
                        "bsn_code='ZZDRY'").fetchone()[0] == 903501
    conn.close()
