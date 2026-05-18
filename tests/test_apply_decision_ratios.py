"""Tests for scripts/apply_decision_ratios.py (synthetic pids).

numeric decision + blank/'-' remap → upsert unit_conversions.ratio;
existing row UPDATEd, absent → INSERTed; already-correct = noop;
rows with a remap target or free-text decision are NOT touched here;
dry-run writes nothing.
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apply_decision_ratios as ar  # noqa: E402

UPD, INS, NOOP, RMP = 904001, 904002, 904003, 904004
COLS = ["product_id", "sku", "sku_code", "product_name", "unit",
        "number_of_stock", "bsn_code", "bsn_name", "bsn_unit",
        "stock_conversion_ratio", "ratio_suggestion", ar.DEC, ar.REMAP]


def _p(c, pid):
    c.execute("INSERT INTO products (id,sku,product_name,unit_type,sku_code,"
              "is_active) VALUES (?,?,?,'ตัว',?,1)", (pid, pid, f"P{pid}",
                                                      f"S{pid}"))


def _u(c, pid, unit, r):
    c.execute("INSERT INTO unit_conversions (product_id,bsn_unit,ratio) "
              "VALUES (?,?,?)", (pid, unit, r))


def _csv(tmp, rows):
    p = tmp / "r.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([r.get(c, "") for c in COLS])
    return str(p)


def test_apply_ratios(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, UPD); _u(conn, UPD, "โหล", 1)        # will UPDATE 1→12
    _p(conn, INS)                                  # no conv → INSERT 24
    _p(conn, NOOP); _u(conn, NOOP, "กล่อง", 100)   # already 100 → noop
    _p(conn, RMP); _u(conn, RMP, "ตัว", 1)         # remap target → skip
    conn.commit()
    conn.close()

    csvf = _csv(tmp_path, [
        {"product_id": UPD, "bsn_unit": "โหล", "decision": "12"},
        {"product_id": INS, "bsn_unit": "ลัง", "decision": "24"},
        {"product_id": NOOP, "bsn_unit": "กล่อง", "decision": "100"},
        {"product_id": RMP, "bsn_unit": "ตัว", "decision": "",
         ar.REMAP: "SomeProduct (ตัว)"},          # has target → skip
    ])
    assert ar.main([csvf, "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def one(sql, *a):
        return conn.execute(sql, a).fetchone()

    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='โหล'", UPD)[0] == 12
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='ลัง'", INS)[0] == 24
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='กล่อง'", NOOP)[0] == 100
    # remap-target row untouched (still its original 1, not changed by B)
    assert one("SELECT COUNT(*) FROM unit_conversions WHERE product_id=?",
               RMP)[0] == 1
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=?",
               RMP)[0] == 1
    conn.close()


def test_dry_run_writes_nothing(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 904101); _u(conn, 904101, "โหล", 1)
    conn.commit()
    conn.close()
    csvf = _csv(tmp_path, [{"product_id": 904101, "bsn_unit": "โหล",
                            "decision": "12"}])
    assert ar.main([csvf, "--db", tmp_db]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT ratio FROM unit_conversions WHERE "
                        "product_id=904101").fetchone()[0] == 1
    conn.close()
