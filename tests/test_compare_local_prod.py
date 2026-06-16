"""Tests for scripts/compare_local_prod.py — the prod/local diff tool.

Covers the risky part: business-key classification (local-only / prod-only /
differing), float-noise tolerance, and the PROTECTED prod-only detection.
"""
import sqlite3
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import compare_local_prod as C  # noqa: E402


def _mk(path):
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE marketplace_orders (
            platform TEXT, order_sn TEXT, status TEXT, payout REAL,
            actual_payout REAL, settled_at TEXT, payout_batch_id INTEGER);
        CREATE TABLE sales_transactions (
            doc_no TEXT, bsn_code TEXT, qty REAL, unit_price REAL,
            net REAL, product_id INTEGER);
        CREATE TABLE customer_call_log (id INTEGER PRIMARY KEY, note TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, txn_type TEXT, note TEXT,
            quantity_change REAL);
        """
    )
    c.commit()
    return c


def test_keyed_diff_classifies_local_prod_only_and_differing(tmp_path):
    prod, local = str(tmp_path / "prod.db"), str(tmp_path / "local.db")
    pc, lc = _mk(prod), _mk(local)
    # prod: A (matches local), B (status will differ on local)
    pc.execute("INSERT INTO marketplace_orders VALUES ('shopee','A','done',10,10,'2026-06-01',NULL)")
    pc.execute("INSERT INTO marketplace_orders VALUES ('shopee','B','done',20,20,'2026-06-02',5)")
    # local: A (same), B (status differs), C (local-only / new on local)
    lc.execute("INSERT INTO marketplace_orders VALUES ('shopee','A','done',10,10,'2026-06-01',NULL)")
    lc.execute("INSERT INTO marketplace_orders VALUES ('shopee','B','shipped',20,20,'2026-06-02',NULL)")
    lc.execute("INSERT INTO marketplace_orders VALUES ('shopee','C','done',30,NULL,NULL,NULL)")
    pc.commit(); lc.commit(); pc.close(); lc.close()

    pconn, lconn = C.connect_ro(prod), C.connect_ro(local)
    key, sig = C.KEYED_TABLES["marketplace_orders"]
    pm = C.keyed_map(pconn, "marketplace_orders", key, sig)
    lm = C.keyed_map(lconn, "marketplace_orders", key, sig)
    local_only, prod_only, differing, shared = C.diff_keyed(pm, lm)
    assert local_only == 1   # C
    assert prod_only == 0
    assert differing == 1    # B status changed
    assert shared == 2       # A, B


def test_float_noise_does_not_count_as_differing(tmp_path):
    prod, local = str(tmp_path / "prod.db"), str(tmp_path / "local.db")
    pc, lc = _mk(prod), _mk(local)
    pc.execute("INSERT INTO sales_transactions VALUES ('IV1','c1',1,90.0,90.0,7)")
    # IEEE-754 noise in net — must be treated as identical after 2dp rounding
    lc.execute("INSERT INTO sales_transactions VALUES ('IV1','c1',1,90.000000001,90.0,7)")
    pc.commit(); lc.commit(); pc.close(); lc.close()

    pconn, lconn = C.connect_ro(prod), C.connect_ro(local)
    key, sig = C.KEYED_TABLES["sales_transactions"]
    pm = C.keyed_map(pconn, "sales_transactions", key, sig)
    lm = C.keyed_map(lconn, "sales_transactions", key, sig)
    _, _, differing, shared = C.diff_keyed(pm, lm)
    assert shared == 1
    assert differing == 0


def test_protected_prod_only_rows_flagged_loudly(tmp_path, capsys):
    prod, local = str(tmp_path / "prod.db"), str(tmp_path / "local.db")
    pc, lc = _mk(prod), _mk(local)
    # prod has team call logs + a payout-batch link; local has neither
    pc.execute("INSERT INTO customer_call_log (note) VALUES ('called A'),('called B')")
    pc.execute("INSERT INTO marketplace_orders VALUES ('shopee','B','done',20,20,'2026-06-02',5)")
    lc.execute("INSERT INTO marketplace_orders VALUES ('shopee','B','done',20,20,'2026-06-02',NULL)")
    pc.commit(); lc.commit(); pc.close(); lc.close()

    C.run(prod, local)
    text = capsys.readouterr().out
    assert "PROTECTED" in text
    assert "customer_call_log" in text
    # 2 prod-only call logs must be called out as erasable
    assert "prod has 2 row(s) local lacks" in text
    # the team bank-deposit link on prod must be flagged
    assert "payout_batch_id" in text
    assert "team bank-deposit links on prod" in text


def test_count_rows_where_and_missing_table(tmp_path):
    prod = str(tmp_path / "prod.db")
    pc = _mk(prod)
    pc.execute("INSERT INTO transactions (txn_type,note,quantity_change) VALUES ('ADJUST','manual fix',5)")
    pc.execute("INSERT INTO transactions (txn_type,note,quantity_change) VALUES ('OUT','BSN ขาย',-1)")
    pc.commit(); pc.close()

    conn = C.connect_ro(prod)
    adj = "txn_type='ADJUST' AND COALESCE(note,'') NOT LIKE 'BSN%'"
    assert C.count_rows(conn, "transactions", adj) == 1
    assert C.count_rows(conn, "does_not_exist") is None
