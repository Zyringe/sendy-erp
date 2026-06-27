"""Phase 6 — Self-service payslip.

Task 6.1: get_employee_payslips — finalized-only helper.
Task 6.2: /me/payslip list + /me/payslip/<item_id> detail routes (security core).
Task 6.3: parameterize payslip back-button (manager flow unchanged).
Task 6.4: general allowlist + สลิป nav slot.
Task 6.5: per-actor security matrix.

Python 3.9 — Optional[...] not `X | None`.
"""
import os
import sqlite3

import pytest


# ── Shared seed helpers ───────────────────────────────────────────────────────

def _seed_payslips(db):
    """EMP004 = our employee. Returns (employee_id_A, employee_id_B).

    Creates:
      finalized run 901 (item 9001 for EMP004, item 9002 for EMP002),
      draft run 902 (item 9003 for EMP004).
    """
    c = sqlite3.connect(db)
    a = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    b = c.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    # Use 2099-xx year_months to avoid the UNIQUE(year_month,company_id)
    # constraint collision with real finalized runs (3='2026-05', 4='2026-04').
    c.executescript(f"""
        INSERT OR IGNORE INTO payroll_runs(id,year_month,company_id,status) VALUES
            (901,'2099-01',1,'finalized'), (902,'2099-02',1,'draft');
        INSERT OR IGNORE INTO payroll_items(id,run_id,employee_id,gross,net_pay) VALUES
            (9001,901,{a},20000,19000),
            (9002,901,{b},30000,28000),
            (9003,902,{a},20000,19000);
    """)
    c.commit(); c.close()
    return a, b


def _link(db, emp_code, uid):
    """Link an employee to a user_id."""
    c = sqlite3.connect(db)
    c.execute("UPDATE employees SET user_id=? WHERE emp_code=?", (uid, emp_code))
    c.commit(); c.close()


def _client(role, user_id):
    """Test client pre-logged-in as given role and user_id.
    Relies on tmp_db monkeypatching config/database.DATABASE_PATH."""
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


# ── Task 6.1 — get_employee_payslips (finalized only) ────────────────────────

def test_get_employee_payslips_finalized_only(tmp_db):
    """Own finalized run visible; own DRAFT and another employee's item hidden."""
    import hr_queries as hrq
    a, _ = _seed_payslips(tmp_db)
    # No explicit conn — uses get_connection() → monkeypatched DATABASE_PATH
    rows = hrq.get_employee_payslips(a)
    ids = [r["item_id"] for r in rows]
    assert 9001 in ids          # own finalized → visible
    assert 9003 not in ids      # own DRAFT → hidden
    assert 9002 not in ids      # another employee's item → never


# ── Task 6.2 — /me/payslip routes (security core) ────────────────────────────

def test_payslip_list_shows_only_own_finalized(tmp_db):
    """GET /me/payslip links own finalized items, never draft or other-employee."""
    # NOTE: tmp_db carries REAL payroll data (runs 3='2026-04', 4='2026-05' are
    # already finalized), so DO NOT assert on year-month strings — they can appear.
    # Key on the self-scoped item links.
    _seed_payslips(tmp_db); _link(tmp_db, 'EMP004', 2)
    html = _client('staff', 2).get('/me/payslip').get_data(as_text=True)
    assert '/me/payslip/9001' in html       # own FINALIZED item → linked
    assert '/me/payslip/9003' not in html   # own DRAFT item → never linked
    assert '/me/payslip/9002' not in html   # another employee's item → never


def test_payslip_detail_own_finalized_ok(tmp_db):
    """Detail page for own finalized item → 200."""
    _seed_payslips(tmp_db); _link(tmp_db, 'EMP004', 2)
    assert _client('staff', 2).get('/me/payslip/9001').status_code == 200


def test_payslip_detail_other_employee_403(tmp_db):
    """Detail page for another employee's item → 403."""
    _seed_payslips(tmp_db); _link(tmp_db, 'EMP004', 2)
    assert _client('staff', 2).get('/me/payslip/9002').status_code == 403


def test_payslip_detail_own_draft_403(tmp_db):
    """Detail page for own item in a DRAFT run → 403."""
    _seed_payslips(tmp_db); _link(tmp_db, 'EMP004', 2)
    assert _client('staff', 2).get('/me/payslip/9003').status_code == 403


# ── Task 6.3 — back-button regression (manager flow byte-identical) ───────────

def test_manager_payslip_still_links_to_run(tmp_db):
    """The manager-side payslip template still renders its own 'กลับ Payroll Run'
    back-link when back_url is not passed (manager flow is byte-identical)."""
    _seed_payslips(tmp_db)
    html = _client('admin', 1).get('/hr/payroll/901/payslip/9001').get_data(as_text=True)
    assert 'กลับ Payroll Run' in html
    assert '/hr/payroll/901' in html


# ── Task 6.4 — general allowlist + สลิป nav slot ─────────────────────────────

def test_general_can_access_payslip(tmp_db):
    """A 'general' role user can reach /me/payslip (the empty list), not redirect."""
    _seed_payslips(tmp_db)
    # EMP005 (บอล) linked to uid 9; no finalized items → empty list, still 200
    _link(tmp_db, 'EMP005', 9)
    assert _client('general', 9).get('/me/payslip').status_code == 200


def test_payslip_slot_in_mobile_nav():
    """build_mobile_nav_slots includes me.payslip_list for general, staff, and manager."""
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import build_mobile_nav_slots
    for role in ('general', 'staff', 'manager'):
        eps = [s['endpoint'] for s in build_mobile_nav_slots(role)]
        assert 'me.payslip_list' in eps, f"role={role} missing สลิป nav slot"
