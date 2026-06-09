"""Unit tests for the cashbook dashboard P&L logic (change A).

Seeds a controlled scenario into empty_db (clean schema clone, zero rows) and
calls the aggregation helpers directly, so every assertion is exact and
independent of the live DB's contents.

Scenario (operating account OP + transfer account TR):
  OP income  ยอดขายของ            100   -> operating income
  OP income  เงินทุน/เงินโอน      1000   -> transfer (EXCLUDED from P&L, counts to balance)
  OP expense ค่าไฟ / โกดัง Lion     50   -> business
  OP expense ค่าไฟ / บ้านสุนทร      30   -> personal
  OP expense เงินทุน/เงินโอน       500   -> transfer (EXCLUDED)
  OP expense ซื้อสินค้า (untagged)  20   -> unclassified
  TR income  ยอดขายของ            999   -> transfer ACCOUNT (EXCLUDED from headline)
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from blueprints.cashbook import (
    _get_accounts_with_totals, _get_category_summary, _get_tag_summary,
    _get_monthly_summary, TRANSFER_CATEGORIES,
)

TRANSFER = TRANSFER_CATEGORIES[0]


def _seed(conn):
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (1,'OP',1,0,1)")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (2,'TR',1,1,2)")
    rows = [
        (1, '2026-01-01', 'income',  'ยอดขายของ', None,         100.0),
        (1, '2026-01-02', 'income',  TRANSFER,    None,        1000.0),
        (1, '2026-01-03', 'expense', 'ค่าไฟ',     'โกดัง Lion',  50.0),
        (1, '2026-01-04', 'expense', 'ค่าไฟ',     'บ้านสุนทร',   30.0),
        (1, '2026-01-05', 'expense', TRANSFER,    None,         500.0),
        (1, '2026-01-06', 'expense', 'ซื้อสินค้า', None,          20.0),
        (2, '2026-01-01', 'income',  'ยอดขายของ', None,         999.0),
    ]
    conn.executemany(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, user_category, amount) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def test_per_account_operating_vs_transfer(empty_db_conn):
    _seed(empty_db_conn)
    accts = {a['code']: a for a in _get_accounts_with_totals(empty_db_conn)}
    op = accts['OP']
    assert op['income'] == 100.0          # transfer income (1000) excluded
    assert op['expense'] == 100.0         # 50+30+20 ; transfer expense (500) excluded
    assert op['transfer_in'] == 1000.0
    assert op['transfer_out'] == 500.0
    assert op['balance'] == (100 + 1000) - (100 + 500)   # true cash = 500


def test_headline_excludes_transfer_accounts_and_categories(empty_db_conn):
    _seed(empty_db_conn)
    op = [a for a in _get_accounts_with_totals(empty_db_conn) if not a['is_transfer']]
    assert sum(a['income'] for a in op) == 100.0    # not 1100, not 2099
    assert sum(a['expense'] for a in op) == 100.0   # not 600


def test_headline_balance_is_true_cash_and_reconciles_with_table(empty_db_conn):
    """The headline คงเหลือ must equal the sum of the per-account balance column
    (true cash, incl. transfers) — NOT operating income − expense."""
    _seed(empty_db_conn)
    op = [a for a in _get_accounts_with_totals(empty_db_conn) if not a['is_transfer']]
    total_income = sum(a['income'] for a in op)
    total_expense = sum(a['expense'] for a in op)
    total_balance = sum(a['balance'] for a in op)          # = how dashboard() computes it
    assert total_balance == 500.0                          # (100+1000) - (100+500)
    assert total_balance != total_income - total_expense   # operating net is 0; cash includes transfers


def test_category_summary_drops_transfer(empty_db_conn):
    _seed(empty_db_conn)
    inc, exp = _get_category_summary(empty_db_conn)
    cats = {c['category'] for c in inc} | {c['category'] for c in exp}
    assert TRANSFER not in cats
    exp_map = {c['category']: c['total'] for c in exp}
    assert exp_map == {'ค่าไฟ': 80.0, 'ซื้อสินค้า': 20.0}
    inc_map = {c['category']: c['total'] for c in inc}
    assert inc_map == {'ยอดขายของ': 100.0}          # TR account's 999 excluded


def test_tag_summary_excludes_transfers_and_untagged(empty_db_conn):
    _seed(empty_db_conn)
    tags = {t['tag']: t['total'] for t in _get_tag_summary(empty_db_conn)}
    assert tags == {'โกดัง Lion': 50.0, 'บ้านสุนทร': 30.0}


def test_monthly_excludes_transfer(empty_db_conn):
    _seed(empty_db_conn)
    m = _get_monthly_summary(empty_db_conn, exclude_transfer=True)
    assert len(m) == 1
    assert m[0]['income'] == 100.0
    assert m[0]['expense'] == 100.0


import pytest


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


def test_dashboard_renders_new_sections(admin_client):
    """The full template renders (Jinja valid) with the by-tag and
    transfer-disclosure sections present."""
    html = admin_client.get('/cashbook/').data.decode('utf-8')
    assert 'ค่าใช้จ่ายตามผู้ใช้/สถานที่' in html
    assert 'เงินสดจริงในบัญชี' in html          # คงเหลือ disclosure (true cash incl transfers)
