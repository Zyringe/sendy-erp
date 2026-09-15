"""TDD for #534 — cashbook account names, "รายรับบันทึกที่อื่น" flag, บันทึกถึง.

Three independent pieces, decided with Put 2026-09-15 (see CONTEXT.md
"Cashbook" and decisions/log.md):

1. `cashbook_accounts.display_name` (pre-existing column, never had a UI)
   is now editable on /cashbook-accounts and renders on the dashboard's
   per-account table + the account page's header/title, code small beside
   it, falling back to the code when unnamed.
2. `income_recorded_elsewhere` (migration 183): an account whose income is
   tracked outside the cashbook. Its all-history balance is excluded from
   the dashboard's all-time คงเหลือ headline (with a disclosure note) and
   shown as "—" in the per-account table; its EXPENSES still count
   everywhere. Its own account page drops the all-history คงเหลือ card in
   all-time mode; the month-mode เข้า-ออกสุทธิ figure (#524) is untouched.
3. "บันทึกถึง" — each account's latest txn_date, UNSCOPED by the dashboard's
   month filter (the point is surfacing entry lag even while viewing one
   month).

Testing notes from the issue: seed one flagged + one unflagged account with
rows, keep a control proving the unflagged one still renders its คงเหลือ, and
break-it-once on the dashboard's balance exclusion. "คงเหลือ" appears in
several places on these pages — every assertion below is scoped to its own
element/fragment, never page-wide.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest

import database


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_client(tmp_db):
    database.init_db()  # ensure migration 183 has run against this tmp_db copy
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _row_containing(html, marker, after='บัญชีดำเนินการ'):
    """The single <tr>...</tr> whose content includes `marker` — nearest
    preceding <tr> / nearest following </tr>. A non-greedy `<tr>.*?marker.*?
    </tr>` pattern is WRONG here: when an earlier sibling row sorts first in
    the table, the lazy `.*?` happily starts at THAT row's <tr> and spans
    through to marker, silently including the sibling's own cells.

    `after` skips past the headline disclosure note (which can ALSO name a
    flagged account's display_name, above the per-account table) so a name
    that appears in BOTH places resolves to the table row, not the note."""
    table_start = html.index(after)
    idx = html.index(marker, table_start)
    start = html.rfind('<tr>', 0, idx)
    assert start != -1, f"no <tr> found before marker {marker!r}"
    end = html.index('</tr>', idx) + len('</tr>')
    return html[start:end]


def _acct_by_code(db, code):
    return _conn(db).execute(
        "SELECT id, display_name, income_recorded_elsewhere FROM cashbook_accounts"
        " WHERE code=?", (code,)
    ).fetchone()


def _seed_flag_scenario(db):
    """OP (id=1, unflagged) + FLAG (id=2, flagged) — both non-transfer, so
    both land in op_accounts and differ ONLY on income_recorded_elsewhere.

    OP  : income 1000, expense 200  -> balance  800
    FLAG: income    0, expense 300  -> balance -300 (never keyed income, per
          the real ชฎามาศ scenario the issue describes)

    Unfiltered total_balance would be 800 + (-300) = 500; the correct,
    flag-aware headline is 800 (FLAG excluded). total_income/total_expense
    (the รายรับรวม/รายจ่ายรวม cards) must NOT be flag-filtered: 1000 / 500.
    """
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute(
        "INSERT INTO cashbook_accounts"
        " (id, code, display_name, is_active, is_transfer, income_recorded_elsewhere, sort_order)"
        " VALUES (1,'OP','บัญชีหลักทดสอบ',1,0,0,1)"
    )
    conn.execute(
        "INSERT INTO cashbook_accounts"
        " (id, code, display_name, is_active, is_transfer, income_recorded_elsewhere, sort_order)"
        " VALUES (2,'FLAG','บัญชีชฎามาศทดสอบ',1,0,1,2)"
    )
    rows = [
        (1, '2026-05-01', 'income',  'ยอดขายของ', None, 1000.0),
        (1, '2026-05-02', 'expense', 'ค่าไฟ',      None,  200.0),
        (2, '2026-05-03', 'expense', 'ค่าไฟ',      None,  300.0),
    ]
    conn.executemany(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount)"
        " VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _unnamed_no_flag_scenario(db):
    """A single unnamed, unflagged account — for the "no flagged accounts
    present" control (no disclosure note) and the "falls back to code" case."""
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute(
        "INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order)"
        " VALUES (1,'BARE',1,0,1)"
    )
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount)"
        " VALUES (1,'2026-05-01','income','ยอดขายของ',50.0)"
    )
    conn.commit()
    conn.close()


# ── A. Admin form: name + flag persist ──────────────────────────────────────

def test_create_sets_display_name_and_flag(admin_client, tmp_db):
    admin_client.post('/cashbook-accounts/new', data={
        'code': 'TEST-A', 'display_name': 'บัญชีทดสอบ A', 'income_recorded_elsewhere': '1',
    }, follow_redirects=True)
    row = _acct_by_code(tmp_db, 'TEST-A')
    assert row is not None
    assert row['display_name'] == 'บัญชีทดสอบ A'
    assert row['income_recorded_elsewhere'] == 1


def test_edit_updates_display_name_and_flag(admin_client, tmp_db):
    admin_client.post('/cashbook-accounts/new', data={'code': 'TEST-B'}, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'TEST-B')['id']
    admin_client.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TEST-B', 'display_name': 'บัญชีทดสอบ B', 'income_recorded_elsewhere': '1',
    }, follow_redirects=True)
    row = _acct_by_code(tmp_db, 'TEST-B')
    assert row['display_name'] == 'บัญชีทดสอบ B'
    assert row['income_recorded_elsewhere'] == 1


def test_edit_blank_display_name_clears_it(admin_client, tmp_db):
    """A submitted-but-EMPTY name field clears the name (falls back to code)
    — the real "un-name this account" UX, distinct from the key being absent."""
    admin_client.post('/cashbook-accounts/new', data={
        'code': 'TEST-C', 'display_name': 'มีชื่อ',
    }, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'TEST-C')['id']
    admin_client.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TEST-C', 'display_name': '',
    }, follow_redirects=True)
    assert _acct_by_code(tmp_db, 'TEST-C')['display_name'] is None


def test_edit_missing_display_name_key_preserves_existing_name(admin_client, tmp_db):
    """A POST that omits the display_name key entirely (never happens from
    the real always-rendered <input>, but a partial/programmatic POST can)
    must NOT wipe a stored name — the erp-engineering-discipline.md hazard
    named directly in the issue brief."""
    admin_client.post('/cashbook-accounts/new', data={
        'code': 'TEST-D', 'display_name': 'ชื่อเดิม',
    }, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'TEST-D')['id']
    admin_client.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TEST-D', 'bank_name': 'KBank',   # display_name key entirely absent
    }, follow_redirects=True)
    row = _acct_by_code(tmp_db, 'TEST-D')
    assert row['display_name'] == 'ชื่อเดิม'       # untouched


def test_edit_income_recorded_elsewhere_untick_to_0(admin_client, tmp_db):
    admin_client.post('/cashbook-accounts/new', data={
        'code': 'TEST-E', 'income_recorded_elsewhere': '1',
    }, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'TEST-E')['id']
    assert _acct_by_code(tmp_db, 'TEST-E')['income_recorded_elsewhere'] == 1
    admin_client.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TEST-E', 'income_recorded_elsewhere': '0',
    }, follow_redirects=True)
    assert _acct_by_code(tmp_db, 'TEST-E')['income_recorded_elsewhere'] == 0


def test_create_missing_income_recorded_elsewhere_key_defaults_to_0(admin_client, tmp_db):
    """CREATE path only: a brand-new row has nothing to preserve, so a
    missing key on /new is fine as 0 (same as is_transfer's own convention
    there). This is NOT the stale-form hazard F2 guards against below —
    that one is EDIT-only."""
    admin_client.post('/cashbook-accounts/new', data={'code': 'TEST-F'}, follow_redirects=True)
    assert _acct_by_code(tmp_db, 'TEST-F')['income_recorded_elsewhere'] == 0


def test_edit_stale_form_post_preserves_flag_and_name(admin_client, tmp_db):
    """review SHOULD-FIX 1 (money): a stale/partial edit POST — e.g. a
    browser tab still holding the form from BEFORE this feature shipped, or
    any caller sending only the old field set — must not silently wipe
    income_recorded_elsewhere back to 0. That flip would swing the
    dashboard's all-time คงเหลือ headline by the account's own balance
    (฿1.74M on prod for ชฎามาศ). Mirrors display_name's own missing-key
    preserve guard, same shape, same reason.

    Control in the SAME test: an explicit '0' POST still turns the flag off
    — this is a preserve-on-ABSENCE guard, not a "flag can never change"
    guard."""
    admin_client.post('/cashbook-accounts/new', data={
        'code': 'TEST-STALE', 'display_name': 'ชื่อเดิม', 'income_recorded_elsewhere': '1',
    }, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'TEST-STALE')['id']

    # The stale/partial POST: only `code` (required) + an unrelated field
    # (`note`) — display_name AND income_recorded_elsewhere keys both absent.
    admin_client.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TEST-STALE', 'note': 'แก้แค่หมายเหตุ',
    }, follow_redirects=True)
    row = _acct_by_code(tmp_db, 'TEST-STALE')
    assert row['display_name'] == 'ชื่อเดิม'          # preserved
    assert row['income_recorded_elsewhere'] == 1      # preserved — NOT wiped to 0

    # Control: an explicit, present '0' still works.
    admin_client.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TEST-STALE', 'income_recorded_elsewhere': '0',
    }, follow_redirects=True)
    assert _acct_by_code(tmp_db, 'TEST-STALE')['income_recorded_elsewhere'] == 0


def test_admin_list_shows_name_with_code_and_flag_badge(admin_client, tmp_db):
    admin_client.post('/cashbook-accounts/new', data={
        'code': 'TEST-G', 'display_name': 'บัญชีทดสอบ G', 'income_recorded_elsewhere': '1',
    }, follow_redirects=True)
    html = admin_client.get('/cashbook-accounts').data.decode('utf-8')
    assert 'บัญชีทดสอบ G' in html
    assert 'รายรับบันทึกที่อื่น' in html


# ── B. Dashboard: name+code rendering, falls back to code ──────────────────

def test_dashboard_table_shows_name_with_code(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    assert 'บัญชีหลักทดสอบ' in html
    assert '>OP<' in html or '>OP</span>' in html  # code still shown, small


def test_dashboard_table_falls_back_to_code_when_unnamed(admin_client, tmp_db):
    _unnamed_no_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    assert '<span class="badge bg-secondary">BARE</span>' in html


# ── C. "บันทึกถึง" — unscoped by the month filter ───────────────────────────

def test_dashboard_bantuek_thueng_shows_latest_txn_date_unscoped_by_month(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    conn = sqlite3.connect(tmp_db)
    # OP's true latest activity is June — even while viewing May, บันทึกถึง
    # for OP must still read the June date, not clip to the viewed month.
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount)"
        " VALUES (1,'2026-06-15','expense','ค่าไฟ',10.0)"
    )
    conn.commit()
    conn.close()

    html = admin_client.get('/cashbook/?month=2026-05').data.decode('utf-8')
    assert '2026-06-15' in html


def test_dashboard_bantuek_thueng_absent_for_idle_account(admin_client, tmp_db):
    """Control: an account with zero rows ever shows the '—' placeholder,
    not a crash and not some other account's date."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute(
        "INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order)"
        " VALUES (1,'IDLE',1,0,1)"
    )
    conn.commit()
    conn.close()
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    assert re.search(r'IDLE.*?<td class="small text-muted">—</td>', html, re.DOTALL)


# ── D. Flagged account: "—" balance cell with a hint ────────────────────────

def test_dashboard_balance_cell_shows_dash_for_flagged_account(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    row = _row_containing(html, 'บัญชีชฎามาศทดสอบ')
    balance_td = re.search(r'<td class="text-end fw-semibold small">.*?</td>', row, re.DOTALL)
    assert balance_td, "control: the คงเหลือ cell must have rendered"
    assert 'title="รายรับบันทึกที่อื่น' in balance_td.group(0)
    assert '—' in balance_td.group(0)
    assert '฿' not in balance_td.group(0)


def test_dashboard_balance_cell_unchanged_for_unflagged_account(admin_client, tmp_db):
    """Control (issue's own testing note): the unflagged account's คงเหลือ
    cell still shows a real number, not '—'."""
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    row = _row_containing(html, 'บัญชีหลักทดสอบ')
    balance_td = re.search(r'<td class="text-end fw-semibold small">.*?</td>', row, re.DOTALL)
    assert balance_td, "control: the คงเหลือ cell must have rendered"
    assert '฿800.00' in balance_td.group(0)
    assert '—' not in balance_td.group(0)


# ── E. Headline คงเหลือ excludes the flagged account, with a note ──────────

def test_dashboard_headline_balance_excludes_flagged_account(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    # Card 3 in all-time mode is labelled คงเหลือ (id-less, but it's the
    # 3rd stat-card-value on the page — scoped by extracting all of them).
    values = re.findall(r'stat-card-value[^"]*">\s*฿([\d,\.\-]+)\s*</div>', html)
    assert len(values) >= 3, "control: all 3 headline cards must have rendered"
    assert values[2].replace(',', '') == '800.00'   # NOT 500.00 (unfiltered sum)


def test_dashboard_headline_income_expense_include_flagged_accounts_expense(admin_client, tmp_db):
    """The issue's explicit carve-out: flagged account's EXPENSE still
    counts in the รายรับรวม/รายจ่ายรวม headline cards (only the balance
    reading is suppressed)."""
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    values = re.findall(r'stat-card-value[^"]*">\s*฿([\d,\.\-]+)\s*</div>', html)
    assert values[0].replace(',', '') == '1,000.00' or values[0].replace(',', '') == '1000.00'
    assert values[1].replace(',', '') == '500.00'   # 200 (OP) + 300 (FLAG)


def test_dashboard_headline_note_appears_when_a_flagged_account_exists(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    assert 'ไม่รวมบัญชีที่รายรับบันทึกที่อื่น' in html
    assert 'บัญชีชฎามาศทดสอบ' in html   # the note names the flagged account


def test_dashboard_headline_note_absent_with_no_flagged_accounts(admin_client, tmp_db):
    """Control: the note is not shown at all when nothing is flagged."""
    _unnamed_no_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/?month=ทั้งหมด').data.decode('utf-8')
    assert 'ไม่รวมบัญชีที่รายรับบันทึกที่อื่น' not in html


# ── F. Account page: header/title name, badge, no card in flagged all-time ─

def test_account_page_header_shows_name_with_code(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/account/2?month=ทั้งหมด').data.decode('utf-8')
    assert 'บัญชีชฎามาศทดสอบ' in html
    assert '<title>บัญชีชฎามาศทดสอบ – บัญชีรับ-จ่าย – Sendy</title>' in html
    assert 'รายรับบันทึกที่อื่น' in html   # badge


def test_account_page_falls_back_to_code_when_unnamed(admin_client, tmp_db):
    _unnamed_no_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/account/1?month=ทั้งหมด').data.decode('utf-8')
    assert '<title>BARE – บัญชีรับ-จ่าย – Sendy</title>' in html


def test_account_page_no_balance_card_for_flagged_account_all_time(admin_client, tmp_db):
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/account/2?month=ทั้งหมด').data.decode('utf-8')
    assert 'id="ledgerCard3Label"' not in html
    assert 'รายรับ (ที่กรอง)' in html, "control: the page itself rendered"


def test_account_page_balance_card_present_for_unflagged_account_all_time(admin_client, tmp_db):
    """Control: an ordinary account keeps its คงเหลือ card in all-time mode."""
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/account/1?month=ทั้งหมด').data.decode('utf-8')
    assert 'id="ledgerCard3Label"' in html
    assert '฿800.00' in html


def test_account_page_balance_card_present_for_flagged_account_month_mode(admin_client, tmp_db):
    """The #524 month-mode figure is untouched by the flag — only the
    ALL-TIME คงเหลือ reading is suppressed."""
    _seed_flag_scenario(tmp_db)
    html = admin_client.get('/cashbook/account/2?month=2026-05').data.decode('utf-8')
    assert 'id="ledgerCard3Label"' in html
    assert 'เข้า-ออกสุทธิ (2026-05)' in html
