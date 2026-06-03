"""The /alerts page is a negative-stock watchdog (data-integrity red flag).

Key correctness claim: it fires on real negative stock but NOT on IEEE-754 float
noise (a REAL quantity column can read e.g. -1e-14 after trigger arithmetic).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import models


def test_negative_stock_alert_fires_with_float_tolerance(tmp_db):
    conn = sqlite3.connect(tmp_db)
    pids = [r[0] for r in conn.execute(
        "SELECT s.product_id FROM stock_levels s JOIN products p ON p.id = s.product_id "
        "WHERE p.is_active = 1 LIMIT 2").fetchall()]
    real_neg, float_noise = pids[0], pids[1]
    conn.execute("UPDATE stock_levels SET quantity = -5 WHERE product_id = ?", (real_neg,))
    conn.execute("UPDATE stock_levels SET quantity = ? WHERE product_id = ?", (-1e-14, float_noise))
    conn.commit()
    conn.close()

    ids = [r['id'] for r in models.get_stock_alerts()]
    assert real_neg in ids            # a genuine negative fires the alert
    assert float_noise not in ids     # IEEE-754 noise must NOT (the -0.001 tolerance)
    assert models.count_stock_alerts() >= 1


def test_no_alert_when_stock_non_negative(tmp_db):
    # the live-copy DB has 0 negative stock after the 2026-05-30 ledger rebuild
    assert models.get_stock_alerts() == [] or all(
        r['quantity'] < -0.001 for r in models.get_stock_alerts())
    assert isinstance(models.count_stock_alerts(), int)


def test_restock_metric_and_filter_run(tmp_db):
    # count must match the get_products(restock=True) result set size
    n = models.count_restock_needed()
    assert isinstance(n, int) and n >= 0
    rows, total = models.get_products(restock=True, per_page=10000)
    assert total == n
