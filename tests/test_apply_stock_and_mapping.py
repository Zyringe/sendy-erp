"""Tests for scripts/apply_stock_and_mapping_csv.py.

Regression focus: the BSN ledger stores the unit ACRONYM but reviewed
unit_conversions are keyed by full Thai, and _get_base_qty matches
unit_conversions.bsn_unit == ledger.unit. The first apply (2026-05-18)
silently used stale/old acronym conv rows (or skipped sync) → wrong stock.
The fix normalises ledger units acronym→full Thai for affected products and
drops superseded acronym conv rows. These tests would have caught it.
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apply_stock_and_mapping_csv as app  # noqa: E402


def _seed(conn, pid, sku, base_unit):
    conn.execute(
        "INSERT INTO products (id,sku,product_name,unit_type,sku_code,is_active) "
        "VALUES (?,?,?,?,?,1)",
        (pid, sku, f"OLD NAME {pid}", base_unit, f"OLD-{pid}"))
    conn.execute("INSERT INTO stock_levels(product_id,quantity) VALUES(?,0)", (pid,))


def _bsn_sale(conn, pid, code, unit, qty, doc):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        " customer,customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
        " synced_to_stock) VALUES "
        "('t','2025-01-01',?,?,?,?,'raw','C','C1',?,?,10,0,0,0,0,0)",
        (doc, doc, pid, code, qty, unit))


def _csv(tmp_path, rows):
    p = tmp_path / "m.csv"
    cols = ["Checked", "product_id", "sku", "sku_code", "product_name",
            "old_product_name", "base_unit", "stock", "bsn_code", "bsn_name",
            "bsn_unit", "ratio_to_base"]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    return str(p)


def test_ledger_unit_normalized_and_ratio_applied(tmp_db, tmp_path):
    """หล (acronym) in ledger → conv keyed โหล=12; ledger unit becomes โหล;
    no leftover หล conv; stock == qty*12 == SUM(transactions)."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, 950001, 950001, "แผ่น")
    # stale OLD acronym conv that the buggy version would have used (ratio 1)
    conn.execute("INSERT INTO unit_conversions(product_id,bsn_unit,ratio) "
                 "VALUES (950001,'หล',1)")
    _bsn_sale(conn, 950001, "ZZ950001", "หล", 3, "DOC9501")
    conn.commit(); conn.close()

    csvf = _csv(tmp_path, [{
        "Checked": "TRUE", "product_id": "950001", "sku": "950001",
        "sku_code": "NEW-950001", "product_name": "NEW NAME 950001",
        "base_unit": "แผ่น", "bsn_code": "ZZ950001", "bsn_name": "bn",
        "bsn_unit": "หล", "ratio_to_base": "12"}])
    assert app.main([csvf, "--db", tmp_db, "--apply"]) == 0

    conn = sqlite3.connect(tmp_db)
    units = {r[0] for r in conn.execute(
        "SELECT bsn_unit FROM unit_conversions WHERE product_id=950001")}
    assert "โหล" in units and "หล" not in units          # normalized + deduped
    led = {r[0] for r in conn.execute(
        "SELECT DISTINCT unit FROM sales_transactions WHERE product_id=950001")}
    assert led == {"โหล"}                                  # ledger normalized
    sl = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=950001"
                       ).fetchone()[0]
    sm = conn.execute("SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
                      "WHERE product_id=950001").fetchone()[0]
    # sales → OUT → negative; ratio 12 applied (3 หล × 12 = 36 แผ่น out)
    assert sl == sm == -(3 * 12)
    nm = conn.execute("SELECT sku_code,product_name FROM products WHERE id=950001"
                      ).fetchone()
    assert nm[0] == "NEW-950001" and nm[1] == "NEW NAME 950001"
    conn.close()


def test_forced_base_equals_1_overrides_csv(tmp_db, tmp_path):
    """bsn_unit maps to product base unit → ratio forced 1 even if CSV≠1."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, 950002, 950002, "อัน")        # base = อัน
    _bsn_sale(conn, 950002, "ZZ950002", "อน", 5, "DOC9502")  # อน→อัน == base
    conn.commit(); conn.close()
    csvf = _csv(tmp_path, [{
        "Checked": "TRUE", "product_id": "950002", "sku": "950002",
        "sku_code": "S2", "product_name": "P2", "base_unit": "อัน",
        "bsn_code": "ZZ950002", "bsn_name": "b", "bsn_unit": "อน",
        "ratio_to_base": "30"}])                # CSV says 30 — must be ignored
    assert app.main([csvf, "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)
    sm = conn.execute("SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
                      "WHERE product_id=950002").fetchone()[0]
    assert sm == -5                                         # 5*1 (not 5*30)
    conn.close()


def test_unknown_acronym_skips_conversion_keeps_mapping(tmp_db, tmp_path):
    """UNKNOWN acronym (ปน) → no conversion, but mapping + sku/name applied,
    ledger unit left untouched."""
    conn = sqlite3.connect(tmp_db)
    _seed(conn, 950003, 950003, "ตัว")
    _bsn_sale(conn, 950003, "ZZ950003", "ปน", 2, "DOC9503")
    conn.commit(); conn.close()
    csvf = _csv(tmp_path, [{
        "Checked": "TRUE", "product_id": "950003", "sku": "950003",
        "sku_code": "S3", "product_name": "P3", "base_unit": "ตัว",
        "bsn_code": "ZZ950003", "bsn_name": "b", "bsn_unit": "ปน",
        "ratio_to_base": ""}])
    assert app.main([csvf, "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM unit_conversions WHERE product_id=950003"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT DISTINCT unit FROM sales_transactions "
                        "WHERE product_id=950003").fetchone()[0] == "ปน"
    assert conn.execute("SELECT product_id FROM product_code_mapping "
                        "WHERE bsn_code='ZZ950003'").fetchone()[0] == 950003
    assert conn.execute("SELECT sku_code FROM products WHERE id=950003"
                        ).fetchone()[0] == "S3"
    conn.close()


def test_false_and_done_rows_untouched(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _seed(conn, 950004, 950004, "ตัว")
    conn.commit(); conn.close()
    csvf = _csv(tmp_path, [
        {"Checked": "FALSE", "product_id": "950004", "sku": "950004",
         "sku_code": "X", "product_name": "X", "base_unit": "ตัว",
         "bsn_code": "ZZ950004", "bsn_name": "b", "bsn_unit": "อน",
         "ratio_to_base": "1"},
        {"Checked": "Done", "product_id": "950004", "sku": "950004",
         "sku_code": "Y", "product_name": "Y", "base_unit": "ตัว",
         "bsn_code": "ZZ950004b", "bsn_name": "b", "bsn_unit": "อน",
         "ratio_to_base": "1"}])
    assert app.main([csvf, "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)
    # untouched: original OLD name/sku, no mapping rows created
    nm = conn.execute("SELECT sku_code,product_name FROM products WHERE id=950004"
                      ).fetchone()
    assert nm[0] == "OLD-950004"
    assert conn.execute("SELECT COUNT(*) FROM product_code_mapping "
                        "WHERE bsn_code LIKE 'ZZ950004%'").fetchone()[0] == 0
    conn.close()
