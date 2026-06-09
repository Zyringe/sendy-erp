"""Tests for scripts/apply_unit_type_change.py (synthetic pids).

D1: unit_type changed; transactions ×N; stock = old×N; per-bsn_unit ratio
set; multi-row product gets all its ratios. dry-run writes nothing;
--dump-d2 emits the fill-in CSV without touching the DB.
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import apply_unit_type_change as uc  # noqa: E402

P1, P2 = 905001, 905002
COLS = ["product_id", "sku", "sku_code", "product_name", "unit",
        "number_of_stock", "bsn_code", "bsn_name", "bsn_unit",
        "stock_conversion_ratio", "ratio_suggestion", uc.DEC,
        "change mapping of particular bsn_name and bsn_unit to following "
        "product (with conversion = 1)"]


def _p(c, pid, ut):
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, ?, ?, 1)", (pid, f"P{pid}", ut, f"S{pid}"))


def _t(c, pid, q):
    c.execute("INSERT INTO transactions (product_id,txn_type,quantity_change,"
              "unit_mode,note,created_at) VALUES (?,'ADJUST',?,'unit','x',"
              "'2025-01-01 00:00:00')", (pid, q))


def _csv(tmp, rows):
    p = tmp / "r.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([r.get(c, "") for c in COLS])
    return str(p)


D1A = ("change the unit from โหล to อัน then convert old stock by x12. "
       "Leave the bsn's ratio suggestion at 1")
D1B = ("change the unit from โหลคู่ to อัน then convert old stock by x24. "
       "Leave the bsn's ratio suggestion at 12")


def test_d1(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, P1, "โหล"); _t(conn, P1, 2)             # 2 → ×12 = 24
    _p(conn, P2, "โหลคู่"); _t(conn, P2, 1); _t(conn, P2, 1)   # 2 → ×24 =48
    conn.commit()
    conn.close()
    csvf = _csv(tmp_path, [
        {"product_id": P1, "bsn_unit": "อัน", uc.DEC: D1A},
        {"product_id": P2, "bsn_unit": "ตัว", uc.DEC: D1B},
        {"product_id": P2, "bsn_unit": "โหล", uc.DEC: D1B},   # 2nd ratio
    ])
    assert uc.main([csvf, "--db", tmp_db, "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def one(s, *a):
        return conn.execute(s, a).fetchone()[0]

    assert one("SELECT unit_type FROM products WHERE id=?", P1) == "อัน"
    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               P1) == 24
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='อัน'", P1) == 1
    assert one("SELECT unit_type FROM products WHERE id=?", P2) == "อัน"
    assert one("SELECT quantity FROM stock_levels WHERE product_id=?",
               P2) == 48
    # P2 carries both its bsn_unit ratios (ตัว & โหล both → 12)
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='ตัว'", P2) == 12
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=? AND "
               "bsn_unit='โหล'", P2) == 12
    conn.close()


def test_dump_d2_no_writes(tmp_db, tmp_path, monkeypatch):
    # write the dump into tmp_path, not the real data/exports dir
    monkeypatch.setattr(uc, "EXPORTS", tmp_path)
    conn = sqlite3.connect(tmp_db)
    _p(conn, 905101, "ตัว")
    conn.commit()
    conn.close()
    csvf = _csv(tmp_path, [{"product_id": 905101, "bsn_unit": "กิโลกรัม",
                            uc.DEC: "1 and change ตัว unit to กิโลกรัม"}])
    assert uc.main([csvf, "--db", tmp_db, "--dump-d2"]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT unit_type FROM products WHERE id=905101"
                        ).fetchone()[0] == "ตัว"          # untouched
    conn.close()
    assert (tmp_path / "bucket_D2_fill_N.csv").exists()


def test_d2_apply(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 905301, "ตัว"); _t(conn, 905301, 3)
    conn.execute("INSERT INTO unit_conversions (product_id,bsn_unit,ratio) "
                 "VALUES (905301,'แผง',5)")               # will → 1
    conn.commit()
    conn.close()
    reviewed = _csv(tmp_path, [{"product_id": 905301, "bsn_unit": "แผง",
                                uc.DEC: "1 and change ตัว unit to แผง"}])
    d2 = tmp_path / "d2.csv"
    with open(d2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "product_name", "from_unit", "to_unit",
                    "current_stock", "convert_by_N"])
        w.writerow([905301, "P", "ตัว", "แผง", 3, "1"])
    assert uc.main([reviewed, "--db", tmp_db, "--d2", str(d2),
                    "--apply"]) == 0
    conn = sqlite3.connect(tmp_db)

    def one(s, *a):
        return conn.execute(s, a).fetchone()[0]

    assert one("SELECT unit_type FROM products WHERE id=905301") == "แผง"
    assert one("SELECT quantity FROM stock_levels WHERE product_id=905301"
               ) == 3                                     # ×1 = unchanged
    assert one("SELECT ratio FROM unit_conversions WHERE product_id=905301 "
               "AND bsn_unit='แผง'") == 1                  # decision "1 ..."
    conn.close()


def test_d1_dry_run_writes_nothing(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, 905201, "โหล"); _t(conn, 905201, 5)
    conn.commit()
    conn.close()
    csvf = _csv(tmp_path, [{"product_id": 905201, "bsn_unit": "อัน",
                            uc.DEC: D1A}])
    assert uc.main([csvf, "--db", tmp_db]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT unit_type FROM products WHERE id=905201"
                        ).fetchone()[0] == "โหล"
    conn.close()
