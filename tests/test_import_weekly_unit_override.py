"""import_weekly resolves product_id via bsn_code (+ unit) mapping.

A single (bsn_code, bsn_unit='') mapping row is the non-split catch-all: ALL
units of that code route to the same product (mig 124 restored bsn_unit, but
these test codes were never split, so behavior is unchanged from mig-112).
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

MIG_124 = os.path.join(REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")

PA, PB = 907101, 907102
CODE = "ZIMP100"


def _migrate124(conn):
    with open(MIG_124, encoding="utf-8") as f:
        conn.executescript(f.read())


def _entry(code, unit, doc):
    return {"date_iso": "2026-05-09", "doc_no": doc,
            "product_code_raw": code, "product_name_raw": "P",
            "party": "S", "party_code": "S1", "qty": 0.0, "unit": unit,
            "unit_price": 10.0, "vat_type": 0, "discount": 0,
            "total": 0.0, "net": 0.0}


def test_import_routes_all_units_to_single_product(tmp_db, monkeypatch, patch_models_conn):
    """All units of a bsn_code map to the single mapped product (no unit override)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate124(conn)
    for pid in (PA, PB):
        conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (pid, f"P{pid}", f"SK{pid}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id) "
                 "VALUES (?,?,?)", (CODE, "n", PA))
    conn.commit()
    conn.close()

    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    tconn.execute("PRAGMA foreign_keys = ON")
    patch_models_conn(lambda: tconn)

    models.import_weekly([_entry(CODE, "แผง", "RRO1"),
                          _entry(CODE, "ตัว", "RRO2")],
                         "purchase", "ov.csv")

    c = sqlite3.connect(tmp_db)
    got = {r[0]: r[1] for r in c.execute(
        "SELECT doc_no, product_id FROM purchase_transactions "
        "WHERE bsn_code=?", (CODE,))}
    assert got["RRO1"] == PA        # แผง → single mapped product
    assert got["RRO2"] == PA        # ตัว → same single mapped product
    c.close()


def test_non_mapped_code_stays_null(tmp_db, monkeypatch, patch_models_conn):
    """A code with no mapping row results in NULL product_id."""
    conn = sqlite3.connect(tmp_db)
    _migrate124(conn)
    conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (PA, "P", f"SK{PA}"))
    conn.commit()
    conn.close()

    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    patch_models_conn(lambda: tconn)

    models.import_weekly([_entry(CODE, "แผง", "RRN1"),
                          _entry(CODE, "ตัว", "RRN2")],
                         "purchase", "nrm.csv")

    c = sqlite3.connect(tmp_db)
    pids = {r[0] for r in c.execute(
        "SELECT product_id FROM purchase_transactions WHERE bsn_code=?",
        (CODE,))}
    assert pids == {None}           # no mapping → NULL product_id
    c.close()
