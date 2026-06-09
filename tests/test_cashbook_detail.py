"""Unit tests for cashbook drill-down detail (_get_detail_rows).

Reconciliation property: for every dimension, the detail total equals the
matching dashboard summary helper's figure, at full float precision.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from blueprints.cashbook import (
    _get_detail_rows, _get_category_summary, _get_tag_summary,
    _get_monthly_summary,
)


def _seed(conn):
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (1,'OP',1,0,1)")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (2,'TR',1,1,2)")
    rows = [
        (1, '2026-01-01', 'income',  'ยอดขายของ',  None,         100.0),
        (1, '2026-01-02', 'income',  'เงินทุน/เงินโอน', None,     1000.0),  # transfer cat (excluded)
        (1, '2026-01-07', 'income',  None,         None,           7.0),   # NULL category -> (ไม่ระบุ)
        (1, '2026-01-03', 'expense', 'ค่าไฟ',      'โกดัง Lion',   50.0),
        (1, '2026-01-04', 'expense', 'ค่าไฟ',      'บ้านสุนทร',    30.0),
        (1, '2026-01-05', 'expense', 'เงินทุน/เงินโอน', None,      500.0),  # transfer cat (excluded)
        (1, '2026-01-06', 'expense', 'ซื้อสินค้า',  None,          20.0),
        (2, '2026-01-01', 'income',  'ยอดขายของ',  None,         999.0),   # transfer ACCOUNT (excluded)
    ]
    conn.executemany(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, user_category, amount) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def test_income_category_reconciles_each_row(empty_db_conn):
    _seed(empty_db_conn)
    inc, _ = _get_category_summary(empty_db_conn)
    assert inc, "expected at least one income category"
    for c in inc:
        rows, summary = _get_detail_rows(empty_db_conn, 'income_category', c['category'])
        assert summary['total'] == c['total']
        assert sum(r['amount'] for r in rows) == c['total']


def test_expense_category_reconciles_each_row(empty_db_conn):
    _seed(empty_db_conn)
    _, exp = _get_category_summary(empty_db_conn)
    assert exp
    for c in exp:
        rows, summary = _get_detail_rows(empty_db_conn, 'expense_category', c['category'])
        assert summary['total'] == c['total']
        assert sum(r['amount'] for r in rows) == c['total']


def test_user_tag_reconciles_each_row(empty_db_conn):
    _seed(empty_db_conn)
    for t in _get_tag_summary(empty_db_conn):
        rows, summary = _get_detail_rows(empty_db_conn, 'user_tag', t['tag'])
        assert summary['total'] == t['total']
        assert sum(r['amount'] for r in rows) == t['total']


def test_month_reconciles_income_and_expense(empty_db_conn):
    _seed(empty_db_conn)
    for m in _get_monthly_summary(empty_db_conn, exclude_transfer=True):
        rows, summary = _get_detail_rows(empty_db_conn, 'month', m['month'])
        assert summary['income'] == m['income']
        assert summary['expense'] == m['expense']
        assert summary['count'] == len(rows)


def test_transfer_category_never_reachable(empty_db_conn):
    _seed(empty_db_conn)
    rows, summary = _get_detail_rows(empty_db_conn, 'expense_category', 'เงินทุน/เงินโอน')
    assert rows == []
    assert summary['total'] == 0


def test_transfer_account_excluded(empty_db_conn):
    _seed(empty_db_conn)
    rows, summary = _get_detail_rows(empty_db_conn, 'income_category', 'ยอดขายของ')
    assert all(r['account_code'] == 'OP' for r in rows)   # TR's 999 excluded
    assert summary['total'] == 100.0


def test_unspecified_category_roundtrip(empty_db_conn):
    _seed(empty_db_conn)
    rows, summary = _get_detail_rows(empty_db_conn, 'income_category', '(ไม่ระบุ)')
    assert summary['total'] == 7.0
    assert len(rows) == 1


def test_rows_have_display_string(empty_db_conn):
    _seed(empty_db_conn)
    rows, _ = _get_detail_rows(empty_db_conn, 'expense_category', 'ค่าไฟ')
    assert rows[0]['amount_display'].startswith('฿')


def test_unknown_dim_raises(empty_db_conn):
    _seed(empty_db_conn)
    with pytest.raises(ValueError):
        _get_detail_rows(empty_db_conn, 'bogus', 'x')


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def anon_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app.test_client()


def test_detail_api_valid_dim_returns_json(admin_client):
    r = admin_client.get('/cashbook/api/detail?dim=expense_category&key=ค่าไฟ')
    assert r.status_code == 200
    data = r.get_json()
    assert 'rows' in data and 'summary' in data
    assert 'count' in data['summary']
    assert data['dim'] == 'expense_category'


def test_detail_api_unknown_dim_400(admin_client):
    assert admin_client.get('/cashbook/api/detail?dim=bogus&key=x').status_code == 400


def test_detail_api_missing_key_400(admin_client):
    assert admin_client.get('/cashbook/api/detail?dim=month').status_code == 400


def test_detail_api_requires_login(anon_client):
    r = anon_client.get('/cashbook/api/detail?dim=month&key=2026-01')
    assert r.status_code in (301, 302)   # before_request redirects anon to login


def test_dashboard_includes_drilldown_modal(admin_client):
    """Full template renders (url_for(cashbook.detail_api) resolves, no BuildError)
    and the clickable rows + modal are present."""
    html = admin_client.get('/cashbook/').data.decode('utf-8')
    assert 'id="cbDetailModal"' in html
    assert 'data-cb-dim="month"' in html
    assert 'data-cb-dim="expense_category"' in html
