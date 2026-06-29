"""Impersonate-a-user (จำลอง → "ดูในมุมมองของ…").

An admin temporarily BECOMES a specific user: session user_id+role+display_name
swap to the target so identity-keyed pages (/me/*) show that person's data, and
the admin acts AS them. Real identity is stashed in _real_* and restored on exit;
exit is always reachable regardless of the impersonated role.
"""
import os
import sqlite3

import pytest


def _admin_client(tmp_db):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['display_name'] = 'Put'
        s['role'] = 'admin'
    return c


def _sess(client):
    with client.session_transaction() as s:
        return dict(s)


# Live-DB facts (fixture copies): user 1=admin(Put,EMP001), user 2=l(staff,EMP004 หลุย),
# user 10=mamaput(shareholder,EMP008). No general user exists → tests make one.


def test_impersonate_swaps_user_identity(tmp_db):
    c = _admin_client(tmp_db)
    c.post('/admin/simulate-role', data={'user_id': '2'})
    s = _sess(c)
    assert s['user_id'] == 2 and s['role'] == 'staff'
    assert s['display_name'] == 'Louis'            # target's display name
    assert s['_real_user_id'] == 1 and s['_real_role'] == 'admin'
    assert s['_real_display_name'] == 'Put'


def test_me_leave_resolves_to_target_not_admin(tmp_db):
    """The identity-keyed page must follow the impersonated user."""
    c = _admin_client(tmp_db)
    # As admin: /me/leave is exempt → redirect away (not 200).
    assert c.get('/me/leave').status_code == 302
    # Impersonating user 2 (staff, linked to EMP004): now it renders for them.
    c.post('/admin/simulate-role', data={'user_id': '2'})
    assert c.get('/me/leave').status_code == 200


def test_exit_restores_admin_identity(tmp_db):
    c = _admin_client(tmp_db)
    c.post('/admin/simulate-role', data={'user_id': '2'})
    c.post('/admin/exit-simulate')
    s = _sess(c)
    assert s['user_id'] == 1 and s['role'] == 'admin' and s['display_name'] == 'Put'
    assert '_real_user_id' not in s and '_real_role' not in s


def test_exit_reachable_while_impersonating_general(tmp_db):
    """The trap fix: a general/shareholder session must still be able to exit."""
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO users(username,password_hash,display_name,role) "
        "VALUES('gen1','x','พนักงานทดสอบ','general')")
    conn.commit()
    gid = conn.execute("SELECT id FROM users WHERE username='gen1'").fetchone()[0]
    conn.close()
    c = _admin_client(tmp_db)
    c.post('/admin/simulate-role', data={'user_id': str(gid)})
    assert _sess(c)['role'] == 'general'
    c.post('/admin/exit-simulate')           # would be blocked by general's gates w/o the fix
    assert _sess(c)['role'] == 'admin', "must be able to leave impersonation from general"


def test_reimpersonate_keeps_original_admin(tmp_db):
    c = _admin_client(tmp_db)
    c.post('/admin/simulate-role', data={'user_id': '2'})   # become user 2
    c.post('/admin/simulate-role', data={'user_id': '10'})  # switch to user 10 w/o exiting
    s = _sess(c)
    assert s['user_id'] == 10 and s['role'] == 'shareholder'
    assert s['_real_user_id'] == 1, "real identity must stay the ORIGINAL admin"
    assert s['_real_display_name'] == 'Put'


def test_non_admin_cannot_impersonate(tmp_db):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 2; s['username'] = 'l'; s['role'] = 'staff'
    assert c.post('/admin/simulate-role', data={'user_id': '9'}).status_code == 403


def test_cannot_impersonate_admin_target(tmp_db):
    c = _admin_client(tmp_db)
    c.post('/admin/simulate-role', data={'user_id': '1'})   # user 1 is admin
    assert _sess(c)['role'] == 'admin' and '_real_role' not in _sess(c), \
        "impersonating an admin target must be refused (no swap)"


def test_impersonate_logs_enter_under_real_admin(tmp_db, caplog):
    import logging
    c = _admin_client(tmp_db)
    with caplog.at_level(logging.INFO):
        c.post('/admin/simulate-role', data={'user_id': '2'})
    msgs = [r.getMessage() for r in caplog.records]
    assert any('IMPERSONATE enter' in m and 'admin' in m and 'user 2' in m for m in msgs), \
        "entering impersonation must log a trail under the real admin"
