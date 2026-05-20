"""
tests/test_hr_sync_no_clobber.py
TDD tests for the HR-sync no-clobber rule in inventory_app/import_cashbook.py.

The rule: sync_salary_sheet() must never modify any field on an EXISTING
employee. New-employee creation is unaffected. Sheet/DB mismatches are
surfaced as DIFF warnings; bank_account_no values are masked (PII).

See docs/superpowers/specs/2026-05-21-hr-sync-no-clobber-design.md for
the design.
"""
import sqlite3

import pytest

import import_cashbook as ic


def _seed_companies(conn):
    """Idempotently seed BSN (id=1) + SD (id=2) so employees.company_id FK satisfied."""
    conn.execute(
        "INSERT OR IGNORE INTO companies (id, code, name_th) VALUES (1, 'BSN', 'บุญสวัสดิ์ นำชัย')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO companies (id, code, name_th) VALUES (2, 'SD', 'เซ็นไดเทรดดิ้ง')"
    )
    conn.commit()


def _seed_employee(conn, *, full_name, nickname=None, bank_name=None,
                   bank_account_no=None, emp_code="EMP001"):
    """Insert one row into employees with the schema columns the sync touches."""
    _seed_companies(conn)
    conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, nickname, bank_name, bank_account_no,
              company_id, sso_enrolled, diligence_allowance, is_active,
              start_date, probation_days)
           VALUES (?, ?, ?, ?, ?, 1, 0, 0, 1, NULL, 90)""",
        (emp_code, full_name, nickname, bank_name, bank_account_no),
    )
    conn.commit()


def _parsed_salary(rows):
    """Build the minimum `parsed` dict shape sync_salary_sheet consumes."""
    return {"salary": rows, "advances": [], "transactions": [],
            "categories": {"income": [], "expense": []}, "overview": {}}


# ── Test 2 (กิติยา case) — first failing test ────────────────────────────────

def test_existing_employee_with_null_nickname_not_refilled(empty_db_conn):
    """
    Seed an employee with nickname=NULL (Put manually cleared it). The salary
    sheet has a nickname for the same person. After sync, the DB must remain
    NULL — and a DIFF warning must surface the mismatch.
    """
    conn = empty_db_conn
    _seed_employee(conn, full_name="กิติยา ทดสอบ", nickname=None,
                   emp_code="EMP001")

    parsed = _parsed_salary([{
        "first_name": "กิติยา",
        "last_name":  "ทดสอบ",
        "nickname":   "กี",
        "bank":       None,
        "bank_account_no": None,
        "salary":     12000.0,
        "sso_deduction": 600.0,
        "is_active":  True,
    }])

    result = ic.sync_salary_sheet(parsed, conn)

    db_nickname = conn.execute(
        "SELECT nickname FROM employees WHERE emp_code='EMP001'"
    ).fetchone()[0]
    assert db_nickname is None, \
        f"DB nickname must remain NULL, got {db_nickname!r}"

    assert "EMP001" in result["skipped"]
    assert "EMP001" not in result["updated"]

    diff_warnings = [w for w in result["warnings"] if "DIFF" in w]
    assert len(diff_warnings) >= 1, \
        f"Expected at least one DIFF warning, got: {result['warnings']!r}"
    msg = diff_warnings[0]
    assert "EMP001" in msg
    assert "nickname" in msg


# ── Test 1 — non-NULL custom value (วิภา=หลุย case) ──────────────────────────

def test_existing_employee_with_non_null_nickname_not_modified(empty_db_conn):
    """
    Seed nickname='หลุย' (Put's custom value, different from sheet's 'วิภา').
    DB must remain 'หลุย'; a DIFF warning must surface both values.
    """
    conn = empty_db_conn
    _seed_employee(conn, full_name="วิภา ทดสอบ", nickname="หลุย",
                   emp_code="EMP002")

    parsed = _parsed_salary([{
        "first_name": "วิภา", "last_name": "ทดสอบ",
        "nickname":   "วิภา",
        "bank":       None, "bank_account_no": None,
        "salary": 12000.0, "sso_deduction": 600.0, "is_active": True,
    }])

    result = ic.sync_salary_sheet(parsed, conn)

    db_nickname = conn.execute(
        "SELECT nickname FROM employees WHERE emp_code='EMP002'"
    ).fetchone()[0]
    assert db_nickname == "หลุย", \
        f"DB nickname must remain 'หลุย', got {db_nickname!r}"

    assert "EMP002" in result["skipped"]
    diffs = [w for w in result["warnings"] if "DIFF" in w]
    assert len(diffs) == 1, f"Expected exactly 1 DIFF warning, got: {diffs!r}"
    msg = diffs[0]
    assert "EMP002" in msg
    assert "nickname" in msg
    assert "หลุย" in msg
    assert "วิภา" in msg
