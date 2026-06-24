"""export_multiunit_candidates.py: lists only codes with >1 sold unit;
read-only (no DB mutation)."""
import importlib.util
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
_spec = importlib.util.spec_from_file_location(
    "exp_mu", os.path.join(REPO, "scripts",
                           "export_multiunit_candidates.py"))
exp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exp)

PA, PB = 907301, 907302
MULTI, SINGLE = "ZMU100", "ZSU100"


def _sale(conn, code, unit, doc):
    conn.execute(
        "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,doc_base,"
        "product_id,bsn_code,product_name_raw,customer,customer_code,qty,"
        "unit,unit_price,vat_type,discount,total,net,synced_to_stock) "
        "VALUES (0,'2026-05-09',?,?,?,?,'r','C','C1',1,?,1,0,0,0,0,1)",
        (doc, doc, PA, code, unit))


def test_lists_only_multiunit_codes_readonly(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'แผง', ?, 1)", (PA, "PA", f"SK{PA}"))
    conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (PB, "PB", f"SK{PB}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id) VALUES (?,?,?)", (MULTI, "mn", PA))
    _sale(conn, MULTI, "แผง", "DM1")
    _sale(conn, MULTI, "ตัว", "DM2")
    _sale(conn, SINGLE, "ตัว", "DS1")
    conn.commit()

    n_pcm = conn.execute(
        "SELECT COUNT(*) FROM product_code_mapping").fetchone()[0]
    n_sale = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions").fetchone()[0]

    rows = exp.build_rows(sqlite3.connect(tmp_db))
    by_code = {r["bsn_code"]: r for r in rows}

    assert MULTI in by_code
    assert SINGLE not in by_code               # single-unit excluded
    r = by_code[MULTI]
    assert set(r["distinct_units"].split("|")) == {"แผง", "ตัว"}
    assert r["current_mapped_pid"] == PA
    # siblings listed grouped per distinct unit
    assert "[ตัว]" in r["sibling_skus_by_unit"]
    assert "[แผง]" in r["sibling_skus_by_unit"]
    assert r["override_unit"] == "" and r["override_product_id"] == ""

    # read-only: nothing mutated
    assert conn.execute(
        "SELECT COUNT(*) FROM product_code_mapping").fetchone()[0] == n_pcm
    assert conn.execute(
        "SELECT COUNT(*) FROM sales_transactions").fetchone()[0] == n_sale
    conn.close()
