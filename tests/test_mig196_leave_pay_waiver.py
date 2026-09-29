"""Migration 196 — leave_requests.pay_waived + the audit trigger that sees it.

`pre196_conn` rebuilds the pre-196 state on the cloned DB (which carries 196
already once `init_db` has run on this branch), so every test exercises the raw
forward and rollback SQL — same shape as `pre178_conn`.
"""
import json
import os
import sqlite3

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIG = os.path.join(REPO, "data", "migrations", "196_leave_pay_waiver.sql")
ROLLBACK = os.path.join(REPO, "data", "migrations",
                        "196_leave_pay_waiver.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _has_col(conn):
    return any(r[1] == "pay_waived"
               for r in conn.execute("PRAGMA table_info(leave_requests)"))


def _master(conn):
    return {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = 'leave_requests' AND sql IS NOT NULL")
    }


def _any_leave_id(conn):
    row = conn.execute("SELECT id FROM leave_requests LIMIT 1").fetchone()
    if row:
        return row[0]
    emp = conn.execute("SELECT id FROM employees LIMIT 1").fetchone()[0]
    lt = conn.execute("SELECT id FROM leave_types LIMIT 1").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO leave_requests (employee_id, leave_type_id, start_date,"
        " end_date, days, status) VALUES (?, ?, '2026-03-02', '2026-03-02', 1,"
        " 'approved')", (emp, lt))
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def pre196_conn(tmp_db_conn):
    if _has_col(tmp_db_conn):
        _apply(tmp_db_conn, ROLLBACK)
    assert not _has_col(tmp_db_conn)
    return tmp_db_conn


def test_forward_adds_zero_default_and_check(pre196_conn):
    c = pre196_conn
    rid = _any_leave_id(c)
    _apply(c, MIG)
    assert c.execute("SELECT COUNT(*) FROM leave_requests WHERE pay_waived <> 0"
                     ).fetchone()[0] == 0, "deploying must change no money"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE leave_requests SET pay_waived = 2 WHERE id = ?", (rid,))
    c.rollback()
    assert c.execute(
        "SELECT 1 FROM applied_migrations WHERE filename='196_leave_pay_waiver.sql'"
    ).fetchone() is not None


def test_audit_trigger_records_pay_waived(pre196_conn):
    c = pre196_conn
    rid = _any_leave_id(c)
    _apply(c, MIG)
    before = c.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0
    c.execute("UPDATE leave_requests SET pay_waived = 1 WHERE id = ?", (rid,))
    c.commit()
    rows = c.execute(
        "SELECT changed_fields FROM audit_log WHERE id > ? AND table_name = "
        "'leave_requests' AND row_id = ?", (before, rid)).fetchall()
    assert [json.loads(r[0]) for r in rows] == [{"pay_waived": [0, 1]}]


MIG_073 = os.path.join(REPO, "data", "migrations", "073_audit_hr_trigger_gaps.sql")


def _trigger_073():
    """The leave_requests UPDATE trigger exactly as mig 073 wrote it — an
    oracle independent of 196's rollback (the fixture already ran that)."""
    with open(MIG_073, encoding="utf-8") as f:
        src = f.read()
    start = src.index("CREATE TRIGGER audit_leave_requests_update")
    return src[start:src.index("END;", start) + len("END")]


def test_rollback_restores_the_073_trigger_byte_identical(pre196_conn):
    c = pre196_conn
    _apply(c, MIG)
    _apply(c, ROLLBACK)
    got = c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='audit_leave_requests_update'").fetchone()[0]
    assert got == _trigger_073()
    assert "pay_waived" not in got


def test_rollback_restores_schema_byte_identical_and_reruns(pre196_conn):
    c = pre196_conn
    before = _master(c)
    assert ("trigger", "audit_leave_requests_update") in before
    _apply(c, MIG)
    assert "pay_waived" in _master(c)[("trigger", "audit_leave_requests_update")]
    _apply(c, ROLLBACK)
    assert _master(c) == before
    assert c.execute(
        "SELECT 1 FROM applied_migrations WHERE filename='196_leave_pay_waiver.sql'"
    ).fetchone() is None
    # up → down → up again is clean.
    _apply(c, MIG)
    assert _has_col(c)
