"""Mapping resolution (models.py) via resolve_pending_mappings() — pure
bsn_code→product_id (resolve_pending_mappings does NOT consult bsn_unit even
after mig 124 restored the column; see test_mapping_unit_aware_restore.py for
the unit-aware `_resolve_mapping` spec used at import time).

Tests verify: one row per bsn_code, product_id-only join, routes ANY unit of
that code to the same product.
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

MIG_124 = os.path.join(REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")

PA, PB = 907001, 907002          # synthetic product ids
CODE = "ZRES100"


def _migrate124(conn):
    with open(MIG_124, encoding="utf-8") as f:
        conn.executescript(f.read())


def _seed(conn):
    conn.row_factory = sqlite3.Row    # _sync_bsn_to_stock uses row['col']
    for pid in (PA, PB):
        conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (pid, f"P{pid}", f"SK{pid}"))


def _sale(conn, unit, did):
    # qty=0 → resolution runs, _sync_bsn_to_stock creates no ledger row
    conn.execute(
        "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,doc_base,"
        "product_id,bsn_code,product_name_raw,customer,customer_code,qty,"
        "unit,unit_price,vat_type,discount,total,net,synced_to_stock) "
        "VALUES (0,'2026-05-09',?,?,NULL,?,'r','C','C1',0,?,1,0,0,0,0,0)",
        (did, did, CODE, unit))


def _pid_of(conn, did):
    return conn.execute("SELECT product_id FROM sales_transactions "
                        "WHERE doc_no=?", (did,)).fetchone()[0]


def test_single_mapping_resolves_all_units(tmp_db):
    """After mig-112: one mapping row routes all units of that code."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id) "
                 "VALUES (?,?,?)", (CODE, "n", PA))
    _sale(conn, "แผง", "D1")
    _sale(conn, "ตัว", "D2")
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "D1") == PA
    assert _pid_of(conn, "D2") == PA      # both units → same single mapping
    conn.close()


def test_unmapped_code_leaves_null(tmp_db):
    """A code with no mapping row stays unresolved."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    _sale(conn, "ตัว", "D1")
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "D1") is None
    conn.close()


def test_null_unit_resolves_to_same_product(tmp_db):
    """NULL unit still resolves via the single mapping row."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id) "
                 "VALUES (?,?,?)", (CODE, "n", PA))
    conn.execute(
        "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,doc_base,"
        "product_id,bsn_code,product_name_raw,customer,customer_code,qty,"
        "unit,unit_price,vat_type,discount,total,net,synced_to_stock) "
        "VALUES (0,'2026-05-09','DN','DN',NULL,?,'r','C','C1',0,NULL,1,"
        "0,0,0,0,0)", (CODE,))
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "DN") == PA
    conn.close()


def test_upsert_mapping_by_bsn_code(tmp_db):
    """upsert_mapping by bsn_code only — second call updates same row."""
    conn = sqlite3.connect(tmp_db)
    _migrate124(conn)
    _seed(conn)
    conn.commit()
    conn.close()
    models.upsert_mapping(CODE, "n1", product_id=PA)
    models.upsert_mapping(CODE, "n2", product_id=PB)  # update — same bsn_code
    c = sqlite3.connect(tmp_db)
    rows = c.execute("SELECT product_id, bsn_name FROM "
                     "product_code_mapping WHERE bsn_code=?",
                     (CODE,)).fetchall()
    # exactly one row, updated to PB/n2
    assert len(rows) == 1
    assert rows[0][0] == PB
    assert rows[0][1] == "n2"
    c.close()
