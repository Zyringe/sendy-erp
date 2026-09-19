"""Part B: /unit-conversions learns unknown acronyms.

- bsn_units.is_known / normalize_unit / add_acronym round-trip against the
  unit_map DB table (#596 — no more JSON file to monkeypatch)
- get_pending_unit_conversions flags is_acronym for unknown units only
- models.learn_acronyms_normalize persists to unit_map + rewrites the ledger
"""
import os
import sqlite3
import sys
from pathlib import Path

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import bsn_units  # noqa: E402
import models  # noqa: E402

PID = 906301

_FORWARD_MIG = Path(REPO) / "data" / "migrations" / "185_unit_map_table.sql"


def _ensure_migrated(conn):
    """Self-contained regardless of whether some earlier test in this
    session already migrated the DB `tmp_db` copied from (erp-engineering-
    discipline.md ordering trap). Resets the table first: 185 itself keeps
    existing rows, and the live DB may carry codes named on prod."""
    conn.executescript("DROP TABLE IF EXISTS unit_map;\n"
                       + _FORWARD_MIG.read_text(encoding="utf-8"))
    conn.commit()


def test_helpers_roundtrip(tmp_db_conn):
    _ensure_migrated(tmp_db_conn)
    assert bsn_units.is_known("โหล", conn=tmp_db_conn) and bsn_units.is_known("หล", conn=tmp_db_conn)
    assert not bsn_units.is_known("Zx9", conn=tmp_db_conn)
    assert bsn_units.normalize_unit("หล", conn=tmp_db_conn) == "โหล"
    assert bsn_units.normalize_unit("Zx9", conn=tmp_db_conn) == "Zx9"      # unknown kept
    bsn_units.add_acronym("Zx9", "หน่วยใหม่", conn=tmp_db_conn)
    assert bsn_units.normalize_unit("Zx9", conn=tmp_db_conn) == "หน่วยใหม่"
    assert bsn_units.is_known("Zx9", conn=tmp_db_conn)


def test_pending_is_acronym_flag(tmp_db, patch_models_conn):
    conn = sqlite3.connect(tmp_db)
    _ensure_migrated(conn)
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
    patch_models_conn(lambda: tconn)
    pend = {p["bsn_unit"]: p["is_acronym"]
            for p in models.get_pending_unit_conversions()
            if p["product_id"] == PID}
    assert pend.get("Zx9") is True            # unknown → flagged
    assert pend.get("โหล") is False           # known full → not flagged


def test_learn_acronyms_normalize(tmp_db, patch_models_conn):
    conn = sqlite3.connect(tmp_db)
    _ensure_migrated(conn)
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
    patch_models_conn(lambda: tconn)

    models.learn_acronyms_normalize({"Qq9": "กระป๋องใหม่"})

    # No conn= here on purpose: proves the write is on DISK (config.DATABASE_PATH,
    # which tmp_db already points at THIS file), readable from a brand-new
    # connection — not just visible on the connection that wrote it.
    assert bsn_units.normalize_unit("Qq9") == "กระป๋องใหม่"
    c = sqlite3.connect(tmp_db)
    for t in ("sales_transactions", "purchase_transactions"):
        u = c.execute(f"SELECT unit FROM {t} WHERE product_id=?",
                      (PID + 1,)).fetchone()[0]
        assert u == "กระป๋องใหม่"                              # ledger rewritten
    assert c.execute(
        "SELECT word FROM unit_map WHERE book='BSN5657' AND spelling='Qq9'"
    ).fetchone()[0] == "กระป๋องใหม่"                              # unit_map row landed
    c.close()
