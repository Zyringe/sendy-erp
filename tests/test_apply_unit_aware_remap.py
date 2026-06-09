"""apply_unit_aware_remap.py:
- dry-run writes nothing
- --apply writes the (code,unit) override row, moves the ledger off the
  wrong product onto the target, re-syncs, conserves total stock, and
  leaves non-CSV codes byte-identical
"""
import csv
import importlib.util
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
_spec = importlib.util.spec_from_file_location(
    "aur", os.path.join(REPO, "scripts", "apply_unit_aware_remap.py"))
aur = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aur)

PA, PB, PC = 907401, 907402, 907403          # แผง / ตัว / control
CODE, CTRL = "ZAP100", "ZCTRL9"


def _setup(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'แผง', ?, 1)", (PA, "PA", f"S{PA}"))
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (PB, "PB", f"S{PB}"))
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'ตัว', ?, 1)", (PC, "PC", f"S{PC}"))
    c.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
              "product_id,bsn_unit) VALUES (?,?,?,'')", (CODE, "n", PA))
    c.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
              "product_id,bsn_unit) VALUES (?,?,?,'')", (CTRL, "c", PC))

    def sale(doc, unit, qty, pid):
        c.execute(
            "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
            "doc_base,product_id,bsn_code,product_name_raw,customer,"
            "customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
            "synced_to_stock) VALUES (1,'2026-05-09',?,?,?,?,'r','C','C1',"
            "?,?,1,0,0,0,0,1)", (doc, doc, pid, CODE if pid != PC else CTRL,
                                 qty, unit))

    sale("DS1", "แผง", 3, PA)        # stays on PA
    sale("DS2", "ตัว", 5, PA)        # → must move to PB
    c.execute(  # control row, must be untouched
        "INSERT INTO sales_transactions (batch_id,date_iso,doc_no,doc_base,"
        "product_id,bsn_code,product_name_raw,customer,customer_code,qty,"
        "unit,unit_price,vat_type,discount,total,net,synced_to_stock) "
        "VALUES (1,'2026-05-09','DC1','DC1',?,?,'r','C','C1',2,'ตัว',1,0,0,"
        "0,0,1)", (PC, CTRL))

    def txn(pid, qty, ref):
        c.execute("INSERT INTO transactions (product_id,txn_type,"
                  "quantity_change,unit_mode,reference_no,note,created_at) "
                  "VALUES (?,'OUT',?,'unit',?,'BSN ขาย','2026-05-09 00:00:00')",
                  (pid, qty, ref))
    txn(PA, -3, "DS1")               # synced ledger for แผง line
    txn(PA, -5, "DS2")               # synced ledger for ตัว line (wrong pid)
    txn(PC, -2, "DC1")               # control ledger
    # stock_levels is maintained by the after_transaction_insert trigger:
    # PA=-8, PC=-2; PB has no ledger row → treated as 0.
    c.commit()
    c.close()


def _csv(tmp_path):
    p = tmp_path / "reviewed.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["bsn_code", "override_unit",
                                          "override_product_id"])
        w.writeheader()
        w.writerow({"bsn_code": CODE, "override_unit": "ตัว",
                    "override_product_id": PB})
    return str(p)


def test_dry_run_writes_nothing(tmp_db, tmp_path):
    _setup(tmp_db)
    rc = aur.main(["--csv", _csv(tmp_path), "--db", tmp_db])
    assert rc == 0
    c = sqlite3.connect(tmp_db)
    # no override row created, S2 still on PA
    assert c.execute("SELECT COUNT(*) FROM product_code_mapping WHERE "
                     "bsn_code=? AND bsn_unit='ตัว'", (CODE,)).fetchone()[0] == 0
    assert c.execute("SELECT product_id FROM sales_transactions WHERE "
                     "doc_no='DS2'").fetchone()[0] == PA
    c.close()


def test_apply_moves_ledger_conserves_stock_and_isolates(tmp_db, tmp_path):
    _setup(tmp_db)
    rc = aur.main(["--csv", _csv(tmp_path), "--db", tmp_db, "--apply"])
    assert rc == 0
    c = sqlite3.connect(tmp_db)

    # override row written, catch-all untouched
    assert c.execute("SELECT product_id FROM product_code_mapping WHERE "
                     "bsn_code=? AND bsn_unit='ตัว'", (CODE,)
                     ).fetchone()[0] == PB
    assert c.execute("SELECT product_id FROM product_code_mapping WHERE "
                     "bsn_code=? AND bsn_unit=''", (CODE,)
                     ).fetchone()[0] == PA
    # ledger moved: ตัว line now on PB & re-synced, แผง line still PA
    assert c.execute("SELECT product_id,synced_to_stock FROM "
                     "sales_transactions WHERE doc_no='DS2'"
                     ).fetchone() == (PB, 1)
    assert c.execute("SELECT product_id FROM sales_transactions WHERE "
                     "doc_no='DS1'").fetchone()[0] == PA
    sPA = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                    (PA,)).fetchone()[0]
    sPB = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                    (PB,)).fetchone()[0]
    assert sPA == -3 and sPB == -5            # split correctly
    assert sPA + sPB == -8                    # conserved (was PA -8, PB 0)

    # control code fully untouched
    assert c.execute("SELECT product_id FROM sales_transactions WHERE "
                     "doc_no='DC1'").fetchone()[0] == PC
    assert c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                     (PC,)).fetchone()[0] == -2
    assert c.execute("SELECT COUNT(*) FROM product_code_mapping WHERE "
                     "bsn_code=?", (CTRL,)).fetchone()[0] == 1
    c.close()
