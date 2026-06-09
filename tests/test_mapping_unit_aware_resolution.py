"""Unit-aware mapping resolution (models.py).

- catch-all-only ('' row) behaves exactly like pre-061 (regression)
- exact (bsn_code, unit) override beats the catch-all
- a unit with no override falls back to catch-all; NULL unit → catch-all
- upsert_mapping composite-conflict semantics
- get_mapping(bsn_unit=...) precedence
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

PA, PB = 907001, 907002          # synthetic product ids
CODE = "ZRES100"


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


def test_catchall_only_resolves_like_before(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    _sale(conn, "แผง", "D1")
    _sale(conn, "ตัว", "D2")
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "D1") == PA
    assert _pid_of(conn, "D2") == PA      # both → catch-all (legacy behavior)
    conn.close()


def test_exact_unit_override_beats_catchall(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'แผง')", (CODE, "n", PB))
    _sale(conn, "แผง", "D1")
    _sale(conn, "ตัว", "D2")
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "D1") == PB      # exact unit override wins
    assert _pid_of(conn, "D2") == PA      # no ตัว override → catch-all
    conn.close()


def test_missing_unit_then_catchall(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'แผง')", (CODE, "n", PB))
    _sale(conn, "ตัว", "D1")
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "D1") is None    # no catch-all, no ตัว override
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "D1") == PA      # now falls back to catch-all
    conn.close()


def test_null_unit_hits_catchall(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    conn.execute(
        "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,doc_base,"
        "product_id,bsn_code,product_name_raw,customer,customer_code,qty,"
        "unit,unit_price,vat_type,discount,total,net,synced_to_stock) "
        "VALUES (0,'2026-05-09','DN','DN',NULL,?,'r','C','C1',0,NULL,1,"
        "0,0,0,0,0)", (CODE,))
    conn.commit()
    models.resolve_pending_mappings(conn)
    assert _pid_of(conn, "DN") == PA      # COALESCE(unit,'') → catch-all
    conn.close()


def test_upsert_mapping_composite_conflict(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.commit()
    conn.close()
    models.upsert_mapping(CODE, "n1", product_id=PA, bsn_unit="แผง")
    models.upsert_mapping(CODE, "n2", product_id=PB, bsn_unit="แผง")  # update
    models.upsert_mapping(CODE, "n3", product_id=PA, bsn_unit="ตัว")  # new row
    c = sqlite3.connect(tmp_db)
    rows = c.execute("SELECT bsn_unit,product_id,bsn_name FROM "
                     "product_code_mapping WHERE bsn_code=? ORDER BY bsn_unit",
                     (CODE,)).fetchall()
    assert rows == [("ตัว", PA, "n3"), ("แผง", PB, "n2")]
    c.close()


def test_get_mapping_unit_arg(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _seed(conn)
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "ca", PA))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'แผง')", (CODE, "ov", PB))
    conn.commit()
    conn.close()
    assert models.get_mapping(CODE, "แผง")["product_id"] == PB
    assert models.get_mapping(CODE, "ตัว")["product_id"] == PA   # → catch-all
    assert models.get_mapping(CODE)["product_id"] == PA          # legacy
