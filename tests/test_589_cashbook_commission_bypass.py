"""#589 — a keyer blocked from paying an in-engine rep's commission in the
cashbook must see WHERE to pay it, and must not route around the block by
filing the same payment under another category without being told.

Prod row 845 is the case: ฿400 to ทวีเกียรติ (ท/03) with description
'คอมมิชชั่น' filed under อื่นๆ after the จ่ายค่าคอมมิชชั่น block refused it.
Prod row 306 ('ค่าซ่อมคอม', a computer repair) is the wording that must NOT
trip the warning: "คอม" alone is ambiguous in Thai.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import lxml.html
import pytest

import database

COMMISSION_CATEGORY = 'จ่ายค่าคอมมิชชั่น'
OTHER = 'อื่นๆ'
DRILLDOWN_03 = '/commission/sp/03'


@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'test-admin'
        s['display_name'] = 'Test Admin'; s['role'] = 'admin'
    return c


def _account(db):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0 "
                       "ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip('no active non-transfer cashbook account')
    return row[0]


def _count(db, date, amount):
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date=? AND amount=?",
                     (date, amount)).fetchone()[0]
    conn.close()
    return n


def _row(i, category, user, amount, desc):
    return {f'rows-{i}-direction': 'expense', f'rows-{i}-category': category,
            f'rows-{i}-user_category': user, f'rows-{i}-amount': str(amount),
            f'rows-{i}-description': desc}


def _alerts(html, kind):
    tree = lxml.html.fromstring(html)
    return tree.xpath(f"//div[contains(concat(' ', @class, ' '), ' alert-{kind} ')]")


def _links_in(elements, href):
    return [a for el in elements for a in el.xpath('.//a') if a.get('href') == href]


# ── the wording matcher ──────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('คอมมิชชั่น', True),                       # prod row 845
    ('คอมมิชชั่นทวีเกียรติ 5/26', True),          # prod rows 345 / 639
    ('ค่าคอม + พิเศษ (1,000) 3/26', True),       # prod row 298
    ('คอมมิชชัน', True),
    ('Commission 9/26', True),
    ('ค่าซ่อมคอม', False),                      # prod row 306 — computer repair
    ('ซ่อมคอม', False),                         # prod row 640
    ('ค่าคอมพิวเตอร์', False),
    ('', False),
])
def test_commission_wording(text, expected):
    from blueprints.cashbook import _names_a_commission
    assert _names_a_commission(text) is expected


# ── the block now says where to go ───────────────────────────────────────────

def test_block_message_links_to_the_reps_commission_page(migrated_db):
    account_id = _account(migrated_db)
    form = {'txn_date': '2031-01-10', 'account_id': str(account_id),
            **_row(0, COMMISSION_CATEGORY, 'ทวีเกียรติ', 401, 'คอมมิชชั่น')}
    resp = _client().post('/cashbook/new', data=form)
    assert resp.status_code == 200
    assert _count(migrated_db, '2031-01-10', 401) == 0

    html = resp.get_data(as_text=True)
    danger = _alerts(html, 'danger')
    assert any('หน้าคอมมิชชั่น' in a.text_content() for a in danger), 'control: the block fired'
    assert len(_links_in(danger, DRILLDOWN_03)) == 1, 'the flash carries a real link, not escaped text'
    row_error = lxml.html.fromstring(html).xpath("//tr[contains(@class,'table-danger')]/td")
    assert len(_links_in(row_error, DRILLDOWN_03)) == 1, 'the row error carries it too'


def test_bulk_skip_summary_keeps_the_link_as_markup(migrated_db):
    account_id = _account(migrated_db)
    form = {'txn_date': '2031-01-11', 'account_id': str(account_id), 'bulk_mode': '1',
            'confirm_new_categories': '1',
            **_row(0, COMMISSION_CATEGORY, 'ทวีเกียรติ', 402, 'คอมมิชชั่น'),
            **_row(1, 'ทดสอบ 589 bulk', '', 55, 'ของใช้')}
    resp = _client().post('/cashbook/new', data=form, follow_redirects=True)
    assert resp.status_code == 200
    assert _count(migrated_db, '2031-01-11', 402) == 0
    assert _count(migrated_db, '2031-01-11', 55) == 1, 'control: the valid row saved'

    warning = _alerts(resp.get_data(as_text=True), 'warning')
    assert any('ข้าม 1 แถว' in a.text_content() for a in warning)
    assert len(_links_in(warning, DRILLDOWN_03)) == 1


# ── the off-category warning ─────────────────────────────────────────────────

def test_row_845_shape_warns_and_saves_nothing_until_confirmed(migrated_db):
    account_id = _account(migrated_db)
    base = {'txn_date': '2031-01-12', 'account_id': str(account_id),
            **_row(0, OTHER, 'ทวีเกียรติ', 403, 'คอมมิชชั่น')}
    c = _client()

    resp = c.post('/cashbook/new', data=base)
    assert resp.status_code == 200
    assert _count(migrated_db, '2031-01-12', 403) == 0, 'nothing saved before the confirm'
    html = resp.get_data(as_text=True)
    tree = lxml.html.fromstring(html)
    assert len(tree.xpath("//input[@type='checkbox'][@name='confirm_commission_elsewhere']")) == 1
    assert any('ลงหมวดอื่น' in a.text_content() for a in _alerts(html, 'warning'))
    warned_row = tree.xpath("//tr[contains(@class,'table-warning')]/td")
    assert len(_links_in(warned_row, DRILLDOWN_03)) == 1, 'the row says where to pay it instead'

    resp = c.post('/cashbook/new', data={**base, 'confirm_commission_elsewhere': '1'})
    assert resp.status_code == 302
    assert _count(migrated_db, '2031-01-12', 403) == 1, 'a confirmed row saves as keyed'


def test_row_306_wording_does_not_warn_for_the_same_rep(migrated_db):
    account_id = _account(migrated_db)
    form = {'txn_date': '2031-01-13', 'account_id': str(account_id),
            **_row(0, OTHER, 'ทวีเกียรติ', 404, 'ค่าซ่อมคอม')}
    resp = _client().post('/cashbook/new', data=form)
    assert resp.status_code == 302, 'same rep, same category: only the wording differs from 845'
    assert _count(migrated_db, '2031-01-13', 404) == 1


def test_off_system_rep_with_commission_wording_does_not_warn(migrated_db):
    account_id = _account(migrated_db)
    form = {'txn_date': '2031-01-14', 'account_id': str(account_id),
            **_row(0, OTHER, 'อัคเรศ', 405, 'คอมมิชชั่น')}
    resp = _client().post('/cashbook/new', data=form)
    assert resp.status_code == 302, 'อัคเรศ is off-system: the cashbook is their home (ADR 0008)'
    assert _count(migrated_db, '2031-01-14', 405) == 1


def test_confirm_survives_a_second_gate(migrated_db):
    """#532 lesson: a confirmation given on one gate must ride through the
    re-render of another gate, or the batch can never be saved."""
    account_id = _account(migrated_db)
    form = {'txn_date': '2031-01-15', 'account_id': str(account_id),
            'confirm_commission_elsewhere': '1',
            **_row(0, 'ทดสอบ 589 หมวดใหม่', 'ทวีเกียรติ', 406, 'คอมมิชชั่น')}
    resp = _client().post('/cashbook/new', data=form)
    assert resp.status_code == 200, 'the new-category gate stops this render'
    tree = lxml.html.fromstring(resp.get_data(as_text=True))
    assert len(tree.xpath("//input[@name='confirm_new_categories'][@type='checkbox']")) == 1, \
        'control: it is the new-category gate that rendered'
    assert len(tree.xpath(
        "//input[@type='hidden'][@name='confirm_commission_elsewhere'][@value='1']")) == 1
