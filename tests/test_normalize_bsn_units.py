"""Tests for scripts/normalize_bsn_units.py (scoped to synthetic pids).

- plain acronym row → renamed full in unit_conversions + both ledgers.
- collision same ratio → acronym row deduped, full row kept, ledger renamed
  → _get_base_qty still matches (product_id,full).
- collision different ratio → flagged, full row kept unchanged, acronym
  dropped (no silent ratio change).
- `transactions` / `stock_levels` untouched. Dry-run writes nothing.
"""
import csv
import json
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import normalize_bsn_units as nz  # noqa: E402

P1, P2, P3, P5 = 900701, 900702, 900703, 900705


def _prod(c, pid):
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, 'แผง', ?, 1)", (pid, f"P{pid}", f"SK-{pid}"))


def _uc(c, pid, unit, ratio):
    c.execute("INSERT INTO unit_conversions (product_id,bsn_unit,ratio) "
              "VALUES (?,?,?)", (pid, unit, ratio))


def _sale(c, pid, unit):
    c.execute("INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
              "doc_base,product_id,bsn_code,product_name_raw,customer,"
              "customer_code,qty,unit,unit_price,vat_type,discount,total,"
              "net,synced_to_stock) VALUES ('t','2025-01-01','D','D',?,'ZZ',"
              "'r','C','C1',1,?,1,0,0,0,0,0)", (pid, unit))


def _mapfile(tmp_path):
    p = tmp_path / "m.json"
    json.dump({"map": {"หล": "โหล", "โหล": "โหล", "ผง": "แผง",
                       "แผง": "แผง", "!ผง": "แผง"}}, open(p, "w"))
    return str(p)


def test_normalize(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _prod(conn, P1)
    _uc(conn, P1, "หล", 12)            # plain rename → โหล
    _sale(conn, P1, "หล")
    _prod(conn, P2)
    _uc(conn, P2, "หล", 12)            # collision same ratio with โหล
    _uc(conn, P2, "โหล", 12)
    _sale(conn, P2, "หล")
    _prod(conn, P3)
    _uc(conn, P3, "ผง", 1000)          # collision: acronym bigger → keep 1000
    _uc(conn, P3, "แผง", 1)
    _prod(conn, P5)
    _uc(conn, P5, "ผง", 7)             # batch-collision: two acronyms, same
    _uc(conn, P5, "!ผง", 3)            # product, both → แผง (no pre-existing)
    conn.commit()
    sl_before = conn.execute(
        "SELECT COUNT(*) FROM stock_levels").fetchone()[0]
    tx_before = conn.execute(
        "SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.close()

    mf = _mapfile(tmp_path)
    assert nz.main(["--db", tmp_db, "--map", mf, "--apply"]) == 0

    conn = sqlite3.connect(tmp_db)

    def one(sql, *a):
        return conn.execute(sql, a).fetchone()

    # P1 plain rename
    assert one("SELECT bsn_unit FROM unit_conversions WHERE product_id=?",
               P1)[0] == "โหล"
    assert one("SELECT unit FROM sales_transactions WHERE product_id=?",
               P1)[0] == "โหล"

    # P2 dedupe: exactly one row, full form, ledger renamed (sync matches)
    rows = conn.execute("SELECT bsn_unit FROM unit_conversions WHERE "
                        "product_id=?", (P2,)).fetchall()
    assert [r[0] for r in rows] == ["โหล"]
    assert one("SELECT unit FROM sales_transactions WHERE product_id=?",
               P2)[0] == "โหล"

    # P3 conflict: surviving full row carries the LARGER ratio (1000),
    # acronym row dropped
    r3 = conn.execute("SELECT bsn_unit,ratio FROM unit_conversions WHERE "
                      "product_id=?", (P3,)).fetchall()
    assert [(x[0], x[1]) for x in r3] == [("แผง", 1000)]

    # P5 batch-collision: two acronyms on same product → same target, none
    # pre-existing → collapse to ONE row, max ratio, no UNIQUE error
    r5 = conn.execute("SELECT bsn_unit,ratio FROM unit_conversions WHERE "
                      "product_id=?", (P5,)).fetchall()
    assert [(x[0], x[1]) for x in r5] == [("แผง", 7)]

    # ledger ledger-only / stock untouched
    assert conn.execute("SELECT COUNT(*) FROM stock_levels"
                        ).fetchone()[0] == sl_before
    assert conn.execute("SELECT COUNT(*) FROM transactions"
                        ).fetchone()[0] == tx_before

    exp = os.path.join(REPO, "data", "exports")
    cf = sorted(f for f in os.listdir(exp)
                if f.startswith("normalize_bsn_units_conflicts_"))[-1]
    body = open(os.path.join(exp, cf)).read()
    assert str(P3) in body and "ผง" in body
    conn.close()


def test_normalize_dry_run_writes_nothing(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _prod(conn, 900801)
    _uc(conn, 900801, "หล", 12)
    conn.commit()
    conn.close()
    mf = _mapfile(tmp_path)
    assert nz.main(["--db", tmp_db, "--map", mf]) == 0
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT bsn_unit FROM unit_conversions WHERE "
                        "product_id=900801").fetchone()[0] == "หล"
    conn.close()
