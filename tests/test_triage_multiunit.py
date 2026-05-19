"""triage_multiunit_candidates.classify:
- bulk unit (โหล) → RATIO
- same-name sibling in the other unit → SUGGEST (override pre-filled)
- tiny minority count vs dominant → NOISE
- read-only (no DB mutation)
"""
import importlib.util
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
_spec = importlib.util.spec_from_file_location(
    "triage", os.path.join(REPO, "scripts",
                           "triage_multiunit_candidates.py"))
tri = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tri)

PA, PB = 907501, 907502          # "X (แผง)"  and its "X (ตัว)" sibling
PN = 907503                      # NOISE-case product (no sibling)
RA, BU, NO = "ZTRA1", "ZTRB1", "ZTRN1"


def _sale(c, code, unit, qty, n=1, pid=PA):
    for i in range(n):
        c.execute(
            "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
            "doc_base,product_id,bsn_code,product_name_raw,customer,"
            "customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
            "synced_to_stock) VALUES (0,'2026-05-09',?,?,?,?,'r','C','C1',?,"
            "?,1,0,0,0,0,1)",
            (f"{code}{unit}{i}", f"{code}{unit}{i}", pid, code, qty, unit))


def test_triage_classifies(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,'แผง',?,1)",
              (PA, PA, "ของทดสอบ รุ่นA (แผง)", f"S{PA}"))
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,'ตัว',?,1)",
              (PB, PB, "ของทดสอบ รุ่นA (ตัว)", f"S{PB}"))
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,'แผง',?,1)",
              (PN, PN, "เฉพาะกิจไม่มีพี่น้อง (แผง)", f"S{PN}"))
    for code, pid in ((RA, PA), (BU, PA), (NO, PN)):
        c.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
                  "product_id,bsn_unit) VALUES (?,?,?,'')",
                  (code, "n", pid))
    # RA: แผง (many) + ตัว (many) with a real (ตัว) sibling → SUGGEST
    _sale(c, RA, "แผง", 1, 6)
    _sale(c, RA, "ตัว", 1, 6)
    # BU: แผง + โหล (bulk) → RATIO
    _sale(c, BU, "แผง", 1, 6)
    _sale(c, BU, "โหล", 1, 6)
    # NO: แผง (dominant 20) + ตัว (1), product PN has no (ตัว) sibling → NOISE
    _sale(c, NO, "แผง", 1, 20, pid=PN)
    _sale(c, NO, "ตัว", 1, 1, pid=PN)
    c.commit()
    c.close()

    rows, cat = tri.classify(sqlite3.connect(tmp_db))
    by = {(r["bsn_code"], r["override_unit"] or r["all_units"]): r
          for r in rows}

    ra = [r for r in rows if r["bsn_code"] == RA][0]
    assert ra["category"] == "SUGGEST"
    assert ra["override_unit"] == "ตัว"
    assert int(ra["override_product_id"]) == PB

    bu = [r for r in rows if r["bsn_code"] == BU and "โหล" in r["reason"]
          or (r["bsn_code"] == BU)][0]
    assert bu["category"] == "RATIO"

    no = [r for r in rows if r["bsn_code"] == NO][0]
    assert no["category"] == "NOISE"
    assert no["override_product_id"] == ""        # nothing pre-filled

    # read-only
    cc = sqlite3.connect(tmp_db)
    assert cc.execute("SELECT COUNT(*) FROM product_code_mapping WHERE "
                      "bsn_code IN (?,?,?)", (RA, BU, NO)).fetchone()[0] == 3
    cc.close()
