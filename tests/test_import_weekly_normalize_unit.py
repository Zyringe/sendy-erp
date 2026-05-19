"""Part A: models.import_weekly must auto-normalise the BSN unit
acronym → full Thai on insert (known acronym converted; unknown left
as-is so it surfaces on /unit-conversions)."""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402
import bsn_units  # noqa: E402

PID = 906201


def _entry(code, unit):
    return {"date_iso": "2026-05-09", "doc_no": "RRNORM" + code,
            "product_code_raw": code, "product_name_raw": "P",
            "party": "S", "party_code": "S1", "qty": 1.0, "unit": unit,
            "unit_price": 10.0, "vat_type": 0, "discount": 0,
            "total": 10.0, "net": 10.0}


def test_known_acronym_normalized_unknown_kept(tmp_db, monkeypatch):
    # sanity: the live map really has หล→โหล and no 'ZZ' acronym
    assert bsn_units.normalize_unit("หล") == "โหล"
    assert bsn_units.normalize_unit("ZZ") == "ZZ"

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO products (id,sku,product_name,unit_type,"
                 "sku_code,is_active) VALUES (?,?,?,?,?,1)",
                 (PID, PID, "P", "ตัว", f"SK{PID}"))
    for code in ("CNORMK", "CNORMU"):
        conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                     "product_id,is_ignored) VALUES (?,?,?,0)",
                     (code, "P", PID))
    conn.commit()
    conn.close()

    tconn = sqlite3.connect(tmp_db)
    tconn.row_factory = sqlite3.Row
    tconn.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(models, "get_connection", lambda: tconn)

    models.import_weekly([_entry("CNORMK", "หล"),        # known → โหล
                          _entry("CNORMU", "ผป")],       # unknown → kept
                         "purchase", "norm.csv")

    c = sqlite3.connect(tmp_db)
    units = {r[0]: r[1] for r in c.execute(
        "SELECT bsn_code, unit FROM purchase_transactions WHERE "
        "product_id=?", (PID,))}
    assert units["CNORMK"] == "โหล"        # acronym normalised on import
    assert units["CNORMU"] == "ผป"         # unknown acronym preserved
    c.close()
