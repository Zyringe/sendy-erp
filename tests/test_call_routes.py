"""Route-level POST tests for the call-card write endpoints.

The call_card unit tests cover the helpers; these exercise the REAL authed
request path (form parsing → helper → DB → redirect) for the 5 write routes,
the class of failure unit tests on the helpers can't catch (CSRF wiring, form
field names, redirect targets).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import call_card as cc


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'sanchai'
        sess['role'] = 'staff'
    return c


def _seed_customer(conn, code='ROUTETEST', name='ร้านเทสต์'):
    conn.execute("INSERT INTO customers(code, name) VALUES(?, ?)", (code, name))
    conn.commit()
    return code


def test_mark_called_route_writes_call_row(empty_db_conn):
    code = _seed_customer(empty_db_conn)
    r = _client().post(f'/call/{code}/mark-called')
    assert r.status_code in (302, 303)
    assert cc.last_called_at(empty_db_conn, code) is not None


def test_note_route_writes_note(empty_db_conn):
    code = _seed_customer(empty_db_conn)
    r = _client().post(f'/call/{code}/note', data={'body': 'โทรแล้วลูกค้าสนใจ'})
    assert r.status_code in (302, 303)
    rows = cc.get_log(empty_db_conn, code)
    assert len(rows) == 1
    assert rows[0]['kind'] == 'note'
    assert rows[0]['body'] == 'โทรแล้วลูกค้าสนใจ'


def test_note_route_with_flag_is_data_flag(empty_db_conn):
    code = _seed_customer(empty_db_conn)
    _client().post(f'/call/{code}/note', data={'flag': '1', 'body': 'ราคานี้น่าจะผิด'})
    rows = cc.get_log(empty_db_conn, code)
    assert rows[0]['kind'] == 'data_flag'


def test_crm_route_upserts_fields(empty_db_conn):
    code = _seed_customer(empty_db_conn)
    r = _client().post(f'/call/{code}/crm',
                       data={'tags': 'VIP', 'call_target_days': '90', 'next_call_date': ''})
    assert r.status_code in (302, 303)
    crm = cc.get_crm(empty_db_conn, code)
    assert crm['tags'] == 'VIP'
    assert crm['call_target_days'] == 90


def test_contact_route_updates_customer(empty_db_conn):
    code = _seed_customer(empty_db_conn)
    r = _client().post(f'/call/{code}/contact',
                       data={'phone': '081-234-5678', 'contact': 'คุณสมชาย', 'address': 'กรุงเทพ'})
    assert r.status_code in (302, 303)
    row = empty_db_conn.execute(
        "SELECT phone, contact, address FROM customers WHERE code=?", (code,)).fetchone()
    assert row['phone'] == '081-234-5678'
    assert row['contact'] == 'คุณสมชาย'
    assert row['address'] == 'กรุงเทพ'


def test_log_delete_route_soft_deletes_own_row(empty_db_conn):
    code = _seed_customer(empty_db_conn)
    cc.add_log(empty_db_conn, code, 'note', 'to delete', 'sanchai')
    log_id = cc.get_log(empty_db_conn, code)[0]['id']
    r = _client().post(f'/call/log/{log_id}/delete', data={'customer_code': code})
    assert r.status_code in (302, 303)
    assert cc.get_log(empty_db_conn, code) == []
