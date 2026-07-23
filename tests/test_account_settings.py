"""Account settings + self-service change-password (/me/account, /me/change-password).

Method B: a new all-roles 'settings' module. Every logged-in role reaches its own
account page and can change its own password; the admin-only 'ระบบ' tools stay
locked (the regression guard below is the whole point of the module split).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest
from werkzeug.security import generate_password_hash, check_password_hash

from access_control import pw_fingerprint


# Real user rows carried in by the tmp_db copy of the live DB.
ADMIN = (1, 'admin'); MANAGER = (3, 's'); STAFF = (2, 'l')
SHARE = (10, 'mamaput'); GENERAL = (11, 'ballwtp1')


def _client(role, uid, un='u', real_role=None, pw_fp=None):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
        s['username'] = un
        s['display_name'] = un
        s['role'] = role
        if real_role:
            s['_real_role'] = real_role
        if pw_fp is not None:          # None => no fingerprint (grandfathered)
            s['pw_fp'] = pw_fp
    return c


def _set_pw(db_path, uid, pw):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(pw, method='pbkdf2:sha256'), uid))
    conn.commit(); conn.close()


def _hash(db_path, uid):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ── reachability: every role reaches its own account page ──────────────────
@pytest.mark.parametrize('role,uid,un', [
    ('admin', 1, 'admin'), ('manager', 3, 's'),
    ('staff', 2, 'l'), ('shareholder', 10, 'mamaput'), ('general', 11, 'ballwtp1'),
])
def test_account_page_reachable_all_roles(tmp_db, role, uid, un):
    r = _client(role, uid, un).get('/me/account')
    assert r.status_code == 200
    assert 'เปลี่ยนรหัสผ่าน'.encode() in r.data


def test_account_page_shows_identity(tmp_db):
    r = _client('staff', 2, 'l').get('/me/account')
    assert b'l' in r.data                      # username
    assert 'พนักงานออฟฟิศ'.encode() in r.data   # role label


# ── change password: happy path + every guard ─────────────────────────────
def test_change_password_success(tmp_db):
    _set_pw(tmp_db, 2, 'oldpass1')
    r = _client('staff', 2, 'l').post('/me/change-password', data={
        'current_password': 'oldpass1', 'new_password': 'newpass9',
        'confirm_password': 'newpass9'})
    assert r.status_code == 302
    assert check_password_hash(_hash(tmp_db, 2), 'newpass9')
    assert not check_password_hash(_hash(tmp_db, 2), 'oldpass1')


def test_change_password_wrong_current_rejected(tmp_db):
    _set_pw(tmp_db, 2, 'oldpass1')
    _client('staff', 2, 'l').post('/me/change-password', data={
        'current_password': 'WRONGPW', 'new_password': 'newpass9',
        'confirm_password': 'newpass9'})
    assert check_password_hash(_hash(tmp_db, 2), 'oldpass1')   # unchanged


def test_change_password_too_short_rejected(tmp_db):
    _set_pw(tmp_db, 2, 'oldpass1')
    _client('staff', 2, 'l').post('/me/change-password', data={
        'current_password': 'oldpass1', 'new_password': 'ab12',
        'confirm_password': 'ab12'})
    assert check_password_hash(_hash(tmp_db, 2), 'oldpass1')   # unchanged


def test_change_password_mismatch_rejected(tmp_db):
    _set_pw(tmp_db, 2, 'oldpass1')
    _client('staff', 2, 'l').post('/me/change-password', data={
        'current_password': 'oldpass1', 'new_password': 'newpass9',
        'confirm_password': 'other999'})
    assert check_password_hash(_hash(tmp_db, 2), 'oldpass1')   # unchanged


def test_change_password_blocked_while_impersonating(tmp_db):
    # Put impersonating staff (session is staff's, _real_role stashed): must not be
    # able to change the impersonated user's password (he doesn't hold their pw).
    _set_pw(tmp_db, 2, 'oldpass1')
    _client('staff', 2, 'l', real_role='admin').post('/me/change-password', data={
        'current_password': 'oldpass1', 'new_password': 'newpass9',
        'confirm_password': 'newpass9'})
    assert check_password_hash(_hash(tmp_db, 2), 'oldpass1')   # unchanged


# ── every role's POST reaches the route (POST whitelist + general GET gate) ─
@pytest.mark.parametrize('role,uid,un', [
    ('manager', 3, 's'), ('staff', 2, 'l'),
    ('shareholder', 10, 'mamaput'), ('general', 11, 'ballwtp1'),
])
def test_change_password_allowed_for_every_role(tmp_db, role, uid, un):
    _set_pw(tmp_db, uid, 'oldpass1')
    _client(role, uid, un).post('/me/change-password', data={
        'current_password': 'oldpass1', 'new_password': 'newpass9',
        'confirm_password': 'newpass9'})
    # password actually changed ⇒ the POST reached the route, not blocked by a gate
    assert check_password_hash(_hash(tmp_db, uid), 'newpass9')


# ── REGRESSION GUARD: opening the settings tab must NOT leak admin tools ────
@pytest.mark.parametrize('role,uid,un', [
    ('manager', 3, 's'), ('staff', 2, 'l'), ('shareholder', 10, 'mamaput')])
@pytest.mark.parametrize('path', ['/users', '/admin/backups'])
def test_admin_tools_stay_admin_only(tmp_db, role, uid, un, path):
    assert _client(role, uid, un).get(path).status_code == 403


def test_no_admin_nav_leak_on_account_page(tmp_db):
    r = _client('staff', 2, 'l').get('/me/account')
    assert 'ตั้งค่า'.encode() in r.data           # the new module is present
    assert 'จัดการผู้ใช้'.encode() not in r.data   # admin-only link is NOT


# ── Cross-session invalidation (a password change evicts other sessions) ────
def _evicted(resp):
    return resp.status_code == 302 and '/login' in resp.headers.get('Location', '')


def test_session_without_fp_is_grandfathered(tmp_db):
    # Pre-feature / test-injected sessions carry no pw_fp → NOT evicted (so the
    # deploy doesn't force a mass re-login and the existing suite is unaffected).
    assert _client('staff', 2, 'l').get('/me/account').status_code == 200


def test_session_with_current_fp_reaches_route(tmp_db):
    fp = pw_fingerprint(_hash(tmp_db, 2))
    assert _client('staff', 2, 'l', pw_fp=fp).get('/me/account').status_code == 200


def test_stale_fp_session_is_evicted(tmp_db):
    assert _evicted(_client('staff', 2, 'l', pw_fp='deadbeef' * 4).get('/me/account'))


def test_password_change_evicts_others_keeps_changer(tmp_db):
    _set_pw(tmp_db, 2, 'oldpass1')
    old_fp = pw_fingerprint(_hash(tmp_db, 2))
    changer = _client('staff', 2, 'l', pw_fp=old_fp)
    r = changer.post('/me/change-password', data={
        'current_password': 'oldpass1', 'new_password': 'newpass9',
        'confirm_password': 'newpass9'})
    assert r.status_code == 302
    # changer's session was re-stamped → still reaches an authed route
    assert changer.get('/me/account').status_code == 200
    # a SECOND session for the same user, holding the OLD fingerprint, is evicted
    assert _evicted(_client('staff', 2, 'l', pw_fp=old_fp).get('/me/account'))


def test_admin_reset_evicts_target_sessions(tmp_db):
    # Target logged in (stamped); an admin reset changes the hash out-of-band →
    # the target's existing session is evicted on its next request (no me.py
    # change needed — any password_hash rotation trips the fingerprint).
    _set_pw(tmp_db, 2, 'oldpass1')
    target = _client('staff', 2, 'l', pw_fp=pw_fingerprint(_hash(tmp_db, 2)))
    assert target.get('/me/account').status_code == 200
    _set_pw(tmp_db, 2, 'adminreset9')                 # simulates /users password reset
    assert _evicted(target.get('/me/account'))


def test_impersonation_skips_staleness(tmp_db):
    # While impersonating, session pw_fp is the real admin's (won't match the
    # impersonated user's hash) — the check is skipped, so it must NOT evict.
    r = _client('staff', 2, 'l', real_role='admin', pw_fp='deadbeef' * 4).get('/me/account')
    assert r.status_code == 200
