# HR Sync No-Clobber Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `import_cashbook.sync_salary_sheet()` from silently modifying any field on an existing employee; surface sheet-vs-DB mismatches as opt-in DIFF warnings (with `bank_account_no` masked).

**Architecture:** Single-function behavioural change in `inventory_app/import_cashbook.py`. The existing-employee branch (`if emp_row is not None:`, lines 282-316) loses its 3 "fill if NULL" UPDATE statements and gains a diff-only warning loop. New-employee creation and downstream `salary_advances`/`_build_nickname_map` plumbing are untouched. Seven pytest cases lock the new behaviour and one knock-on consequence (advances unmatched for NULL-nickname employees).

**Tech Stack:** Python 3.9, Flask 3.x, SQLite, openpyxl (for sheet fixtures), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-21-hr-sync-no-clobber-design.md` (committed at `747c66c`).

---

## File Structure

- **Modify:** `inventory_app/import_cashbook.py`
  - Docstring of `sync_salary_sheet` (lines 236-242) — update `updated` key description.
  - Existing-employee branch (lines 282-316) — replace fill-if-NULL writes with diff-only warning loop.
- **Create:** `tests/test_hr_sync_no_clobber.py` — 7 pytest cases.

No other files change. No schema migration. No template change. No new routes.

---

## Task 1: Create branch

**Files:** none changed in this task — branch setup only.

- [ ] **Step 1: Confirm clean working tree on main**

Run from `/Users/putty/Sendai-Boonsawat/sendy_erp/`:

```bash
git status --short && git log --oneline -3
```

Expected: empty working-tree, HEAD at `747c66c` (or later — main may have advanced).

- [ ] **Step 2: Create feature branch off main**

```bash
git fetch origin && git checkout -b fix/hr-sync-no-clobber origin/main
```

Expected: `Switched to a new branch 'fix/hr-sync-no-clobber'`.

(Per project memory `feedback_fetch_before_operate`: always `git fetch` before branch-off, in case another tab landed work on `origin/main`.)

---

## Task 2: Add failing test — guards the motivating กิติยา case

**Files:**
- Create: `sendy_erp/tests/test_hr_sync_no_clobber.py`

- [ ] **Step 1: Create the test file with the kิติยา-case test only**

Create `sendy_erp/tests/test_hr_sync_no_clobber.py`:

```python
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


def _seed_employee(conn, *, full_name, nickname=None, bank_name=None,
                   bank_account_no=None, emp_code="EMP001"):
    """Insert one row into employees with the schema columns the sync touches."""
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
```

- [ ] **Step 2: Run the test to confirm it fails (RED)**

Run from `sendy_erp/`:

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_existing_employee_with_null_nickname_not_refilled -v
```

Expected: **FAIL.** Today's code at `import_cashbook.py:289` runs `if nickname and not emp_row[2]: UPDATE employees SET nickname=?` → DB nickname becomes `'กี'` (not NULL). The assertion `db_nickname is None` fails.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: add failing test — กิติยา NULL nickname must not be refilled (RED)"
```

---

## Task 3: Implement the diff-only existing-employee branch

**Files:**
- Modify: `inventory_app/import_cashbook.py:282-316`

- [ ] **Step 1: Replace the fill-if-NULL block with the diff-only loop**

Edit `inventory_app/import_cashbook.py`. Replace the entire existing-employee branch (line 282 to `continue` at line 316) with:

```python
        if emp_row is not None:
            # ── Existing employee — NO-CLOBBER RULE ──────────────────────────
            # Diff the 3 previously-fillable fields and surface mismatches as
            # warnings. Never UPDATE. bank_account_no is masked (PII): emit
            # field name only, never raw values from either side.
            emp_code = emp_row[1]
            for idx, field, sheet_val, sensitive in (
                (2, "nickname",        nickname,  False),
                (3, "bank_name",       bank,      False),
                (4, "bank_account_no", bank_acct, True),
            ):
                db_val = emp_row[idx]
                # Treat None and "" as equivalent (both = blank).
                sv = sheet_val or None
                dv = db_val or None
                if sv != dv:
                    if sensitive:
                        result["warnings"].append(
                            f"DIFF {emp_code} {field}: sheet differs from DB "
                            f"(skipped — edit in HR UI)"
                        )
                    else:
                        result["warnings"].append(
                            f"DIFF {emp_code} {field}: sheet={sheet_val!r} "
                            f"db={db_val!r} (skipped — edit in HR UI to change)"
                        )
            result["skipped"].append(emp_code)
            continue
```

The two surrounding branches stay exactly as-is:
- The `emp_row` lookup (lines 267-280) is unchanged.
- The new-employee branch (lines 318+ in the original file) is unchanged.

- [ ] **Step 2: Run the kิติยา test to confirm GREEN**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_existing_employee_with_null_nickname_not_refilled -v
```

Expected: **PASS.** DB nickname stays NULL; one DIFF warning emitted; `EMP001` in `skipped`.

- [ ] **Step 3: Commit the implementation**

```bash
git add inventory_app/import_cashbook.py
git commit -m "fix(hr-sync): existing employees skipped — no fill-if-NULL writes (GREEN)"
```

- [ ] **Step 4: Early check — confirm pre-existing cashbook tests behave**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_cashbook_import.py -v
```

Pre-existing tests `test_emp001_nickname_filled` and `test_advance_ball_matched` may break here depending on the state of EMP001 in your local DB clone (it currently has `nickname='บอล'` per the live DB, so they likely pass vacuously — but a fresh clone or future change could expose them). If they fail, **jump to Task 11 to update them, then return**. If they pass, continue with Task 4.

---

## Task 4: Add test #1 — non-NULL nickname diff also warns

**Files:**
- Modify: `tests/test_hr_sync_no_clobber.py` (append)

- [ ] **Step 1: Append the test**

Append to `tests/test_hr_sync_no_clobber.py`:

```python
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
```

- [ ] **Step 2: Run the new test**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_existing_employee_with_non_null_nickname_not_modified -v
```

Expected: **PASS** (the diff-only loop already handles this case).

- [ ] **Step 3: Commit**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: lock non-NULL nickname diff emits warning without modifying DB"
```

---

## Task 5: Add test #3 — silent skip when sheet matches DB exactly

**Files:**
- Modify: `tests/test_hr_sync_no_clobber.py` (append)

- [ ] **Step 1: Append the test**

```python
# ── Test 3 — sheet matches DB → no warnings, silent skip ─────────────────────

def test_existing_employee_matching_sheet_silent_skip(empty_db_conn):
    """
    Seed all 3 lockable fields identically to the sheet row. sync must emit
    zero DIFF warnings and the emp_code must land in 'skipped'.
    """
    conn = empty_db_conn
    _seed_employee(conn, full_name="สมศักดิ์ ทดสอบ",
                   nickname="ศักดิ์", bank_name="กสิกร",
                   bank_account_no="1234567890", emp_code="EMP003")

    parsed = _parsed_salary([{
        "first_name": "สมศักดิ์", "last_name": "ทดสอบ",
        "nickname":   "ศักดิ์",
        "bank":       "กสิกร",
        "bank_account_no": "1234567890",
        "salary": 15000.0, "sso_deduction": 750.0, "is_active": True,
    }])

    result = ic.sync_salary_sheet(parsed, conn)

    diff_warnings = [w for w in result["warnings"] if "DIFF" in w]
    assert diff_warnings == [], \
        f"Expected zero DIFF warnings, got: {diff_warnings!r}"
    assert "EMP003" in result["skipped"]
```

- [ ] **Step 2: Run the test**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_existing_employee_matching_sheet_silent_skip -v
```

Expected: **PASS.**

- [ ] **Step 3: Commit**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: matching sheet→DB values produce no DIFF warnings"
```

---

## Task 6: Add test #6 — bank_account_no PII masking

**Files:**
- Modify: `tests/test_hr_sync_no_clobber.py` (append)

- [ ] **Step 1: Append the test**

```python
# ── Test 6 — bank_account_no values must NEVER appear in warning text ────────

def test_bank_account_no_diff_does_not_leak_raw_values(empty_db_conn):
    """
    Seed employee with bank_account_no='1234567890' AND matching nickname +
    bank_name (so they emit no warning). Sheet has a different account number
    ('9999999999'). The only DIFF warning must be the bank_account_no one,
    and neither '1234567890' nor '9999999999' may appear in any warning.
    """
    conn = empty_db_conn
    _seed_employee(conn, full_name="สมหญิง ทดสอบ",
                   nickname="หญิง", bank_name="ไทยพาณิชย์",
                   bank_account_no="1234567890", emp_code="EMP004")

    parsed = _parsed_salary([{
        "first_name": "สมหญิง", "last_name": "ทดสอบ",
        "nickname":   "หญิง",                       # matches DB
        "bank":       "ไทยพาณิชย์",                 # matches DB
        "bank_account_no": "9999999999",            # diverges
        "salary": 14000.0, "sso_deduction": 700.0, "is_active": True,
    }])

    result = ic.sync_salary_sheet(parsed, conn)

    diff_warnings = [w for w in result["warnings"] if "DIFF" in w]
    assert len(diff_warnings) == 1, \
        f"Expected exactly 1 DIFF warning, got: {diff_warnings!r}"
    msg = diff_warnings[0]
    assert "bank_account_no" in msg
    assert "EMP004" in msg

    # PII regression — neither account number may appear in any warning text.
    for w in result["warnings"]:
        assert "1234567890" not in w, f"DB account leaked into warning: {w!r}"
        assert "9999999999" not in w, f"Sheet account leaked into warning: {w!r}"
```

- [ ] **Step 2: Run the test**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_bank_account_no_diff_does_not_leak_raw_values -v
```

Expected: **PASS.** The `sensitive=True` branch in `sync_salary_sheet` already emits the masked form.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: bank_account_no values masked in DIFF warnings (PII)"
```

---

## Task 7: Add test #4 — new-employee creation regression guard

**Files:**
- Modify: `tests/test_hr_sync_no_clobber.py` (append)

- [ ] **Step 1: Append the test**

```python
# ── Test 4 — new employee still auto-created from sheet ──────────────────────

def test_new_employee_still_auto_created(empty_db_conn):
    """
    Sheet has a person not in DB. The new-employee branch must still run:
    employees row inserted with auto EMP-code, employee_salary_history seed
    row inserted, result['created'] populated, "start_date unknown" warning
    emitted.
    """
    conn = empty_db_conn

    parsed = _parsed_salary([{
        "first_name": "ใหม่", "last_name": "มาเอง",
        "nickname":   "ใหม่",
        "bank":       "กสิกร",
        "bank_account_no": "5551112220",
        "salary": 13000.0, "sso_deduction": 650.0, "is_active": True,
    }])

    result = ic.sync_salary_sheet(parsed, conn)

    row = conn.execute(
        "SELECT emp_code, nickname, bank_name, bank_account_no "
        "FROM employees WHERE full_name='ใหม่ มาเอง'"
    ).fetchone()
    assert row is not None, "New employee must be inserted"
    emp_code = row[0]
    assert emp_code.startswith("EMP")
    assert row[1] == "ใหม่"
    assert row[2] == "กสิกร"
    assert row[3] == "5551112220"

    assert emp_code in result["created"]
    assert emp_code not in result["skipped"]
    assert emp_code not in result["updated"]

    hist = conn.execute(
        "SELECT effective_date, reason FROM employee_salary_history "
        "WHERE employee_id=(SELECT id FROM employees WHERE emp_code=?)",
        (emp_code,),
    ).fetchone()
    assert hist is not None, "Salary history seed row missing for new employee"
    assert hist[1] == "initial"

    start_warnings = [w for w in result["warnings"] if "start_date" in w]
    assert len(start_warnings) >= 1, \
        "Expected 'start_date unknown' warning for new employee"
```

- [ ] **Step 2: Run the test**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_new_employee_still_auto_created -v
```

Expected: **PASS** — new-employee branch is unchanged by Task 3.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: new-employee auto-create still works (regression guard)"
```

---

## Task 8: Add test #5 — idempotent re-import after manual clear

**Files:**
- Modify: `tests/test_hr_sync_no_clobber.py` (append)

- [ ] **Step 1: Append the test**

```python
# ── Test 5 — end-to-end idempotence after Put clears a field in HR UI ────────

def test_re_import_idempotent_after_manual_clear(empty_db_conn):
    """
    Simulate Put's workflow:
      1. First import creates EMP with nickname filled.
      2. Put manually clears the nickname via HR UI (modelled here as a
         direct UPDATE … SET nickname=NULL).
      3. Second import on the same sheet row must NOT refill the nickname.
    """
    conn = empty_db_conn

    parsed = _parsed_salary([{
        "first_name": "ทดสอบ", "last_name": "อิดิ",
        "nickname":   "ทด",
        "bank":       None, "bank_account_no": None,
        "salary": 10000.0, "sso_deduction": 500.0, "is_active": True,
    }])

    # Pass 1 — creates the employee.
    result1 = ic.sync_salary_sheet(parsed, conn)
    emp_code = result1["created"][0]
    row = conn.execute(
        "SELECT nickname FROM employees WHERE emp_code=?", (emp_code,)
    ).fetchone()
    assert row[0] == "ทด", "Initial sync must populate nickname for new employee"

    # Simulate Put clearing via HR UI.
    conn.execute(
        "UPDATE employees SET nickname=NULL WHERE emp_code=?", (emp_code,)
    )
    conn.commit()

    # Pass 2 — must NOT refill.
    result2 = ic.sync_salary_sheet(parsed, conn)
    row = conn.execute(
        "SELECT nickname FROM employees WHERE emp_code=?", (emp_code,)
    ).fetchone()
    assert row[0] is None, \
        f"DB nickname must stay NULL on re-import, got {row[0]!r}"

    assert emp_code in result2["skipped"]
    assert emp_code not in result2["updated"]
    diffs = [w for w in result2["warnings"] if "DIFF" in w and "nickname" in w]
    assert len(diffs) == 1, \
        f"Expected exactly 1 nickname DIFF warning on pass 2, got: {diffs!r}"
```

- [ ] **Step 2: Run the test**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_re_import_idempotent_after_manual_clear -v
```

Expected: **PASS.**

- [ ] **Step 3: Commit**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: re-import idempotent after manual nickname clear (กิติยา flow)"
```

---

## Task 9: Add test #7 — salary-advance knock-on regression guard

**Files:**
- Modify: `tests/test_hr_sync_no_clobber.py` (append)

This test runs the **full** `import_cashbook()` flow (not just `sync_salary_sheet`), because the advance-matching logic lives in `import_cashbook()` itself (lines 537-571). A minimal synthetic workbook is built inline.

- [ ] **Step 1: Append the workbook helper and the test**

```python
# ── Test 7 — advance unmatched when existing employee has NULL nickname ──────
import datetime as _dt
import openpyxl


def _build_minimal_advance_test_wb(path, *, full_first, full_last,
                                    sheet_nickname, advance_raw_name,
                                    advance_amount=500.0):
    """
    Build the smallest workbook import_cashbook can process: 1 account with 1
    transaction, Salary_Sheet with 1 employee, 1 advance row, minimal Setup +
    Overview.
    """
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_ov = wb.create_sheet("Overview")
    ws_ov.cell(row=3, column=2, value="รายรับ");  ws_ov.cell(row=3, column=3, value=1000.0)
    ws_ov.cell(row=4, column=2, value="รายจ่าย"); ws_ov.cell(row=4, column=3, value=500.0)
    ws_ov.cell(row=5, column=2, value="คงเหลือ"); ws_ov.cell(row=5, column=3, value=500.0)

    ws = wb.create_sheet("Txn_MAIN")
    for ci, h in enumerate(
        ["วันที่", "ประเภท", "หมวดหมู่", "หมวดหมู่_ผู้ใช้",
         "จำนวนเงิน", "รายละเอียด", "หมายเหตุ"], 1):
        ws.cell(row=1, column=ci, value=h)
    ws.cell(row=2, column=9, value="Bank");           ws.cell(row=2, column=10, value="KBank")
    ws.cell(row=3, column=9, value="Account Number"); ws.cell(row=3, column=10, value="0000000000")
    ws.cell(row=4, column=9, value="Name");           ws.cell(row=4, column=10, value="เจ้าของ")
    ws.cell(row=2, column=1, value=_dt.datetime(2026, 4, 1))
    ws.cell(row=2, column=2, value="รายรับ")
    ws.cell(row=2, column=3, value="เงินฝาก")
    ws.cell(row=2, column=5, value=1000.0)
    ws.cell(row=3, column=1, value=_dt.datetime(2026, 4, 2))
    ws.cell(row=3, column=2, value="รายจ่าย")
    ws.cell(row=3, column=3, value="เงินเดือน")
    ws.cell(row=3, column=5, value=500.0)

    ws_sal = wb.create_sheet("Salary_Sheet")
    ws_sal.cell(row=1, column=1, value="des")
    for ci, h in enumerate(
        ["", "ชื่อ", "นามสกุล", "ชื่อเล่น", "ธนาคาร", "เลขบัญชี",
         "เงินเดือน", "หักประกันสังคม", "เงินเดือนสุทธิ", "is_active"], 1):
        ws_sal.cell(row=2, column=ci, value=h)
    ws_sal.cell(row=3, column=2, value=full_first)
    ws_sal.cell(row=3, column=3, value=full_last)
    ws_sal.cell(row=3, column=4, value=sheet_nickname)
    ws_sal.cell(row=3, column=5, value=None)
    ws_sal.cell(row=3, column=6, value=None)
    ws_sal.cell(row=3, column=7, value=10000.0)
    ws_sal.cell(row=3, column=8, value=500.0)
    ws_sal.cell(row=3, column=9, value=9500.0)
    ws_sal.cell(row=3, column=10, value=True)

    ws_adv = wb.create_sheet("เบิกเงินล่วงหน้า")
    ws_adv.cell(row=2, column=2, value="วันที่")
    ws_adv.cell(row=2, column=3, value="ชื่อ")
    ws_adv.cell(row=2, column=4, value="เบิกเงินล่วงหน้า")
    ws_adv.cell(row=2, column=5, value="หมายเหตุ")
    ws_adv.cell(row=3, column=2, value=_dt.datetime(2026, 4, 10))
    ws_adv.cell(row=3, column=3, value=advance_raw_name)
    ws_adv.cell(row=3, column=4, value=advance_amount)
    ws_adv.cell(row=3, column=5, value=None)

    ws_setup = wb.create_sheet("Setup")
    ws_setup.cell(row=2, column=2, value="รายรับ")
    ws_setup.cell(row=2, column=3, value="รายจ่าย")
    ws_setup.cell(row=3, column=2, value="เงินฝาก")
    ws_setup.cell(row=3, column=3, value="เงินเดือน")

    wb.save(path)


def test_advance_unmatched_when_existing_nickname_null(empty_db_conn, tmp_path):
    """
    Existing employee has full_name='ทดสอบ อิดิ' with nickname=NULL.
    Sheet brings nickname='ทด' AND a salary advance with raw_name='ทด'.

    Expected after import:
      - employee row UNCHANGED (DB nickname still NULL)
      - advance row inserted, but employee_id=NULL (raw_name preserved)
      - DIFF warning for the nickname mismatch emitted
    """
    conn = empty_db_conn
    # Seed employee with NULL nickname.
    _seed_employee(conn, full_name="ทดสอบ อิดิ", nickname=None,
                   emp_code="EMP010")

    wb_path = str(tmp_path / "advance_unmatched.xlsx")
    _build_minimal_advance_test_wb(
        wb_path,
        full_first="ทดสอบ", full_last="อิดิ",
        sheet_nickname="ทด",
        advance_raw_name="ทด",
    )

    ic.import_cashbook(wb_path, conn=conn)

    # 1. Employee row unchanged.
    db_nickname = conn.execute(
        "SELECT nickname FROM employees WHERE emp_code='EMP010'"
    ).fetchone()[0]
    assert db_nickname is None, \
        f"DB nickname must remain NULL after import, got {db_nickname!r}"

    # 2. Advance row inserted, employee_id NULL, raw_name preserved.
    adv_rows = conn.execute(
        "SELECT employee_id, raw_name FROM salary_advances WHERE raw_name='ทด'"
    ).fetchall()
    assert len(adv_rows) == 1, \
        f"Expected exactly 1 advance row with raw_name='ทด', got {adv_rows!r}"
    assert adv_rows[0][0] is None, \
        "Advance must be unmatched (employee_id=NULL) until nickname is set"
    assert adv_rows[0][1] == "ทด"
```

- [ ] **Step 2: Run the test**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py::test_advance_unmatched_when_existing_nickname_null -v
```

Expected: **PASS.** With the new no-clobber rule, sync doesn't fill `employee.nickname` → `_build_nickname_map` doesn't see `ทด` → the advance lookup at `import_cashbook.py:549` returns None → row inserts with `employee_id=NULL`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hr_sync_no_clobber.py
git commit -m "test: salary advance unmatched when existing nickname NULL (knock-on)"
```

---

## Task 10: Update `sync_salary_sheet` docstring

**Files:**
- Modify: `inventory_app/import_cashbook.py:236-242`

- [ ] **Step 1: Replace the stale docstring lines**

In `inventory_app/import_cashbook.py`, find the docstring inside `sync_salary_sheet` (around lines 236-242). The current "Returns" block reads:

```python
    Returns
    -------
    dict with keys:
      created        — list of emp_codes created
      updated        — list of emp_codes updated (nickname / bank filled)
      skipped        — list of full_names skipped (no change needed)
      warnings       — list of warning strings
```

Replace with:

```python
    Returns
    -------
    dict with keys:
      created        — list of emp_codes created
      updated        — list of emp_codes updated (always empty under the
                       no-clobber rule for existing employees; reserved for
                       future use, dict-shape preserved for caller stability)
      skipped        — list of emp_codes whose existing-employee match was
                       not modified; sheet/DB mismatches are surfaced via
                       'warnings' as DIFF lines (bank_account_no masked)
      warnings       — list of warning strings (start_date unknown for new
                       employees, DIFF lines for existing-employee mismatches)
```

- [ ] **Step 2: Also update the module-level "HR sync rules" comment**

Higher up in the file (around lines 31-43), the module docstring describes the OLD HR sync rule. Replace:

```python
HR sync rules (sync_salary_sheet)
----------------------------------
- Match existing employees by full_name (first+' '+last) or nickname.
- Existing employees: fill nickname if NULL/blank; optionally fill bank_*
  if NULL.  Do NOT touch salary, salary_history, diligence, probation,
  start_date, sso_enrolled, company_id, is_active for existing employees.
- New employees: create with next EMP-code, company_id=1 (BSN assumption,
  documented), diligence_allowance=0, sso_enrolled=(sso_deduction>0),
  is_active from sheet, start_date=NULL, bank from sheet.
  Add ONE employee_salary_history row (effective_date=2026-01-01,
  reason='imported_salary_sheet').
- Idempotent: match-first, no duplicates on re-run.
- Emits a WARNING for each new employee that start_date is unknown.
```

With:

```python
HR sync rules (sync_salary_sheet)
----------------------------------
- Match existing employees by full_name (first+' '+last) or nickname.
- Existing employees: NO-CLOBBER. Never UPDATE any field. Sheet/DB diffs
  are surfaced as 'DIFF <emp_code> <field>: ...' entries in result.warnings
  (bank_account_no values are masked — field name + 'differs' marker only).
- New employees: create with next EMP-code, company_id=1 (BSN assumption,
  documented), diligence_allowance=0, sso_enrolled=(sso_deduction>0),
  is_active from sheet, start_date=NULL, bank from sheet.
  Add ONE employee_salary_history row (effective_date=2026-01-01,
  reason='imported_salary_sheet').
- Idempotent: match-first, no duplicates on re-run.
- Emits a WARNING for each new employee that start_date is unknown.
- Knock-on: an advance whose raw_name would match a freshly-filled nickname
  is now inserted with employee_id=NULL until Put fills the nickname in
  HR UI (see test_advance_unmatched_when_existing_nickname_null).
```

- [ ] **Step 3: Commit the docs update**

```bash
git add inventory_app/import_cashbook.py
git commit -m "docs(hr-sync): update docstring to reflect no-clobber rule"
```

---

## Task 11: Update the now-vacuous pre-existing cashbook test

**Files:**
- Modify: `tests/test_cashbook_import.py:683-693` (`test_emp001_nickname_filled`)

Background: that test's docstring claims "EMP001 had no nickname — salary sheet has 'บอล' → nickname must be set." Under the old fill-if-NULL rule, this proved the import wrote the nickname. Under no-clobber it no longer writes; the test would either (a) pass vacuously because the cloned live DB already has `EMP001.nickname='บอล'`, or (b) fail in any environment where the clone has EMP001's nickname NULL. Rewrite to test the new semantics explicitly.

- [ ] **Step 1: Rewrite `test_emp001_nickname_filled` to assert no-clobber**

Find this block in `tests/test_cashbook_import.py` (around lines 683-693):

```python
    def test_emp001_nickname_filled(self, synth_wb, tmp_db, tmp_db_conn):
        """EMP001 had no nickname — salary sheet has 'บอล' → nickname must be set."""
        _ensure_mig056(tmp_db_conn)
        mod = _import_cashbook()
        mod.import_cashbook(synth_wb, conn=tmp_db_conn)
        tmp_db_conn.commit()
        row = tmp_db_conn.execute(
            "SELECT nickname FROM employees WHERE emp_code='EMP001'"
        ).fetchone()
        assert row is not None
        assert row[0] == "บอล", f"EMP001 nickname should be 'บอล', got {row[0]!r}"
```

Replace with:

```python
    def test_emp001_nickname_preserved_under_no_clobber(self, synth_wb, tmp_db, tmp_db_conn):
        """
        No-clobber rule: whatever nickname EMP001 has in the DB before import,
        the import must not change it. Pre-seed NULL → stays NULL; pre-seed a
        custom value → stays as custom value. The sheet's 'บอล' is never written
        to an existing employee.
        """
        _ensure_mig056(tmp_db_conn)

        # Pre-seed EMP001's nickname to NULL to make the test deterministic
        # (the cloned live DB may have any value; reset it for this assertion).
        tmp_db_conn.execute(
            "UPDATE employees SET nickname=NULL WHERE emp_code='EMP001'"
        )
        tmp_db_conn.commit()

        mod = _import_cashbook()
        mod.import_cashbook(synth_wb, conn=tmp_db_conn)
        tmp_db_conn.commit()

        row = tmp_db_conn.execute(
            "SELECT nickname FROM employees WHERE emp_code='EMP001'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, (
            f"No-clobber: EMP001 nickname must remain NULL after import, got {row[0]!r}"
        )
```

- [ ] **Step 2: Adjust `test_advance_ball_matched` to be deterministic**

The advance-matched test (lines 558-575) currently relies on `EMP001.nickname` being populated *somehow* in tmp_db at the moment `_build_nickname_map` runs — which used to happen via sync's fill-if-NULL. Under no-clobber, sync no longer fills. Make the precondition explicit by pre-seeding EMP001's nickname before import:

Find:

```python
    def test_advance_ball_matched(self, synth_wb, tmp_db, tmp_db_conn):
        """'บอล' nickname → matched to EMP001 (employee_id set)."""
        _ensure_mig056(tmp_db_conn)
        mod = _import_cashbook()
        mod.import_cashbook(synth_wb, conn=tmp_db_conn)
        tmp_db_conn.commit()
```

Replace with:

```python
    def test_advance_ball_matched(self, synth_wb, tmp_db, tmp_db_conn):
        """'บอล' nickname → matched to EMP001 (employee_id set).

        Under no-clobber the import will not fill EMP001's nickname from the
        sheet. Pre-seed it here so the advance lookup has something to match.
        This is the same operator step Put performs in HR UI in production.
        """
        _ensure_mig056(tmp_db_conn)
        tmp_db_conn.execute(
            "UPDATE employees SET nickname='บอล' WHERE emp_code='EMP001'"
        )
        tmp_db_conn.commit()
        mod = _import_cashbook()
        mod.import_cashbook(synth_wb, conn=tmp_db_conn)
        tmp_db_conn.commit()
```

- [ ] **Step 3: Check the real-file integration test (REAL_FILE block, around lines 880-920)**

This test only runs when `/Users/putty/Downloads/NoVat_Account.xlsx` exists. It asserts `EMP001.nickname == 'บอล'` after import (lines 888-893). If your local DB clone already has `EMP001.nickname='บอล'`, the assertion is vacuously true and the test passes unchanged. If not, the test will fail.

Reproducible fix — pre-seed the same way (at the top of the test body, before the `mod.import_cashbook(REAL_FILE, ...)` call):

```python
        tmp_db_conn.execute(
            "UPDATE employees SET nickname='บอล' WHERE emp_code='EMP001'"
        )
        tmp_db_conn.commit()
```

Also update the assertion's intent comment from "EMP001 nickname set to บอล" to "EMP001 nickname preserved (no-clobber)".

- [ ] **Step 4: Run the updated cashbook regression suite**

```bash
cd /Users/putty/Sendai-Boonsawat/sendy_erp
~/.virtualenvs/erp/bin/pytest tests/test_cashbook_import.py -v
```

Expected: all tests pass (the real-file test still skips if `/Users/putty/Downloads/NoVat_Account.xlsx` is absent — that's normal).

- [ ] **Step 5: Commit the cashbook test updates**

```bash
git add tests/test_cashbook_import.py
git commit -m "test(cashbook): align EMP001 tests with no-clobber rule (pre-seed nicknames)"
```

---

## Task 12: Run the new test file + the full pytest suite

**Files:** none — verification only.

- [ ] **Step 1: Run the no-clobber test file end-to-end**

```bash
cd /Users/putty/Sendai-Boonsawat/sendy_erp
~/.virtualenvs/erp/bin/pytest tests/test_hr_sync_no_clobber.py -v
```

Expected: 7 tests pass, 0 fail, 0 skip (unless `LIVE_DB` is missing — then `empty_db_conn` will skip; ensure the live DB is present at `inventory_app/instance/inventory.db`).

- [ ] **Step 2: Run the full pytest suite**

```bash
~/.virtualenvs/erp/bin/pytest -x -ra
```

Expected: full suite passes. If anything outside the cashbook/HR area fails, investigate — the no-clobber change should not touch any other code path. If a failure looks legitimately related, fix the root cause (don't paper over by weakening the assertion); if it's a flaky test unrelated to this change, note it in the PR description but proceed.

---

## Task 13: Manual smoke test against the dev server

**Files:** none — runtime verification only.

- [ ] **Step 1: Restart the dev server**

```bash
sendy-down 2>/dev/null; sendy-up
sleep 2 && tail -n 20 /tmp/sendy.log
```

Expected: server starts on `:5001` without error.

- [ ] **Step 2: Re-import the last NoVat workbook via the existing UI**

In a browser, open `http://127.0.0.1:5001/cashbook/import`, upload the most recent NoVat workbook (e.g., from `Document/Boonsawat/` — pick whichever file Put last imported).

- [ ] **Step 3: Verify the import result page**

The result page should now show DIFF warnings for any sheet-vs-DB mismatches on existing employees. Expected at minimum:
- A DIFF warning for `EMP00X nickname:` for the กิติยา case (or whichever employee currently has NULL nickname in DB but a value in the sheet).
- For `bank_account_no` mismatches (if any), the warning text must NOT contain any digit string from either the DB or the sheet — only "sheet differs from DB".

- [ ] **Step 4: Verify no employee row's `updated_at` advanced**

Open a SQLite shell:

```bash
~/.virtualenvs/erp/bin/python -c "
import sqlite3
c = sqlite3.connect('inventory_app/instance/inventory.db')
for row in c.execute('SELECT emp_code, nickname, updated_at FROM employees ORDER BY emp_code'):
    print(row)
"
```

Expected: every `updated_at` is older than the timestamp of the import you just ran. None of the existing employee rows were touched.

- [ ] **Step 5: Tear down dev server**

```bash
sendy-down
```

---

## Task 14: Push branch, open PR, run review gates

**Files:** none — workflow only.

- [ ] **Step 1: Push the branch**

```bash
git fetch origin
git push -u origin fix/hr-sync-no-clobber
```

- [ ] **Step 2: Open the PR via gh**

```bash
gh pr create --title "fix(hr-sync): no-clobber rule for existing employees on cashbook re-import" --body "$(cat <<'EOF'
## Summary

- Replace the "fill if NULL" UPDATEs in `sync_salary_sheet()` with a diff-only
  warning loop. Existing employees are never modified by cashbook re-imports.
- `bank_account_no` mismatches emit a masked warning ("sheet differs from DB")
  — no raw account numbers ever appear in `result["warnings"]`.
- Knock-on: salary advances whose raw_name matches a not-yet-set nickname
  insert with `employee_id=NULL` (no data loss; documented).

Spec: `docs/superpowers/specs/2026-05-21-hr-sync-no-clobber-design.md`
Plan: `docs/superpowers/plans/2026-05-21-hr-sync-no-clobber.md`

## Test plan

- [ ] `pytest tests/test_hr_sync_no_clobber.py -v` → 7 pass
- [ ] `pytest tests/test_cashbook_import.py -v` → no regressions
- [ ] `pytest -x -ra` → full suite green
- [ ] Manual: re-import last NoVat workbook on dev server; DIFF warnings
      present, no employee `updated_at` advances, no account-number digits
      in any warning text.

## Post-merge operator step (one-time)

For each employee whose nickname is NULL in DB but appears in the salary
sheet: populate the nickname via HR UI. This silences the DIFF warning AND
restores the salary-advance auto-match. Affects ~1-2 employees in current
data (e.g., กิติยา).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Trigger Codex adversarial review (optional but matches recent PR pattern)**

```bash
node "/Users/putty/.claude/plugins/cache/openai-codex/codex/1.0.4/scripts/codex-companion.mjs" review ""
```

Apply any blocking findings before merge.

- [ ] **Step 4: Squash-merge once gates pass**

After approvals + green CI:

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull origin main
```

Railway will auto-deploy. Then run the post-deploy verification described in the spec (Rollout step 5).

---

## Definition of done

- 7 new tests in `tests/test_hr_sync_no_clobber.py`, all passing.
- `import_cashbook.py:282-316` rewritten as a diff-only loop; module + function docstrings updated.
- Full `pytest -x -ra` green.
- PR merged to `main`; deployed to Railway.
- Post-deploy smoke: re-import last NoVat workbook surfaces DIFF warnings, no employee `updated_at` advances, no account numbers in warning text.
- Operator step done: nicknames populated in HR UI for any employee where DB was NULL and sheet has a value.
