"""TDD tests for models.get_purchases_summary() — the range-total summary
backing purchases.html's "รวมช่วงนี้" (replaces the page-only Jinja
`rows | sum(attribute='net')`, which only summed the CURRENT page).

Synthetic data on the empty_db schema clone. get_purchases_summary() opens
its own connection via database.get_connection() (same pattern as
get_purchases/get_sales_summary), so tests use the `empty_db` fixture
(patches config.DATABASE_PATH + database.DATABASE_PATH) and seed rows via a
raw sqlite3 connection — mirrors the get_purchases WHERE filter (date_iso
only; product_id is accepted by get_purchases but purchases_view never
passes it, so the summary doesn't need it either).
"""
import sqlite3

import pytest


def _ins_purchase(conn, doc_no, date_iso, net, supplier='ACME Supply',
                   doc_base=None):
    conn.execute(
        """INSERT INTO purchase_transactions
           (date_iso, doc_no, doc_base, supplier, qty, unit, unit_price,
            vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, doc_no, doc_base or doc_no, supplier, 1, 'ตัว', net, 1,
         net, net),
    )


@pytest.fixture
def db_conn(empty_db):
    conn = sqlite3.connect(empty_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def test_total_net_sums_the_whole_filtered_range_not_just_one_page(db_conn):
    """The bug this fixes: purchases.html summed only `rows` (the current
    page). A range with more than one page's worth of purchases must still
    report the FULL total."""
    import models
    for i in range(60):  # > a typical page size
        _ins_purchase(db_conn, f'HP{i:04d}', '2026-05-10', 100.0)
    db_conn.commit()

    summary = models.get_purchases_summary(date_from='2026-01-01',
                                            date_to='2026-12-31')
    # Independent signal: hand-aggregate directly, not via get_purchases.
    expected_total = db_conn.execute(
        "SELECT ROUND(SUM(net), 2) FROM purchase_transactions"
    ).fetchone()[0]
    assert summary['total_net'] == pytest.approx(expected_total)
    assert summary['total_net'] == pytest.approx(6000.0)
    assert summary['txn_count'] == 60


def test_date_filter_respected(db_conn):
    import models
    _ins_purchase(db_conn, 'HP0001', '2025-12-31', 500.0)
    _ins_purchase(db_conn, 'HP0002', '2026-02-01', 700.0)
    db_conn.commit()

    summary = models.get_purchases_summary(date_from='2026-01-01',
                                            date_to='2026-12-31')
    assert summary['total_net'] == pytest.approx(700.0)
    assert summary['txn_count'] == 1


def test_no_rows_in_range_returns_zero_not_none(db_conn):
    import models
    _ins_purchase(db_conn, 'HP0003', '2025-01-01', 300.0)
    db_conn.commit()

    summary = models.get_purchases_summary(date_from='2026-01-01',
                                            date_to='2026-12-31')
    # SUM() over zero matching rows is NULL in SQLite — must be normalised
    # to 0.0 so the template's `| fmt_price` never chokes on None.
    assert summary['total_net'] == 0.0
    assert summary['txn_count'] == 0


def test_no_date_filter_covers_everything(db_conn):
    import models
    _ins_purchase(db_conn, 'HP0004', '2020-06-01', 111.0)
    _ins_purchase(db_conn, 'HP0005', '2026-06-01', 222.0)
    db_conn.commit()

    summary = models.get_purchases_summary()
    assert summary['total_net'] == pytest.approx(333.0)
    assert summary['txn_count'] == 2
