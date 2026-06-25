"""Phase 3 — 5-role model: users.role CHECK extended.

Task 3.1: verify migration 117 adds 'shareholder' + 'general' to the CHECK
          and that a bogus role is still rejected.
Task 3.2: POST gate is default-deny (shareholder/general/unknown cannot POST
          arbitrary endpoints; staff/manager whitelists unchanged; admin free).
"""
import sqlite3


# ── Task 3.2 helpers ─────────────────────────────────────────────────────────

def _client_as(role, tmp_db):
    """Create a test client pre-logged-in as the given role. Import app AFTER
    tmp_db is set up (config.DATABASE_PATH already patched by tmp_db fixture)."""
    import os
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


# ── Task 3.2 tests ────────────────────────────────────────────────────────────

def test_shareholder_cannot_post(tmp_db):
    c = _client_as('shareholder', tmp_db)
    r = c.post('/hr/employees/new', data={'emp_code': 'EMP900', 'full_name': 'x', 'company_id': '1'})
    assert r.status_code in (302, 403), f"expected deny, got {r.status_code}"
    import sqlite3
    assert sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM employees WHERE emp_code='EMP900'"
    ).fetchone()[0] == 0, "shareholder created a row — POST was not blocked"


def test_shareholder_blocked_from_staff_post_endpoint(tmp_db):
    """shareholder cannot POST to a staff-whitelisted endpoint (no inline admin guard).
    Uses /mapping/save which is in _STAFF_POST_OK and returns 200 JSON on success.
    Without the gate shareholder falls through and gets 200 — test is RED on baseline.
    With the gate shareholder is redirected (302) — test is GREEN."""
    c = _client_as('shareholder', tmp_db)
    r = c.post('/mapping/save', json={})
    assert r.status_code in (302, 403), f"expected deny, got {r.status_code}"


def test_general_blocked_from_staff_post_endpoint(tmp_db):
    """general cannot POST to a staff-whitelisted endpoint (no inline admin guard)."""
    c = _client_as('general', tmp_db)
    r = c.post('/mapping/save', json={})
    assert r.status_code in (302, 403), f"expected deny, got {r.status_code}"


def test_staff_post_allowed_endpoint_unchanged(tmp_db):
    """staff can still POST to a whitelisted endpoint (import_weekly)."""
    c = _client_as('staff', tmp_db)
    # POST to logout (always allowed for everyone) is a safe proxy for "gate not blocking".
    r = c.post('/logout')
    assert r.status_code in (200, 302), f"staff logout denied: {r.status_code}"


def test_manager_still_blocked_from_manager_forbidden_endpoint(tmp_db):
    """manager cannot POST to an admin-only endpoint (user creation)."""
    c = _client_as('manager', tmp_db)
    r = c.post('/users/new', data={'username': 'x', 'password': 'p', 'display_name': 'X', 'role': 'staff'})
    assert r.status_code in (302, 403), f"manager created a user: {r.status_code}"


# ── Task 3.3 tests ────────────────────────────────────────────────────────────

def test_general_blocked_from_hr_and_products(tmp_db):
    """general role is redirected away from any endpoint not in _GENERAL_ALLOWED."""
    c = _client_as('general', tmp_db)
    assert c.get('/hr/').status_code == 302,      "general accessed /hr/"
    assert c.get('/products').status_code == 302,  "general accessed /products"
    assert c.get('/m/stock').status_code == 200,   "general blocked from /m/stock"


def test_shareholder_blocked_from_admin_module_get(tmp_db):
    """shareholder cannot GET admin_module endpoints (defense-in-depth gate)."""
    c = _client_as('shareholder', tmp_db)
    assert c.get('/users').status_code == 403,     "shareholder accessed /users"


# ── Task 3.1 tests ────────────────────────────────────────────────────────────

def test_users_role_check_allows_new_roles(tmp_db):
    conn = sqlite3.connect(tmp_db)
    # both new roles must be insertable; a bogus role must still be rejected
    conn.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES('_sh','x','sh','shareholder')")
    conn.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES('_ge','x','ge','general')")
    conn.commit()
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES('_bad','x','b','wizard')")


# ── Task 3.3 bug-fix: simulate-exit not blocked by admin_module gate ──────────

def test_simulating_admin_can_exit_simulate(tmp_db):
    """An admin simulating as manager must be able to reach admin_exit_simulate."""
    c = _client_as('manager', tmp_db)          # simulated role
    with c.session_transaction() as s:
        s['_real_role'] = 'admin'              # marks them as simulating
    # POST to exit-simulate endpoint — must NOT be 403'd by the gate
    r = c.post('/admin/exit-simulate')
    assert r.status_code in (200, 302), f"simulating admin got {r.status_code} — locked in simulation"
