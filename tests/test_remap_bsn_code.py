"""Tests for scripts/remap_bsn_code.py (synthetic pids).

- mapping product_id repointed old→new for the code
- every sales/purchase row of that code moved to new product
- both products stay active (NOT merged); stock recalced for both
- code with no existing mapping → mapping row created
- dry-run writes nothing

NOTE (2026-07-03): `test_remap`'s 3 seeded sales rows are `synced_to_stock=1`
with NO matching `transactions` ledger row (a stand-in for the old bug's
"marked synced, never actually posted" state — see models.repoint_bsn_code's
docstring / decisions/log.md 2026-07-02/07-03). The old (buggy) script never
touched the ledger, so it never posted these 3 units either — the earlier
version of this test asserted NEW's stock stayed at 4704 (the unrelated
manual stock only), which was encoding the BUG as expected behavior. The
fixed script forces a full ledger rebuild for every affected product, so
these 3 historical แผง-unit OUT rows now correctly land on NEW's ledger:
4704 - 3 = 4701. See tests/test_repoint_bsn_code.py for focused coverage of
the fix itself (ledger orphans, unit-conversion ratios, idempotency, the
split-code bsn_unit-scoped case).
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
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'แผง', ?, 1)", (pid, f"P{pid}", f"S{pid}"))


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
    # 4704 (manual stock) - 3 (the 3 historical sales, now correctly rebuilt
    # onto NEW's ledger instead of staying stranded/never-posted — see the
    # module docstring note above).
    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               NEW) == 4701
    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               OLD) == 0
    # regression: no stray `transactions` ledger rows for CODE off of NEW.
    orphan = one(
        "SELECT COUNT(*) FROM transactions t WHERE t.note LIKE 'BSN%' "
        "AND t.reference_no IN (SELECT doc_no FROM sales_transactions "
        "WHERE bsn_code=?) AND t.product_id<>?", CODE, NEW)
    assert orphan == 0
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
