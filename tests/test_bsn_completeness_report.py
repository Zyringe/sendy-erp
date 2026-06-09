"""Smoke + status-logic test for scripts/bsn_completeness_report.py.

Read-only: verifies the status classification + sync_gap flag for one
synthetic product of each kind. Scoped by reading the CSV rows back.
"""
import csv
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import bsn_completeness_report as rep  # noqa: E402

ML, MN, CO, NH, NN, GAP = (902001, 902002, 902003, 902004, 902005, 902006)


def _p(c, pid, base="แผง"):
    c.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) VALUES (?, ?, ?, ?, 1)", (pid, f"P{pid}", base, f"S{pid}"))


def _m(c, pid, code):
    c.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,"
              "product_id,is_ignored) VALUES (?,?,?,0)", (code, "n", pid))


def _u(c, pid, unit, r=1.0):
    c.execute("INSERT INTO unit_conversions (product_id,bsn_unit,ratio) "
              "VALUES (?,?,?)", (pid, unit, r))


def _s(c, pid, unit, synced=1):
    c.execute("INSERT INTO sales_transactions (batch_id,date_iso,doc_no,"
              "doc_base,product_id,bsn_code,product_name_raw,customer,"
              "customer_code,qty,unit,unit_price,vat_type,discount,total,"
              "net,synced_to_stock) VALUES ('t','2025-01-01','D','D',?,'ZZ',"
              "'r','C','C1',1,?,1,0,0,0,0,?)", (pid, unit, synced))


def _t(c, pid):
    c.execute("INSERT INTO transactions (product_id,txn_type,quantity_change,"
              "unit_mode,note,created_at) VALUES (?,'ADJUST',1,'unit','x',"
              "'2025-01-01 00:00:00')", (pid,))


def test_status_logic(tmp_db, tmp_path):
    conn = sqlite3.connect(tmp_db)
    _p(conn, ML); _m(conn, ML, "ZZML"); _s(conn, ML, "แผง")     # mapped+ledger
    _p(conn, MN); _m(conn, MN, "ZZMN")                          # mapped_no_led
    _p(conn, CO); _u(conn, CO, "โหล", 12)                       # conv_orphan
    _p(conn, NH); _t(conn, NH)                                  # no_bsn_hist
    _p(conn, NN)                                                # no_bsn_none
    _p(conn, GAP); _m(conn, GAP, "ZZGAP")
    _s(conn, GAP, "โหล", synced=0)        # unit≠base, no conv, unsynced → gap
    conn.commit()
    conn.close()

    out = tmp_path / "rep.csv"
    assert rep.main(["--db", tmp_db, "--out", str(out)]) == 0
    by = {int(r["product_id"]): r
          for r in csv.DictReader(open(out, encoding="utf-8-sig"))}

    assert by[ML]["status"] == "mapped_with_ledger"
    assert by[MN]["status"] == "mapped_no_ledger"
    assert by[CO]["status"] == "conv_orphan"
    assert by[NH]["status"] == "no_bsn_has_history"
    assert by[NN]["status"] == "no_bsn_no_history"
    assert by[GAP]["sync_gap"] == "Y"
    assert by[ML]["sync_gap"] == ""
