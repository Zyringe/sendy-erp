"""Part B: /unit-conversions learns unknown acronyms.

- bsn_units.is_known / normalize_unit / add_acronym round-trip
- get_pending_unit_conversions flags is_acronym for unknown units only
- models.learn_acronyms_normalize persists to JSON + rewrites ledger
Tests monkeypatch the JSON path to a tmp copy so the real
bsn_unit_full.json is never mutated.
"""
import json
import os
import shutil
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import bsn_units  # noqa: E402
import models  # noqa: E402

PID = 906301


def _tmp_json(tmp_path, monkeypatch):
    dst = tmp_path / "bsn_unit_full.json"
    shutil.copy(bsn_units.map_path(), dst)
    monkeypatch.setattr(bsn_units, "_MAP_PATH", str(dst))
    return dst


def test_helpers_roundtrip(tmp_path, monkeypatch):
    _tmp_json(tmp_path, monkeypatch)
    assert bsn_units.is_known("โหล") and bsn_units.is_known("หล")
    assert not bsn_units.is_known("Zx9")
    assert bsn_units.normalize_unit("หล") == "โหล"
    assert bsn_units.normalize_unit("Zx9") == "Zx9"      # unknown kept
    bsn_units.add_acronym("Zx9", "หน่วยใหม่")
    assert bsn_units.normalize_unit("Zx9") == "หน่วยใหม่"
    assert bsn_units.is_known("Zx9")
    # real bsn_unit_full.json must NOT be polluted by the test
    real = os.path.join(REPO, "data", "reference", "bsn_unit_full.json")
    assert "Zx9" not in json.load(open(real, encoding="utf-8"))["map"]


def test_pending_is_acronym_flag(tmp_db, tmp_path, monkeypatch):
    _tmp_json(tmp_path, monkeypatch)
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (PID, "P", f"SK{PID}"))
    for u in ("Zx9", "โหล"):                 # unknown acronym vs known full
        conn.execute(
            "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
            "doc_base,product_id,bsn_code,product_name_raw,customer,"
            "customer_code,qty,unit,unit_price,vat_type,discount,total,"
            "net,synced_to_stock) VALUES (0,'2026-05-09','D'||?,'D'||?,?,"
            "'C'||?,'r','C','C1',1,?,1,0,0,0,0,0)", (u, u, PID, u, u))
    conn.commit()
    conn.close()
    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    monkeypatch.setattr(models, "get_connection", lambda: tconn)
    pend = {p["bsn_unit"]: p["is_acronym"]
            for p in models.get_pending_unit_conversions()
            if p["product_id"] == PID}
    assert pend.get("Zx9") is True            # unknown → flagged
    assert pend.get("โหล") is False           # known full → not flagged


def test_learn_acronyms_normalize(tmp_db, tmp_path, monkeypatch):
    _tmp_json(tmp_path, monkeypatch)
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (PID + 1, "P", f"SK{PID+1}"))
    for t in ("sales_transactions", "purchase_transactions"):
        party = "customer" if t == "sales_transactions" else "supplier"
        pc = "customer_code" if t == "sales_transactions" else "supplier_code"
        conn.execute(
            f"INSERT INTO {t} (batch_id,date_iso,doc_no,doc_base,"
            f"product_id,bsn_code,product_name_raw,{party},{pc},qty,unit,"
            f"unit_price,vat_type,discount,total,net,synced_to_stock) "
            f"VALUES (0,'2026-05-09','D','D',?,'C','r','X','X1',1,'Qq9',"
            f"1,0,0,0,0,0)", (PID + 1,))
    conn.commit()
    conn.close()
    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    monkeypatch.setattr(models, "get_connection", lambda: tconn)

    models.learn_acronyms_normalize({"Qq9": "กระป๋องใหม่"})

    assert bsn_units.normalize_unit("Qq9") == "กระป๋องใหม่"   # JSON learned
    c = sqlite3.connect(tmp_db)
    for t in ("sales_transactions", "purchase_transactions"):
        u = c.execute(f"SELECT unit FROM {t} WHERE product_id=?",
                      (PID + 1,)).fetchone()[0]
        assert u == "กระป๋องใหม่"                              # ledger rewritten
    c.close()
