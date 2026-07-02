"""Unit tests for cashbook Phase 1 (month-scope query/logic layer).

Covers: _overspend_flags (+ its pure date-math helpers), the month-scoped
_get_accounts_with_totals / _get_category_summary / _get_tag_summary, the
_default_month resolver, and the dashboard() route wiring (card-3 semantics,
default-month resolution). Template is untouched this phase — route-level
assertions inspect the kwargs passed to render_template rather than scraping
HTML, since none of the new context keys are rendered yet.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from datetime import date

import pytest

from blueprints.cashbook import (
    _get_accounts_with_totals, _get_category_summary, _get_tag_summary,
    _get_monthly_summary, _default_month, _overspend_flags, _month_bounds,
    _prev_month, _month_day_count, TRANSFER_CATEGORIES,
)

TRANSFER = TRANSFER_CATEGORIES[0]


# ── Seed helpers ─────────────────────────────────────────────────────────────

def _seed_accounts(conn):
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (1,'OP',1,0,1)")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (2,'TR',1,1,2)")
    # Active account with NO transactions ever — the LEFT-JOIN canary.
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (3,'IDLE',1,0,3)")


def _insert_txns(conn, rows):
    conn.executemany(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, user_category, amount) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def _seed_overspend_scenario(conn):
    """Jan + Feb 2026 on account OP (id=1). Account IDLE (id=3) has zero
    transactions — used for the LEFT-JOIN safety test. Account TR (id=2) has
    a transfer-category txn for the all-time-invariance / card-3 checks.

    Feb 'ค่าไฟ'  : Jan 8000 -> Feb 10000  (+25% / +2000)  => flagged
    Feb 'ค่าน้ำ' : Jan   50 -> Feb  100  (+100% / +50)   => NOT flagged (diff floor)
    Feb 'ค่าเช่า': absent Jan -> Feb 5000              => is_new
    """
    _seed_accounts(conn)
    rows = [
        (1, '2026-01-05', 'income',  'ยอดขายของ', None, 5000.0),
        (1, '2026-01-10', 'expense', 'ค่าไฟ',      None, 8000.0),
        (1, '2026-01-12', 'expense', 'ค่าน้ำ',     None,   50.0),
        (1, '2026-02-01', 'income',  'ยอดขายของ', None, 6000.0),
        (1, '2026-02-10', 'expense', 'ค่าไฟ',      None, 10000.0),
        (1, '2026-02-11', 'expense', 'ค่าน้ำ',     None,  100.0),
        (1, '2026-02-15', 'expense', 'ค่าเช่า',    None, 5000.0),
        (2, '2026-02-01', 'income',  'ยอดขายของ', None,  999.0),   # TR account, excluded
    ]
    _insert_txns(conn, rows)


def _seed_mtd_scenario(conn):
    """Feb + Mar 2026, day-placed so a D=5 MTD clip provably excludes
    later-in-month rows (not just because the data happens to not exist)."""
    _seed_accounts(conn)
    rows = [
        (1, '2026-03-01', 'expense', 'ค่าไฟ', None,  100.0),   # within D=5
        (1, '2026-03-05', 'expense', 'ค่าไฟ', None,   50.0),   # boundary day==D, included
        (1, '2026-03-06', 'expense', 'ค่าไฟ', None, 9999.0),   # beyond D, excluded
        (1, '2026-02-01', 'expense', 'ค่าไฟ', None,   80.0),   # within D=5
        (1, '2026-02-05', 'expense', 'ค่าไฟ', None,   20.0),   # boundary day==D, included
        (1, '2026-02-06', 'expense', 'ค่าไฟ', None, 9999.0),   # beyond D, excluded
    ]
    _insert_txns(conn, rows)


def _seed_transfer_scenario(conn):
    """Mirrors test_cashbook_pnl.py's scenario: OP has both operating and
    transfer-category rows in the same month; TR is a transfer account."""
    _seed_accounts(conn)
    rows = [
        (1, '2026-04-01', 'income',  'ยอดขายของ', None,  100.0),
        (1, '2026-04-02', 'income',  TRANSFER,    None, 1000.0),   # capital in, excluded from net
        (1, '2026-04-03', 'expense', 'ค่าไฟ',      None,   50.0),
        (1, '2026-04-05', 'expense', TRANSFER,    None,  500.0),   # capital out, excluded from net
        (2, '2026-04-01', 'income',  'ยอดขายของ', None,  999.0),   # TR account, excluded
    ]
    _insert_txns(conn, rows)


# ── A. _overspend_flags ──────────────────────────────────────────────────────

def test_overspend_flags_flagged_when_over_pct_and_floor(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    flags = {f['category']: f for f in
             _overspend_flags(empty_db_conn, '2026-02', today=date(2026, 3, 15))}
    f = flags['ค่าไฟ']
    assert f['this'] == 10000.0
    assert f['prev'] == 8000.0
    assert f['diff'] == 2000.0
    assert f['flagged'] is True
    assert f['is_new'] is False


def test_overspend_flags_not_flagged_below_diff_floor(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    flags = {f['category']: f for f in
             _overspend_flags(empty_db_conn, '2026-02', today=date(2026, 3, 15))}
    f = flags['ค่าน้ำ']
    assert f['this'] == 100.0
    assert f['prev'] == 50.0
    assert f['diff'] == 50.0          # +100% but only +฿50 -> floor blocks it
    assert f['flagged'] is False
    assert f['is_new'] is False


def test_overspend_flags_new_category_not_flagged(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    flags = {f['category']: f for f in
             _overspend_flags(empty_db_conn, '2026-02', today=date(2026, 3, 15))}
    f = flags['ค่าเช่า']
    assert f['prev'] == 0.0
    assert f['is_new'] is True
    assert f['flagged'] is False


def test_overspend_flags_only_lists_categories_present_this_month(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    flags = _overspend_flags(empty_db_conn, '2026-02', today=date(2026, 3, 15))
    cats = {f['category'] for f in flags}
    assert cats == {'ค่าไฟ', 'ค่าน้ำ', 'ค่าเช่า'}


def test_overspend_flags_mtd_clips_both_sides_to_day_d(empty_db_conn):
    """today = Mar 5 -> D=5. Both this-month (Mar) and prev-month (Feb) totals
    must only include day 1..5 — the day-6 rows (9999 each side) must NOT
    leak in, proving this is a real clip and not an accident of missing data."""
    _seed_mtd_scenario(empty_db_conn)
    flags = {f['category']: f for f in
             _overspend_flags(empty_db_conn, '2026-03', today=date(2026, 3, 5))}
    f = flags['ค่าไฟ']
    assert f['this'] == 150.0   # 100 + 50, NOT +9999
    assert f['prev'] == 100.0   # 80 + 20, NOT +9999


def test_month_bounds_no_limit_is_full_month(empty_db_conn):
    assert _month_bounds('2026-02') == ('2026-02-01', '2026-02-28')
    assert _month_bounds('2026-03') == ('2026-03-01', '2026-03-31')


def test_month_bounds_clamps_short_prev_month():
    """The literal plan.md example: today Mar 31 (D=31) -> prev Feb clip =
    Feb 1..28, not a nonexistent Feb 31."""
    assert _month_bounds('2026-02', 31) == ('2026-02-01', '2026-02-28')
    assert _month_bounds('2026-03', 31) == ('2026-03-01', '2026-03-31')


def test_prev_month_wraps_january():
    assert _prev_month('2026-01') == '2025-12'
    assert _prev_month('2026-03') == '2026-02'


def test_month_day_count():
    assert _month_day_count('2026-02') == 28   # 2026 is not a leap year
    assert _month_day_count('2026-04') == 30
    assert _month_day_count('2026-12') == 31


# ── B. LEFT-JOIN safety ──────────────────────────────────────────────────────

def test_idle_account_still_appears_in_month_mode(empty_db_conn):
    """IDLE (id=3) never has any transaction, ever. Scoped to a month that HAS
    activity for other accounts (Jan, where OP is active), IDLE must still
    appear with ฿0 columns — a naive WHERE-based month filter would drop it."""
    _seed_overspend_scenario(empty_db_conn)
    accts = {a['code']: a for a in _get_accounts_with_totals(empty_db_conn, '2026-01')}
    assert 'IDLE' in accts
    assert accts['IDLE']['income'] == 0.0
    assert accts['IDLE']['expense'] == 0.0
    assert accts['IDLE']['txn_count'] == 0


def test_active_account_with_no_activity_that_month_still_appears(empty_db_conn):
    """OP (id=1) has Jan/Feb activity but zero rows in March. Scoped to March
    (a month with NO activity for any account), OP and IDLE must both still
    appear with ฿0 columns, not vanish."""
    _seed_overspend_scenario(empty_db_conn)
    accts = {a['code']: a for a in _get_accounts_with_totals(empty_db_conn, '2026-03')}
    assert 'IDLE' in accts
    assert 'OP' in accts
    assert accts['OP']['income'] == 0.0
    assert accts['OP']['expense'] == 0.0
    assert accts['OP']['txn_count'] == 0


def test_accounts_with_totals_month_scopes_correctly(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    accts = {a['code']: a for a in _get_accounts_with_totals(empty_db_conn, '2026-02')}
    op = accts['OP']
    assert op['income'] == 6000.0
    assert op['expense'] == 10000.0 + 100.0 + 5000.0
    assert op['txn_count'] == 4   # income + 3 expense rows in Feb


# ── C. Default-month resolver ────────────────────────────────────────────────

def test_default_month_returns_max_month_present(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    assert _default_month(empty_db_conn) == '2026-02'


def test_default_month_none_when_ledger_empty(empty_db_conn):
    _seed_accounts(empty_db_conn)   # accounts only, zero transactions
    empty_db_conn.commit()
    assert _default_month(empty_db_conn) is None


# ── D/5. All-time invariance (no regression vs pre-Phase-1 behavior) ────────

def test_accounts_with_totals_all_time_matches_no_month_arg(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    assert _get_accounts_with_totals(empty_db_conn) == _get_accounts_with_totals(empty_db_conn, None)


def test_category_summary_all_time_matches_no_month_arg(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    assert _get_category_summary(empty_db_conn) == _get_category_summary(empty_db_conn, None)


def test_tag_summary_all_time_matches_no_month_arg(empty_db_conn):
    _seed_overspend_scenario(empty_db_conn)
    assert _get_tag_summary(empty_db_conn) == _get_tag_summary(empty_db_conn, None)


def test_accounts_with_totals_all_time_sums_across_months(empty_db_conn):
    """All-time (month=None) must NOT silently filter to one month — a
    regression here would look identical to a correctly-scoped single month
    if the test only checked one month's numbers."""
    _seed_overspend_scenario(empty_db_conn)
    op = {a['code']: a for a in _get_accounts_with_totals(empty_db_conn, None)}['OP']
    assert op['income'] == 5000.0 + 6000.0                    # Jan + Feb
    assert op['expense'] == (8000.0 + 50.0) + (10000.0 + 100.0 + 5000.0)


# ── 6. Independent cross-check: category-summary Σexpense == monthly-summary ─

def test_category_summary_expense_matches_monthly_summary(empty_db_conn):
    """For a fully-past seeded month, the month-scoped Σexpense from
    _get_category_summary must equal that month's expense figure from the
    (never-scoped) _get_monthly_summary trend helper — two independent query
    shapes over the same underlying rows."""
    _seed_overspend_scenario(empty_db_conn)
    monthly = {m['month']: m for m in _get_monthly_summary(empty_db_conn, exclude_transfer=True)}

    for month in ('2026-01', '2026-02'):
        _income, expense_cats = _get_category_summary(empty_db_conn, month)
        scoped_total = sum(c['total'] for c in expense_cats)
        assert scoped_total == monthly[month]['expense']


# ── D. dashboard() route wiring ──────────────────────────────────────────────
#
# Template is untouched this phase, so none of the new context keys are
# rendered yet. These tests monkeypatch render_template inside the cashbook
# module to capture the kwargs the route actually computed and passed,
# without needing the template to consume them.

def _seed_via_path(db_path, seed_fn):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    seed_fn(conn)
    conn.commit()
    conn.close()


@pytest.fixture
def captured_dashboard_kwargs(tmp_db):
    """Call the real dashboard() view function inside a request context and
    capture what it would have passed to render_template. Returns a callable
    `run(query_string, seed_fn)` -> captured kwargs dict."""
    from app import app as flask_app
    import blueprints.cashbook as cb

    def run(query_string, seed_fn):
        _seed_via_path(tmp_db, seed_fn)
        captured = {}

        def fake_render(_template_name, **kwargs):
            captured.update(kwargs)
            return ""

        orig = cb.render_template
        cb.render_template = fake_render
        try:
            with flask_app.test_request_context(f"/cashbook/?{query_string}"):
                cb.dashboard()
        finally:
            cb.render_template = orig
        return captured

    return run


def test_dashboard_card3_month_mode_is_net_excluding_transfers(captured_dashboard_kwargs):
    kw = captured_dashboard_kwargs("month=2026-04", _seed_transfer_scenario)
    assert kw['is_all_time'] is False
    assert kw['card3_is_net'] is True
    # income 100, expense 50 (operating only; transfer 1000 in / 500 out excluded)
    assert kw['card3_value'] == 50.0
    assert kw['card3_value'] == kw['total_income'] - kw['total_expense']
    assert kw['card3_value'] != kw['total_balance']   # total_balance folds in transfers


def test_dashboard_card3_all_time_mode_is_balance(captured_dashboard_kwargs):
    kw = captured_dashboard_kwargs("month=ทั้งหมด", _seed_transfer_scenario)
    assert kw['is_all_time'] is True
    assert kw['card3_is_net'] is False
    assert kw['card3_value'] == kw['total_balance']


def test_dashboard_default_month_resolves_to_most_recent_with_data(captured_dashboard_kwargs):
    kw = captured_dashboard_kwargs("", _seed_overspend_scenario)   # no ?month= at all
    assert kw['is_all_time'] is False
    assert kw['selected_month'] == '2026-02'


def test_dashboard_empty_string_month_means_all_time(captured_dashboard_kwargs):
    kw = captured_dashboard_kwargs("month=", _seed_overspend_scenario)
    assert kw['is_all_time'] is True
    assert kw['selected_month'] == 'ทั้งหมด'


def test_dashboard_available_months_present(captured_dashboard_kwargs):
    kw = captured_dashboard_kwargs("month=2026-02", _seed_overspend_scenario)
    assert set(kw['available_months']) == {'2026-01', '2026-02'}
