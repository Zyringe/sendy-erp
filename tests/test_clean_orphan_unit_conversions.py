"""Tests for scripts/clean_orphan_unit_conversions.py (synthetic pids).

- no mapping + no ledger ref          → deleted
- no mapping but ledger references it → KEPT (re-sync would break)
- has active mapping                  → KEPT (not orphan)
- ignored-only mapping + no ledger    → deleted (ignored doesn't count)
- transactions/stock_levels untouched; dry-run writes nothing.
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import clean_orphan_unit_conversions as co  # noqa: E402

DEL, KEEPL, KEEPM, DELIG = 900901, 900902, 900903, 900904


def _p(c, pid):
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'แผง', ?, 1)", (pid, f"P{pid}", f"S{pid}"))


def _u(c, pid, unit, r=1.0):
    c.execute("INSERT INTO unit_conversions (product_id,bsn_unit,ratio) "
              "VALUES (?,?,?)", (pid, unit, r))


def _m(c, pid, code, ignored=0):
    c.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
              "product_id,is_ignored) VALUES (?,?,?,?)",
              (code, "n", pid, ignored))


def _s(c, pid, unit):
    c.execute("INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
              "doc_base,product_id,bsn_code,product_name_raw,customer,"
              "customer_code,qty,unit,unit_price,vat_type,discount,total,"
              "net,synced_to_stock) VALUES ('t','2025-01-01','D','D',?,'ZZ',"
              "'r','C','C1',1,?,1,0,0,0,0,1)", (pid, unit))


def test_clean_orphans(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, DEL);    _u(conn, DEL, "โหล", 12)                 # orphan→del
    _p(conn, KEEPL);  _u(conn, KEEPL, "โหล", 12); _s(conn, KEEPL, "โหล")
    _p(conn, KEEPM);  _u(conn, KEEPM, "โหล", 12); _m(conn, KEEPM, "ZZ903")
    _p(conn, DELIG);  _u(conn, DELIG, "กล่อง", 1)
    _m(conn, DELIG, "ZZ904", ignored=1)                        # ignored→del
    conn.commit()
    tx0 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.close()

    assert co.main(["--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def has(pid):
        return conn.execute("SELECT COUNT(*) FROM unit_conversions WHERE "
                            "product_id=?", (pid,)).fetchone()[0]

    assert has(DEL) == 0                       # deleted
    assert has(DELIG) == 0                     # ignored-only → deleted
    assert has(KEEPL) == 1                     # ledger-referenced → kept
    assert has(KEEPM) == 1                     # active mapping → kept
    assert conn.execute("SELECT COUNT(*) FROM transactions"
                        ).fetchone()[0] == tx0
    conn.close()


def test_clean_dry_run_writes_nothing(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 901001)
    _u(conn, 901001, "โหล", 12)
    conn.commit()
    conn.close()
    assert co.main(["--db", tmp_db]) == 0       # no --apply
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM unit_conversions WHERE "
                        "product_id=901001").fetchone()[0] == 1
    conn.close()
