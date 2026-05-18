"""Part: import_weekly resolves product_id unit-aware (mig 061).

- a per-unit override row routes a matching-unit entry to the override
  product; another unit with only a catch-all goes to the catch-all
- a code with ONLY a catch-all row imports exactly like pre-061 (regression)
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

PA, PB = 907101, 907102
CODE = "ZIMP100"


def _entry(code, unit, doc):
    return {"date_iso": "2026-05-09", "doc_no": doc,
            "product_code_raw": code, "product_name_raw": "P",
            "party": "S", "party_code": "S1", "qty": 0.0, "unit": unit,
            "unit_price": 10.0, "vat_type": 0, "discount": 0,
            "total": 0.0, "net": 0.0}


def test_import_routes_overridden_unit_to_override_product(tmp_db,
                                                           monkeypatch):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for pid in (PA, PB):
        conn.execute("INSERT INTO products (id,sku,product_name,unit_type,"
                     "sku_code,is_active) VALUES (?,?,?,'ตัว',?,1)",
                     (pid, pid, f"P{pid}", f"SK{pid}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'แผง')", (CODE, "n", PB))
    conn.commit()
    conn.close()

    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    tconn.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(models, "get_connection", lambda: tconn)

    models.import_weekly([_entry(CODE, "แผง", "RRO1"),
                          _entry(CODE, "ตัว", "RRO2")],
                         "purchase", "ov.csv")

    c = sqlite3.connect(tmp_db)
    got = {r[0]: r[1] for r in c.execute(
        "SELECT doc_no, product_id FROM purchase_transactions "
        "WHERE bsn_code=?", (CODE,))}
    assert got["RRO1"] == PB        # แผง → override product
    assert got["RRO2"] == PA        # ตัว → catch-all
    c.close()


def test_non_overridden_code_unchanged(tmp_db, monkeypatch):
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id,sku,product_name,unit_type,"
                 "sku_code,is_active) VALUES (?,?,?,'ตัว',?,1)",
                 (PA, PA, "P", f"SK{PA}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    conn.commit()
    conn.close()

    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    monkeypatch.setattr(models, "get_connection", lambda: tconn)

    models.import_weekly([_entry(CODE, "แผง", "RRN1"),
                          _entry(CODE, "ตัว", "RRN2")],
                         "purchase", "nrm.csv")

    c = sqlite3.connect(tmp_db)
    pids = {r[0] for r in c.execute(
        "SELECT product_id FROM purchase_transactions WHERE bsn_code=?",
        (CODE,))}
    assert pids == {PA}             # both → the single catch-all (legacy)
    c.close()
