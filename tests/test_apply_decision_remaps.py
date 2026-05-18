"""Tests for scripts/apply_decision_remaps.py (synthetic pids).

- existing target  → mapping+ledger repointed, unit_conversions=1
- missing target w/ sibling → new product cloned (fields overridden),
  then remapped
- missing target, no sibling → minimal product created + remapped
- dry-run writes nothing
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apply_decision_remaps as dr  # noqa: E402

OLD, EXIST, SIB = 904501, 904502, 904503
COLS = ["product_id", "sku", "sku_code", "product_name", "unit",
        "number_of_stock", "bsn_code", "bsn_name", "bsn_unit",
        "stock_conversion_ratio", "ratio_suggestion", dr.DEC, dr.REMAP]


def _p(c, pid, name, sku_code=None, pkg="แผง"):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,"
              "packaging,sku_code,is_active) VALUES (?,?,?,'ตัว',?,?,1)",
              (pid, pid, name, pkg, sku_code or f"SC-{pid}"))


def _s(c, pid, code):
    c.execute("INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
              "doc_base,product_id,bsn_code,product_name_raw,customer,"
              "customer_code,qty,unit,unit_price,vat_type,discount,total,"
              "net,synced_to_stock) VALUES ('t','2025-01-01','D','D',?,?,"
              "'r','C','C1',1,'ตัว',1,0,0,0,0,1)", (pid, code))


def _csv(tmp, rows):
    p = tmp / "r.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([r.get(c, "") for c in COLS])
    return str(p)


def test_remaps(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, EXIST, "บานพับ TT (ตัว)")
    _p(conn, OLD, "OLD A")
    _s(conn, OLD, "C1EXIST")
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES ('C1EXIST','n',?,0)", (OLD,))
    _p(conn, SIB, "มือจับ ZZ (แผง)", sku_code="HDL-ZZ-PN", pkg="แผง")
    _s(conn, OLD, "C2NEW")
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES ('C2NEW','n',?,0)", (OLD,))
    _s(conn, OLD, "C3MIN")
    conn.commit()
    conn.close()

    csvf = _csv(tmp_path, [
        {"bsn_code": "C1EXIST", "bsn_unit": "โหล", dr.DEC: "",
         dr.REMAP: "บานพับ TT (ตัว)"},
        {"bsn_code": "C2NEW", "bsn_unit": "โหล", dr.DEC: "-",
         dr.REMAP: "มือจับ ZZ (ตัว)"},                 # sibling exists
        {"bsn_code": "C3MIN", "bsn_unit": "อัน", dr.DEC: "",
         dr.REMAP: "ของใหม่ไม่มีพี่น้อง (แผง)"},        # no sibling
    ])
    assert dr.main([csvf, "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def one(sql, *a):
        return conn.execute(sql, a).fetchone()

    # existing target
    assert one("SELECT product_id FROM product_code_mapping WHERE "
               "bsn_code='C1EXIST'")[0] == EXIST
    assert one("SELECT product_id FROM sales_transactions WHERE "
               "bsn_code='C1EXIST'")[0] == EXIST
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='โหล'", EXIST)[0] == 1

    # cloned-from-sibling new product
    nid = one("SELECT id FROM products WHERE product_name='มือจับ ZZ (ตัว)'")
    assert nid is not None
    nid = nid[0]
    sc = one("SELECT sku_code,packaging FROM products WHERE id=?", nid)
    assert sc[0] == "HDL-ZZ-UN" and sc[1] == "ตัว"     # suffix swapped
    assert one("SELECT product_id FROM product_code_mapping WHERE "
               "bsn_code='C2NEW'")[0] == nid

    # minimal new product
    mid = one("SELECT id,unit_type FROM products WHERE "
              "product_name='ของใหม่ไม่มีพี่น้อง (แผง)'")
    assert mid is not None and mid[1] == "แผง"
    assert one("SELECT product_id FROM product_code_mapping WHERE "
               "bsn_code='C3MIN'")[0] == mid[0]
    conn.close()


def test_dry_run_writes_nothing(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 904601, "T (ตัว)")
    _p(conn, 904602, "O")
    _s(conn, 904602, "CDRY")
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                 "product_id,is_ignored) VALUES ('CDRY','n',904602,0)")
    conn.commit()
    conn.close()
    csvf = _csv(tmp_path, [{"bsn_code": "CDRY", "bsn_unit": "โหล",
                            dr.DEC: "", dr.REMAP: "T (ตัว)"}])
    assert dr.main([csvf, "--db", tmp_db]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT product_id FROM product_code_mapping WHERE "
                        "bsn_code='CDRY'").fetchone()[0] == 904602
    conn.close()
