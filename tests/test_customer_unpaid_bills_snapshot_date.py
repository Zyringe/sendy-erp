"""TDD + integration tests for Phase 2 finance revamp R4 (freshness
standard) — models.get_customer_unpaid_bills() now returns
(rows, snapshot_date) instead of a bare row list, so customer_summary.html
can disclose "ณ {snapshot_date}" like the other AR widgets already do.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')


def _admin(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
    return c


def test_returns_rows_and_snapshot_date_tuple(tmp_db):
    import sqlite3
    import models

    conn = sqlite3.connect(tmp_db)
    expected_snapshot = conn.execute(
        "SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity='BSN'"
    ).fetchone()[0]
    # Any real customer name from the live-DB clone with unpaid bills.
    row = conn.execute("""
        SELECT COALESCE(c.name, ao.customer_name) AS name
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.entity='BSN' AND ao.snapshot_date_iso = ?
          AND ao.outstanding_amount > 0
        LIMIT 1
    """, (expected_snapshot,)).fetchone()
    conn.close()
    if not row:
        import pytest
        pytest.skip("No unpaid-bill customer in the live-DB clone to test against")
    customer_name = row[0]

    result = models.get_customer_unpaid_bills(customer_name)
    assert isinstance(result, tuple) and len(result) == 2
    rows, snapshot_date = result
    assert snapshot_date == expected_snapshot
    assert len(rows) >= 1


def test_snapshot_date_is_none_when_no_snapshot_rows(empty_db_conn):
    import models
    rows, snapshot_date = models.get_customer_unpaid_bills('ไม่มีลูกค้านี้')
    assert rows == []
    assert snapshot_date is None


def test_customer_summary_route_shows_snapshot_date(tmp_db):
    """Integration guard: the header must render the freshness date
    whenever there's at least one unpaid bill for that customer."""
    import sqlite3
    import models
    from urllib.parse import quote

    conn = sqlite3.connect(tmp_db)
    snap = conn.execute(
        "SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding WHERE entity='BSN'"
    ).fetchone()[0]
    row = conn.execute("""
        SELECT COALESCE(c.name, ao.customer_name) AS name
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.entity='BSN' AND ao.snapshot_date_iso = ?
          AND ao.outstanding_amount > 0
        LIMIT 1
    """, (snap,)).fetchone()
    conn.close()
    if not row:
        import pytest
        pytest.skip("No unpaid-bill customer in the live-DB clone to test against")
    customer_name = row[0]

    c = _admin(tmp_db)
    r = c.get(f'/customer/{quote(customer_name)}')
    assert r.status_code == 200
    body = r.data.decode()
    assert f'ณ {snap}' in body
