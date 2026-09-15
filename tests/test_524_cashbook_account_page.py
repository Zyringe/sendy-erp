"""TDD for #524 — cashbook account page owns its actions.

Decided with Put in a grilling session (2026-09-14, every answer the
recommended option). Four independent pieces:

  1. The topbar "เพิ่มสินค้า" shortcut (base.html) only renders inside the
     คลังสินค้า (operation) module — today it leaks onto every page, cashbook
     included.
  2. `/cashbook/account/<id>` gets its own "เพิ่มรายการ" button next to
     "กลับ" (same `can_edit_cashbook` gate as the dashboard's), and
     `/cashbook/new?account_id=<id>` preselects that account (falling back to
     the user's default when the id isn't an active account — same as today).
  3. The account page defaults to the latest month THAT ACCOUNT has data in —
     NOT the dashboard's ledger-wide latest (ADR 0007's rule, scoped to one
     account). An explicit `?month=` (including ทั้งหมด, reusing the
     dashboard's own token) always wins, even over zero rows. Add/edit/delete
     redirect back to the month of the row(s) just touched. Card 3's label
     follows the mode: "คงเหลือ" all-time, "เข้า-ออกสุทธิ (YYYY-MM)" in a
     selected month — today it always says คงเหลือ, which is wrong once
     transfer rows are counted in a month figure.

Testing notes from the issue: scope every assertion to the topbar/element,
never page-wide ("เพิ่มสินค้า" is a substring of "เพิ่มสินค้าใหม่", the
product-form title; "เพิ่มรายการ" is the add-entry form's own title), and
pair every "absent" assertion with a control proving the area rendered.

See docs/adr/0007-cashbook-month-scope.md (the pattern this generalises) and
CONTEXT.md "Cashbook".
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest

import database

from tests.test_cashbook_month_scope import _seed_accounts, _insert_txns, _seed_via_path


# ── Shared fixtures / helpers ────────────────────────────────────────────────

@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _client_as_user(user_id, role):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _topbar_html(html):
    """The <header class="topbar">...</header> fragment only — excludes the
    page <h1> (which can itself contain "เพิ่มสินค้าใหม่", e.g. the product
    form's own page title) and the page body."""
    m = re.search(r'<header class="topbar">(.*?)</header>', html, re.DOTALL)
    assert m, "topbar header not found in rendered page"
    return m.group(1)


def _account_ids(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0"
        " ORDER BY id"
    ).fetchall()
    conn.close()
    if len(rows) < 2:
        pytest.skip("need >=2 active non-transfer cashbook accounts in live DB clone")
    return [r["id"] for r in rows]


def _insert_txn(db, account_id, txn_date, direction='expense', category='ทดสอบ 524',
                amount=100.0):
    conn = sqlite3.connect(db)
    txn_id = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, created_by)"
        " VALUES (?,?,?,?,?, 'seed')",
        (account_id, txn_date, direction, category, amount),
    ).lastrowid
    conn.commit()
    conn.close()
    return txn_id


# ── 1. topbar "เพิ่มสินค้า" scoped to the คลังสินค้า (operation) module ─────

def test_topbar_add_product_shown_on_operation_module_page(migrated_db):
    resp = _client_as_user(1, 'admin').get('/products')
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    topbar = _topbar_html(resp.get_data(as_text=True))
    assert 'ออกจากระบบ' in topbar, "control: topbar must have rendered at all"
    assert 'เพิ่มสินค้า' in topbar


def test_topbar_add_product_hidden_on_cashbook_page(migrated_db):
    resp = _client_as_user(1, 'admin').get('/cashbook/')
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    topbar = _topbar_html(resp.get_data(as_text=True))
    assert 'ออกจากระบบ' in topbar
    assert 'เพิ่มสินค้า' not in topbar


def test_topbar_add_product_hidden_on_hr_page(migrated_db):
    resp = _client_as_user(1, 'admin').get('/hr/')
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    topbar = _topbar_html(resp.get_data(as_text=True))
    assert 'ออกจากระบบ' in topbar
    assert 'เพิ่มสินค้า' not in topbar


# ── 2. account page's own "เพิ่มรายการ" (account preselected on /cashbook/new) ─

def test_account_ledger_shows_own_add_entry_button_preselecting_this_account(migrated_db):
    aid = _account_ids(migrated_db)[0]
    resp = _client_as_user(1, 'admin').get(f'/cashbook/account/{aid}')
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    topbar = _topbar_html(resp.get_data(as_text=True))
    assert 'กลับ' in topbar, "control: topbar must have rendered at all"
    assert 'เพิ่มรายการ' in topbar
    assert f'/cashbook/new?account_id={aid}' in topbar


def test_new_transaction_get_preselects_account_from_query_param(migrated_db):
    aid = _account_ids(migrated_db)[1]
    resp = _client_as_user(1, 'admin').get(f'/cashbook/new?account_id={aid}')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'value="{aid}" selected>' in html


def test_new_transaction_get_falls_back_to_default_on_inactive_account(migrated_db):
    # mig 126 seeds admin (user id 1) -> cashbook_accounts.code '392'.
    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    default_id = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code='392'"
    ).fetchone()["id"]
    inactive_id = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND id != ? ORDER BY id DESC LIMIT 1",
        (default_id,),
    ).fetchone()["id"]
    conn.execute("UPDATE cashbook_accounts SET is_active=0 WHERE id=?", (inactive_id,))
    conn.commit()
    conn.close()

    resp = _client_as_user(1, 'admin').get(f'/cashbook/new?account_id={inactive_id}')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'value="{default_id}" selected>' in html


# ── 3a. account page's own month default (kwargs-level, mirrors
#        test_cashbook_month_scope.py::captured_dashboard_kwargs) ───────────

def _seed_two_accounts_different_latest_months(conn):
    """OP (id=1): Jan + Feb 2026. IDLE (id=3): a single March 2026 row.

    Ledger-wide latest month is March (from IDLE) — if the account page's
    default ever regressed to the dashboard's own (unscoped) `_default_month`,
    OP would wrongly open on March instead of its own latest, Feb.
    """
    _seed_accounts(conn)
    _insert_txns(conn, [
        (1, '2026-01-05', 'expense', 'ค่าไฟ', None, 100.0),
        (1, '2026-02-10', 'expense', 'ค่าไฟ', None, 100.0),
        (3, '2026-03-15', 'expense', 'ค่าไฟ', None, 50.0),
    ])


@pytest.fixture
def captured_account_ledger_kwargs(tmp_db):
    """Call the real account_ledger() view function inside a request context
    and capture what it would have passed to render_template. Returns a
    callable `run(account_id, query_string, seed_fn)` -> captured kwargs."""
    from app import app as flask_app
    import blueprints.cashbook as cb

    def run(account_id, query_string, seed_fn):
        _seed_via_path(tmp_db, seed_fn)
        captured = {}

        def fake_render(_template_name, **kwargs):
            captured.update(kwargs)
            return ""

        orig = cb.render_template
        cb.render_template = fake_render
        try:
            with flask_app.test_request_context(
                f"/cashbook/account/{account_id}?{query_string}"
            ):
                cb.account_ledger(account_id)
        finally:
            cb.render_template = orig
        return captured

    return run


def test_account_default_month_is_scoped_to_that_account_not_the_ledger(captured_account_ledger_kwargs):
    kw = captured_account_ledger_kwargs(1, '', _seed_two_accounts_different_latest_months)
    assert kw['is_all_time'] is False
    assert kw['selected_month'] == '2026-02'   # OP's own latest, not IDLE's March


def test_account_default_month_falls_back_to_all_time_with_no_data(captured_account_ledger_kwargs):
    kw = captured_account_ledger_kwargs(3, '', _seed_accounts)   # IDLE: zero txns
    assert kw['is_all_time'] is True
    assert kw['selected_month'] == 'ทั้งหมด'


def test_account_explicit_month_wins_even_with_zero_rows_that_month(captured_account_ledger_kwargs):
    kw = captured_account_ledger_kwargs(
        1, 'month=2026-04', _seed_two_accounts_different_latest_months,
    )
    assert kw['is_all_time'] is False
    assert kw['selected_month'] == '2026-04'
    assert kw['total_count'] == 0


def test_account_thangmod_and_empty_string_both_mean_all_time(captured_account_ledger_kwargs):
    kw_word = captured_account_ledger_kwargs(
        1, 'month=ทั้งหมด', _seed_two_accounts_different_latest_months,
    )
    kw_blank = captured_account_ledger_kwargs(
        1, 'month=', _seed_two_accounts_different_latest_months,
    )
    assert kw_word['is_all_time'] is True
    assert kw_word['selected_month'] == 'ทั้งหมด'
    assert kw_blank['is_all_time'] is True
    assert kw_blank['selected_month'] == 'ทั้งหมด'
    assert kw_word['total_count'] == kw_blank['total_count'] == 2   # OP's Jan + Feb rows


# ── 3b. card 3 label follows the mode (render-level) ────────────────────────

def _card3_label(html):
    m = re.search(r'id="ledgerCard3Label"[^>]*>\s*(.*?)\s*</div>', html, re.DOTALL)
    assert m, "card 3 label element not found"
    return m.group(1).strip()


def test_account_ledger_card3_label_all_time_is_khongluea(migrated_db):
    aid = _account_ids(migrated_db)[0]
    _insert_txn(migrated_db, aid, '2026-05-01', direction='income', amount=500)
    resp = _client_as_user(1, 'admin').get(f'/cashbook/account/{aid}?month=ทั้งหมด')
    assert resp.status_code == 200
    assert _card3_label(resp.get_data(as_text=True)) == 'คงเหลือ'


def test_account_ledger_card3_label_month_mode_is_net_movement(migrated_db):
    aid = _account_ids(migrated_db)[0]
    _insert_txn(migrated_db, aid, '2026-05-01', direction='income', amount=500)
    resp = _client_as_user(1, 'admin').get(f'/cashbook/account/{aid}?month=2026-05')
    assert resp.status_code == 200
    assert _card3_label(resp.get_data(as_text=True)) == 'เข้า-ออกสุทธิ (2026-05)'


# ── 3c. add / edit / delete redirect to the month of the row(s) touched ─────

def test_add_single_row_redirects_to_that_rows_month(migrated_db):
    aid = _account_ids(migrated_db)[0]
    resp = _client_as_user(1, 'admin').post('/cashbook/new', data={
        'txn_date': '2026-05-20', 'account_id': str(aid),
        'rows-0-direction': 'expense', 'rows-0-category': 'ทดสอบ 524',
        'rows-0-amount': '150',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    loc = resp.headers['Location']
    assert f'account_id={aid}' in loc or f'/cashbook/account/{aid}' in loc
    assert 'month=2026-05' in loc


def test_bulk_add_redirects_to_latest_row_in_batch_not_first(migrated_db):
    aid = _account_ids(migrated_db)[0]
    resp = _client_as_user(1, 'admin').post('/cashbook/new', data={
        'txn_date': '2026-04-01', 'account_id': str(aid), 'bulk_mode': '1',
        'rows-0-direction': 'expense', 'rows-0-category': 'ทดสอบ A',
        'rows-0-amount': '10', 'rows-0-txn_date': '2026-04-05',
        'rows-1-direction': 'expense', 'rows-1-category': 'ทดสอบ B',
        'rows-1-amount': '20', 'rows-1-txn_date': '2026-06-15',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert 'month=2026-06' in resp.headers['Location']


def test_edit_redirects_to_new_dates_month(migrated_db):
    aid = _account_ids(migrated_db)[0]
    txn_id = _insert_txn(migrated_db, aid, '2026-02-10')
    resp = _client_as_user(1, 'admin').post(f'/cashbook/txn/{txn_id}/edit', data={
        'account_id': str(aid), 'txn_date': '2026-06-15', 'direction': 'expense',
        'category': 'ทดสอบ 524', 'amount': '100',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert 'month=2026-06' in resp.headers['Location']


def test_delete_redirects_to_deleted_rows_month(migrated_db):
    aid = _account_ids(migrated_db)[0]
    txn_id = _insert_txn(migrated_db, aid, '2026-07-08')
    resp = _client_as_user(1, 'admin').post(
        f'/cashbook/txn/{txn_id}/delete', follow_redirects=False,
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert 'month=2026-07' in resp.headers['Location']


# ── 3d. a dashboard link carries the dashboard's selected month ────────────

def _account_link_month(html, aid):
    """The `month=` value on the dashboard's "บัญชี" link for account `aid`,
    URL-decoded — robust against however Flask happens to encode ทั้งหมด."""
    m = re.search(rf'/cashbook/account/{aid}\?month=([^"&]+)"', html)
    assert m, f"no account link with a month= param found for account {aid}"
    from urllib.parse import unquote
    return unquote(m.group(1))


def test_dashboard_account_links_carry_the_dashboards_selected_month(migrated_db):
    aid = _account_ids(migrated_db)[0]
    _insert_txn(migrated_db, aid, '2026-03-05')
    resp = _client_as_user(1, 'admin').get('/cashbook/?month=2026-03')
    assert resp.status_code == 200
    assert _account_link_month(resp.get_data(as_text=True), aid) == '2026-03'


def test_dashboard_account_links_carry_thangmod_in_all_time_mode(migrated_db):
    aid = _account_ids(migrated_db)[0]
    resp = _client_as_user(1, 'admin').get('/cashbook/?month=ทั้งหมด')
    assert resp.status_code == 200
    assert _account_link_month(resp.get_data(as_text=True), aid) == 'ทั้งหมด'


def test_dashboard_link_to_account_with_no_rows_that_month_shows_empty_message_not_default(migrated_db):
    """The dashboard's chosen month beats the account's own default — even
    when that account has zero rows in it (must NOT silently fall back to the
    account's own latest month)."""
    aid = _account_ids(migrated_db)[0]
    # This account's only row is in a DIFFERENT month from the one we'll follow.
    _insert_txn(migrated_db, aid, '2026-01-10')
    resp = _client_as_user(1, 'admin').get(f'/cashbook/account/{aid}?month=2026-09')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ไม่มีรายการในช่วงนี้' in html
    select = re.search(r'<select name="month".*?</select>', html, re.DOTALL)
    assert select, "control: month select must have rendered"
    assert 'value="2026-09" selected' in select.group(0)


# ── 3e. paging keeps the resolved month in effect ───────────────────────────

def test_pagination_link_carries_selected_month(migrated_db):
    aid = _account_ids(migrated_db)[0]
    conn = sqlite3.connect(migrated_db)
    conn.executemany(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, created_by)"
        " VALUES (?,?,?,?,?, 'seed')",
        [(aid, f'2026-08-{(i % 28) + 1:02d}', 'expense', 'ทดสอบ 524', 10.0)
         for i in range(60)],   # per_page=50 -> forces a page 2
    )
    conn.commit()
    conn.close()

    resp = _client_as_user(1, 'admin').get(f'/cashbook/account/{aid}?month=2026-08')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    nav = re.search(r'<nav>(.*?)</nav>', html, re.DOTALL)
    assert nav, "control: pagination nav must have rendered (60 rows > 50/page)"
    assert 'month=2026-08' in nav.group(1)
