"""TDD tests for the doc_no search feature on /sales and /purchases.

Spec (LOCKED): a `doc_no` filter, partial/case-insensitive match on the
`doc_no` column (`doc_no LIKE '%'||?||'%'`), that OVERRIDES date_from/date_to
entirely when non-empty (search all history) — same "widen the range"
precedent product_id already has in the route. Summaries must scope to the
same filtered row set. /purchases also gains a get_purchases_summary_by_vat
mirroring get_sales_summary, plus a vat_type filter on get_purchases.

Model-level tests use the `empty_db` schema clone with synthetic rows (same
pattern as tests/test_purchases_summary.py) so assertions are deterministic.
Route-level render smoke tests use `tmp_db` (a copy of the real, live-shaped
DB) with the real doc numbers named in the task spec (IV6901104, HP6900037).
"""
import sqlite3

import pytest


# ── synthetic-data helpers (empty_db schema clone) ──────────────────────────

def _ins_sale(conn, doc_no, date_iso, net, vat_type=1, customer='ACME Co',
              qty=1, doc_base=None):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, qty, unit, unit_price,
            vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, doc_no, doc_base or doc_no, customer, qty, 'ตัว', net,
         vat_type, net, net),
    )


def _ins_purchase(conn, doc_no, date_iso, net, vat_type=1,
                   supplier='ACME Supply', qty=1, doc_base=None):
    conn.execute(
        """INSERT INTO purchase_transactions
           (date_iso, doc_no, doc_base, supplier, qty, unit, unit_price,
            vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, doc_no, doc_base or doc_no, supplier, qty, 'ตัว', net,
         vat_type, net, net),
    )


@pytest.fixture
def db_conn(empty_db):
    conn = sqlite3.connect(empty_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── get_sales: doc_no match + date-override ─────────────────────────────────

def test_get_sales_doc_no_returns_all_lines_and_ignores_date_range(db_conn):
    import models
    # Both lines of IV6900001 sit OUTSIDE the date range we'll filter by.
    _ins_sale(db_conn, 'IV6900001-1', '2020-01-01', 100.0)
    _ins_sale(db_conn, 'IV6900001-2', '2020-01-02', 200.0)
    # An unrelated doc that WOULD be in range but must not match the search.
    _ins_sale(db_conn, 'IV9999999-1', '2026-06-15', 999.0)
    db_conn.commit()

    rows, total = models.get_sales(
        doc_no='IV6900001', date_from='2026-01-01', date_to='2026-12-31'
    )
    assert total == 2
    assert {r['doc_no'] for r in rows} == {'IV6900001-1', 'IV6900001-2'}


def test_get_sales_doc_no_partial_case_insensitive_match(db_conn):
    import models
    _ins_sale(db_conn, 'IV6900123-1', '2026-01-01', 50.0)
    db_conn.commit()

    rows, total = models.get_sales(doc_no='iv690012')
    assert total == 1
    assert rows[0]['doc_no'] == 'IV6900123-1'


def test_get_sales_without_doc_no_still_applies_date_range(db_conn):
    """Regression guard: doc_no is opt-in — when absent, date filtering must
    behave exactly as before."""
    import models
    _ins_sale(db_conn, 'IV7000001-1', '2020-01-01', 100.0)
    db_conn.commit()

    rows, total = models.get_sales(date_from='2026-01-01', date_to='2026-12-31')
    assert total == 0


# ── get_sales_summary: doc_no scoping ────────────────────────────────────────

def test_get_sales_summary_scopes_to_doc_and_ignores_dates(db_conn):
    import models
    _ins_sale(db_conn, 'IV6900001-1', '2020-01-01', 100.0, vat_type=1)
    _ins_sale(db_conn, 'IV6900001-2', '2020-01-02', 200.0, vat_type=2)
    _ins_sale(db_conn, 'IV9999999-1', '2026-06-15', 999.0, vat_type=1)
    db_conn.commit()

    rows = models.get_sales_summary(
        doc_no='IV6900001', date_from='2026-01-01', date_to='2026-12-31'
    )
    by_vat = {r['vat_type']: dict(r) for r in rows}
    assert by_vat[1]['total_net'] == pytest.approx(100.0)
    assert by_vat[2]['total_net'] == pytest.approx(200.0)
    assert 999.0 not in [r['total_net'] for r in rows]


# ── get_purchases: vat_type filter + doc_no override ────────────────────────

def test_get_purchases_vat_type_filters(db_conn):
    import models
    _ins_purchase(db_conn, 'HP7000001-1', '2026-06-01', 100.0, vat_type=1)
    _ins_purchase(db_conn, 'HP7000002-1', '2026-06-01', 200.0, vat_type=2)
    db_conn.commit()

    rows, total = models.get_purchases(vat_type=1, date_from='2026-01-01',
                                        date_to='2026-12-31')
    assert total == 1
    assert rows[0]['doc_no'] == 'HP7000001-1'


def test_get_purchases_doc_no_returns_all_lines_and_ignores_date_range(db_conn):
    import models
    _ins_purchase(db_conn, 'HP7000003-1', '2020-01-01', 300.0)
    _ins_purchase(db_conn, 'HP7000003-2', '2020-01-02', 400.0)
    _ins_purchase(db_conn, 'HP9999999-1', '2026-06-15', 999.0)
    db_conn.commit()

    rows, total = models.get_purchases(
        doc_no='HP7000003', date_from='2026-01-01', date_to='2026-12-31'
    )
    assert total == 2
    assert {r['doc_no'] for r in rows} == {'HP7000003-1', 'HP7000003-2'}


def test_get_purchases_doc_no_partial_case_insensitive_match(db_conn):
    import models
    _ins_purchase(db_conn, 'HP6900037-1', '2026-01-01', 50.0)
    db_conn.commit()

    rows, total = models.get_purchases(doc_no='hp690003')
    assert total == 1
    assert rows[0]['doc_no'] == 'HP6900037-1'


# ── get_purchases_summary: doc_no override ──────────────────────────────────

def test_get_purchases_summary_scopes_to_doc_and_ignores_dates(db_conn):
    import models
    _ins_purchase(db_conn, 'HP7000004-1', '2020-01-01', 111.0)
    _ins_purchase(db_conn, 'HP7000004-2', '2020-01-02', 222.0)
    _ins_purchase(db_conn, 'HP9999999-1', '2026-06-15', 999.0)
    db_conn.commit()

    summary = models.get_purchases_summary(
        doc_no='HP7000004', date_from='2026-01-01', date_to='2026-12-31'
    )
    assert summary['total_net'] == pytest.approx(333.0)
    assert summary['txn_count'] == 2


# ── get_purchases_summary_by_vat: new function ──────────────────────────────

def test_get_purchases_summary_by_vat_groups_by_vat_type(db_conn):
    import models
    _ins_purchase(db_conn, 'HP7000005-1', '2026-06-01', 100.0, vat_type=1)
    _ins_purchase(db_conn, 'HP7000006-1', '2026-06-01', 250.0, vat_type=2)
    _ins_purchase(db_conn, 'HP7000007-1', '2026-06-01', 75.0, vat_type=0)
    db_conn.commit()

    rows = models.get_purchases_summary_by_vat(date_from='2026-01-01',
                                                date_to='2026-12-31')
    by_vat = {r['vat_type']: dict(r) for r in rows}
    assert by_vat[1]['total_net'] == pytest.approx(100.0)
    assert by_vat[2]['total_net'] == pytest.approx(250.0)
    assert by_vat[0]['total_net'] == pytest.approx(75.0)
    assert by_vat[1]['txn_count'] == 1


def test_get_purchases_summary_by_vat_scopes_to_doc_and_ignores_dates(db_conn):
    import models
    # Same doc_base, two vat types, both OUTSIDE the date range filter below.
    _ins_purchase(db_conn, 'HP7000008-1', '2020-01-01', 100.0, vat_type=1,
                  doc_base='HP7000008')
    _ins_purchase(db_conn, 'HP7000008-2', '2020-01-02', 50.0, vat_type=2,
                  doc_base='HP7000008')
    # Unrelated doc, IN range — must not leak into the scoped result.
    _ins_purchase(db_conn, 'HP9999999-1', '2026-06-15', 999.0, vat_type=1)
    db_conn.commit()

    rows = models.get_purchases_summary_by_vat(
        doc_no='HP7000008', date_from='2026-01-01', date_to='2026-12-31'
    )
    by_vat = {r['vat_type']: dict(r) for r in rows}
    assert by_vat[1]['total_net'] == pytest.approx(100.0)
    assert by_vat[2]['total_net'] == pytest.approx(50.0)
    assert 999.0 not in [r['total_net'] for r in rows]


# ── Route-level render smoke tests (real DB copy, authed client) ───────────

def _admin(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
    return c


def test_sales_view_renders_default(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/sales')
    assert r.status_code == 200


def test_sales_view_doc_no_search_renders_and_shows_hint(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/sales?doc_no=IV6901104')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'IV6901104' in body
    assert 'ข้ามตัวกรองวันที่' in body


def test_purchases_view_renders_default(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/purchases')
    assert r.status_code == 200


def test_purchases_view_doc_no_search_renders_and_shows_hint(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/purchases?doc_no=HP6900037')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'HP6900037' in body
    assert 'ข้ามตัวกรองวันที่' in body


def test_purchases_view_vat_type_filter_renders(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/purchases?vat_type=1')
    assert r.status_code == 200
