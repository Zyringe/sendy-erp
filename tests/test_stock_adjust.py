"""Stock-adjust route + add_transaction created_at tests."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')
import sqlite3
from datetime import date, timedelta
import pytest


def _first_active_product_id(db) -> int:
    row = sqlite3.connect(db).execute(
        "SELECT id FROM products WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("No active products in live DB clone")
    return row[0]


def _latest_txn(db, pid):
    return sqlite3.connect(db).execute(
        "SELECT txn_type, quantity_change, note, created_at FROM transactions "
        "WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()


def test_add_transaction_custom_created_at(tmp_db):
    import models
    pid = _first_active_product_id(tmp_db)
    models.add_transaction(pid, 'ADJUST', 1, 'unit', note='นับสต๊อก',
                           created_at='2025-01-15 00:00:00')
    row = _latest_txn(tmp_db, pid)
    assert row[0] == 'ADJUST'
    assert row[3] == '2025-01-15 00:00:00'


def test_add_transaction_default_created_at_is_now(tmp_db):
    import models
    pid = _first_active_product_id(tmp_db)
    models.add_transaction(pid, 'ADJUST', 1, 'unit', note='นับสต๊อก')
    row = _latest_txn(tmp_db, pid)
    assert row[3].startswith(date.today().isoformat())


@pytest.fixture
def staff_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 2; sess['username'] = 'test-staff'; sess['role'] = 'staff'
    return c


@pytest.fixture
def manager_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 3; sess['username'] = 'test-mgr'; sess['role'] = 'manager'
    return c


def _current(db, pid):
    r = sqlite3.connect(db).execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return r[0] if r else 0


def _txn_count(db, pid):
    return sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]


def test_staff_can_adjust_count(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 2
    resp = staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'count', 'adjust_date': '2020-01-01'})
    assert resp.status_code in (302, 303)
    row = _latest_txn(tmp_db, pid)
    assert row[0] == 'ADJUST' and row[2] == 'นับสต๊อก'
    # count ignores the submitted backdate → stamped today/now
    assert row[3].startswith(date.today().isoformat())


def test_manager_can_adjust(manager_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 3
    resp = manager_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'correction', 'adjust_date': date.today().isoformat()})
    assert resp.status_code in (302, 303)
    assert _latest_txn(tmp_db, pid)[2] == 'แก้ยอดผิด'


def test_backdate_non_count_lands_on_date(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) - 1
    past = (date.today() - timedelta(days=10)).isoformat()
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'damaged', 'adjust_date': past})
    row = _latest_txn(tmp_db, pid)
    assert row[2] == 'ชำรุด / แตกหัก'
    assert row[3] == f'{past} 00:00:00'


def test_today_non_count_stamps_now(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 5
    today = date.today().isoformat()
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'lost', 'adjust_date': today})
    ca = _latest_txn(tmp_db, pid)[3]
    assert ca.startswith(today) and ca != f'{today} 00:00:00'  # has a real time


def test_other_requires_text(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    new_q = _current(tmp_db, pid) + 1
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'other', 'note_other': '   ',
              'adjust_date': date.today().isoformat()})
    assert _txn_count(tmp_db, pid) == before  # rejected, no row


def test_other_with_text_stores_verbatim(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 1
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'other', 'note_other': 'ยกชุดไปงาน',
              'adjust_date': date.today().isoformat()})
    assert _latest_txn(tmp_db, pid)[2] == 'ยกชุดไปงาน'


def test_future_date_rejected(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    new_q = _current(tmp_db, pid) + 1
    future = (date.today() + timedelta(days=3)).isoformat()
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'damaged', 'adjust_date': future})
    assert _txn_count(tmp_db, pid) == before


def test_invalid_reason_rejected(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    new_q = _current(tmp_db, pid) + 1
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'bogus', 'adjust_date': date.today().isoformat()})
    assert _txn_count(tmp_db, pid) == before


def test_zero_diff_no_row(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    same = _current(tmp_db, pid)
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': same, 'reason': 'count'})
    assert _txn_count(tmp_db, pid) == before


def test_detail_page_has_adjust_modal(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    body = staff_client.get(f'/products/{pid}').get_data(as_text=True)
    assert 'id="adjustModal"' in body
    assert 'นับสต๊อก' in body
    assert 'name="reason"' in body
    assert 'name="adjust_date"' in body


def test_adjust_fallback_page_renders(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    r = staff_client.get(f'/products/{pid}/adjust')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="reason"' in body
    assert '<textarea name="note"' not in body  # old free-text note removed


def test_alerts_page_renders_for_staff(staff_client, tmp_db):
    r = staff_client.get('/alerts')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="reason"' in body  # modal + partial now visible to staff
