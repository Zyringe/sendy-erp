"""Leave writes are validated at the write layer, once, for every caller.

Admin (`/hr/leave/new`, `/hr/leave/<id>/edit`) and self-service
(`/me/leave/new`, `/me/leave/<rid>/edit`) both passed form values straight
through; `create_leave_request` only cast `days` to float and migration 054
declares the dates as unconstrained TEXT. So a blank or malformed date, a
reversed range, or a non-positive `days` could be stored — and
`_compute_unpaid_days` then parses those dates while generating payroll for the
WHOLE company, and a non-positive `days` evades the unpaid-leave deduction once
approved.

The validator lives in `hr_queries` (the write layer both paths already funnel
through), so a new caller cannot route around it by construction.

Half-days are legitimate (`test_half_day_unpaid` in test_hr_payroll.py depends
on them), so the day-count rule is `0 < days <= inclusive span`, not integers.
"""
import os
import sqlite3

import pytest

import hr
import hr_queries as hrq


# ── helpers ──────────────────────────────────────────────────────────────────

def _leave_type_id(conn, code='SICK'):
    return conn.execute(
        "SELECT id FROM leave_types WHERE code=?", (code,)
    ).fetchone()[0]


def _emp_id(conn, emp_code='EMP002'):
    return conn.execute(
        "SELECT id FROM employees WHERE emp_code=?", (emp_code,)
    ).fetchone()[0]


def _payload(conn, **over):
    d = {
        "employee_id": _emp_id(conn),
        "leave_type_id": _leave_type_id(conn),
        "start_date": "2026-03-10",
        "end_date": "2026-03-12",
        "days": 3,
        "status": "approved",
    }
    d.update(over)
    return d


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0]


INVALID = [
    pytest.param({"start_date": ""}, id="blank-start"),
    pytest.param({"end_date": ""}, id="blank-end"),
    pytest.param({"start_date": None}, id="null-start"),
    pytest.param({"start_date": "10/03/2026"}, id="non-iso-start"),
    pytest.param({"end_date": "2026-13-40"}, id="impossible-end"),
    pytest.param({"start_date": "2026-03-12", "end_date": "2026-03-10"},
                 id="reversed-range"),
    pytest.param({"days": 0}, id="zero-days"),
    pytest.param({"days": -2}, id="negative-days"),
    pytest.param({"days": "abc"}, id="non-numeric-days"),
    pytest.param({"days": ""}, id="blank-days"),
    pytest.param({"start_date": "2026-03-10", "end_date": "2026-03-10",
                  "days": 5}, id="more-days-than-span"),
    pytest.param({"days": 3.5}, id="fractionally-over-span"),
]

VALID = [
    pytest.param({"days": 3}, id="exact-span"),
    pytest.param({"days": 2}, id="fewer-than-span"),
    pytest.param({"start_date": "2026-03-10", "end_date": "2026-03-10",
                  "days": 0.5}, id="half-day"),
    pytest.param({"start_date": "2026-03-10", "end_date": "2026-03-10",
                  "days": 1}, id="single-day"),
    pytest.param({"days": 2.5}, id="half-day-inside-a-range"),
]


# ── engine (write layer) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", INVALID)
def test_create_rejects_invalid_payload(tmp_db_conn, bad):
    before = _count(tmp_db_conn)
    with pytest.raises(ValueError):
        hrq.create_leave_request(_payload(tmp_db_conn, **bad), 1,
                                 conn=tmp_db_conn)
    assert _count(tmp_db_conn) == before, "a rejected create must not insert"


@pytest.mark.parametrize("ok", VALID)
def test_create_accepts_valid_payload(tmp_db_conn, ok):
    before = _count(tmp_db_conn)
    rid = hrq.create_leave_request(_payload(tmp_db_conn, **ok), 1,
                                   conn=tmp_db_conn)
    assert rid is not None
    assert _count(tmp_db_conn) == before + 1


@pytest.mark.parametrize("bad", INVALID)
def test_update_rejects_invalid_payload(tmp_db_conn, bad):
    rid = hrq.create_leave_request(_payload(tmp_db_conn), 1, conn=tmp_db_conn)
    before = dict(tmp_db_conn.execute(
        "SELECT * FROM leave_requests WHERE id=?", (rid,)).fetchone())

    with pytest.raises(ValueError):
        hrq.update_leave_request(rid, _payload(tmp_db_conn, **bad),
                                 conn=tmp_db_conn)

    after = dict(tmp_db_conn.execute(
        "SELECT * FROM leave_requests WHERE id=?", (rid,)).fetchone())
    assert after == before, "a rejected edit must not mutate the row"


def test_status_only_update_is_not_validated(tmp_db_conn):
    """The approve/reject path sends no dates — it must keep working (it is a
    different branch of update_leave_request and validating it would break
    every approval)."""
    rid = hrq.create_leave_request(
        _payload(tmp_db_conn, status='pending'), 1, conn=tmp_db_conn)

    hrq.update_leave_request(rid, {"status": "approved",
                                   "approved_by": "tester",
                                   "approved_at": "2026-03-13 09:00:00"},
                             conn=tmp_db_conn)

    assert tmp_db_conn.execute(
        "SELECT status FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()[0] == 'approved'


def test_payroll_generation_survives_because_nothing_invalid_is_stored(
        tmp_db_conn_hr_clean):
    """The consequence the validator exists to prevent: a blank-date row makes
    _compute_unpaid_days parse '' while generating payroll for the whole
    company. With the write layer refusing it, the row never exists."""
    conn = tmp_db_conn_hr_clean
    with pytest.raises(ValueError):
        hrq.create_leave_request(
            _payload(conn, start_date="", end_date=""), 1, conn=conn)

    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    assert run is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM payroll_items WHERE run_id=?", (run['id'],)
    ).fetchone()[0] > 0


# ── admin routes ─────────────────────────────────────────────────────────────

def _admin_client():
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c


def _count_path(db):
    conn = sqlite3.connect(db)
    try:
        return _count(conn)
    finally:
        conn.close()


def test_admin_create_route_rejects_blank_dates(tmp_db):
    before = _count_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    lt = _leave_type_id(conn); emp = _emp_id(conn); conn.close()

    resp = _admin_client().post('/hr/leave/new', data={
        'employee_id': emp, 'leave_type_id': lt,
        'start_date': '', 'end_date': '', 'days': '1',
    })

    assert resp.status_code == 200, "re-renders the form rather than redirecting"
    assert _count_path(tmp_db) == before, "no row may be written"


def test_admin_create_route_rejects_impossible_day_count(tmp_db):
    before = _count_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    lt = _leave_type_id(conn); emp = _emp_id(conn); conn.close()

    _admin_client().post('/hr/leave/new', data={
        'employee_id': emp, 'leave_type_id': lt,
        'start_date': '2026-03-10', 'end_date': '2026-03-10', 'days': '9',
    })

    assert _count_path(tmp_db) == before


def test_admin_create_route_accepts_a_valid_request(tmp_db):
    before = _count_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    lt = _leave_type_id(conn); emp = _emp_id(conn); conn.close()

    resp = _admin_client().post('/hr/leave/new', data={
        'employee_id': emp, 'leave_type_id': lt,
        'start_date': '2026-03-10', 'end_date': '2026-03-12', 'days': '3',
    })

    assert resp.status_code == 302
    assert _count_path(tmp_db) == before + 1


# ── self-service routes ──────────────────────────────────────────────────────

def _staff_client(uid):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
        s['username'] = 'test-staff'
        s['role'] = 'staff'
    return c


def _link(db, emp_code, uid):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE employees SET user_id=? WHERE emp_code=?", (uid, emp_code))
    conn.commit(); conn.close()


def test_self_service_create_rejects_invalid_payload(tmp_db):
    _link(tmp_db, 'EMP002', 2)
    before = _count_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    lt = _leave_type_id(conn); conn.close()

    resp = _staff_client(2).post('/me/leave/new', data={
        'leave_type_id': lt,
        'start_date': '2026-03-12', 'end_date': '2026-03-10', 'days': '3',
    })

    assert resp.status_code in (200, 302), "must not 500 on a bad payload"
    assert _count_path(tmp_db) == before, "no row may be written"


def test_self_service_create_accepts_a_valid_request(tmp_db):
    _link(tmp_db, 'EMP002', 2)
    before = _count_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    lt = _leave_type_id(conn); conn.close()

    resp = _staff_client(2).post('/me/leave/new', data={
        'leave_type_id': lt,
        'start_date': '2026-03-10', 'end_date': '2026-03-12', 'days': '3',
    })

    assert resp.status_code == 302
    assert _count_path(tmp_db) == before + 1
    conn = sqlite3.connect(tmp_db)
    status = conn.execute(
        "SELECT status FROM leave_requests ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert status == 'pending', "self-service submissions stay pending"


def test_self_service_edit_rejects_invalid_payload(tmp_db):
    _link(tmp_db, 'EMP002', 2)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rid = hrq.create_leave_request(
        _payload(conn, status='pending'), 1, conn=conn)
    lt = _leave_type_id(conn)
    before = dict(conn.execute(
        "SELECT * FROM leave_requests WHERE id=?", (rid,)).fetchone())
    conn.close()

    resp = _staff_client(2).post(f'/me/leave/{rid}/edit', data={
        'leave_type_id': lt,
        'start_date': '2026-03-10', 'end_date': '2026-03-10', 'days': '0',
    })

    assert resp.status_code in (200, 302)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    after = dict(conn.execute(
        "SELECT * FROM leave_requests WHERE id=?", (rid,)).fetchone())
    conn.close()
    assert after == before


def test_self_service_ownership_still_wins_over_validation(tmp_db):
    """An invalid payload aimed at SOMEONE ELSE's request must still be a 403 —
    the ownership guard runs first, so validation never becomes an oracle for
    which request ids exist."""
    _link(tmp_db, 'EMP002', 2)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    other = _emp_id(conn, 'EMP004')
    rid = hrq.create_leave_request(
        _payload(conn, employee_id=other, status='pending'), 1, conn=conn)
    lt = _leave_type_id(conn)
    conn.close()

    resp = _staff_client(2).post(f'/me/leave/{rid}/edit', data={
        'leave_type_id': lt,
        'start_date': '', 'end_date': '', 'days': '0',
    })

    assert resp.status_code == 403


def test_self_service_can_cancel_a_legacy_invalid_row(tmp_db):
    """Cancelling must not be blocked by the new rules.

    Cancel used to resend the whole row (dates, days) through the full-edit
    path, so validating that path turned "cancel" into a 500 for any pending
    row that predates the validator — exactly the rows a user most wants gone.
    Cancelling has no business rewriting the dates anyway.
    """
    _link(tmp_db, 'EMP002', 2)
    conn = sqlite3.connect(tmp_db)
    emp = _emp_id(conn)
    lt = _leave_type_id(conn)
    # Written directly, as a pre-validator row would have been.
    conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status)
           VALUES (?, ?, '2026-04-10', '2026-04-10', 9, 'pending')""",
        (emp, lt))
    conn.commit()
    rid = conn.execute(
        "SELECT id FROM leave_requests ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()

    resp = _staff_client(2).post(f'/me/leave/{rid}/cancel', data={},
                                 follow_redirects=False)

    assert resp.status_code == 302, f"cancel must not 500 (got {resp.status_code})"
    conn = sqlite3.connect(tmp_db)
    status = conn.execute(
        "SELECT status FROM leave_requests WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert status == 'cancelled'


def test_self_service_pending_rule_still_wins_over_validation(tmp_db):
    """Same for the pending-status rule: an approved request is untouchable
    regardless of what the payload says."""
    _link(tmp_db, 'EMP002', 2)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rid = hrq.create_leave_request(
        _payload(conn, status='approved'), 1, conn=conn)
    lt = _leave_type_id(conn)
    conn.close()

    resp = _staff_client(2).post(f'/me/leave/{rid}/edit', data={
        'leave_type_id': lt,
        'start_date': '', 'end_date': '', 'days': '0',
    })

    assert resp.status_code == 403
