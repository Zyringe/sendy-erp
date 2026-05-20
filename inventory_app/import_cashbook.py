"""
import_cashbook.py — Cashbook Excel importer for Sendy ERP.

Entry points
------------
  import_cashbook(path, conn=None) -> dict
      Full import + HR sync.

  sync_salary_sheet(parsed, conn) -> dict
      HR employee upsert from parsed salary rows.
      Called automatically by import_cashbook; may be called stand-alone.

Behaviour overview
------------------
1. Parses workbook via parse_cashbook.parse_cashbook().
2. Applies migration 056 (is_transfer column) if not yet present, via
   database.run_pending_migrations().
3. Upserts cashbook_accounts by code (stable id).
4. Detects transfer/passthrough accounts heuristically:
     abs(Σincome − Σexpense) < 0.01  AND  Σincome > 0
   Sets is_transfer=1.  Clobber-guard: if existing is_transfer=1 and
   heuristic now returns False, leaves it at 1 and emits a warning.
5. Idempotent full-replace per account_id:
     DELETE FROM cashbook_transactions WHERE account_id=?
     then INSERT fresh rows.
6. Upserts cashbook_categories from Setup + transaction categories.
7. Full-replace salary_advances; matches raw_name to employees.nickname.
8. Calls sync_salary_sheet() for HR employee upsert.
9. Returns summary dict (counts + reconciliation + warnings).

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

Encoding notes
--------------
- This importer reads .xlsx files via parse_cashbook (openpyxl); no cp874
  concern.  DB is UTF-8.

company_id assumption
---------------------
- company_id=1 (BSN) is assumed for all new employees created from the
  salary sheet.  The salary sheet carries no company field.  Put must
  manually correct if a future employee belongs to SD (company_id=2).
"""

import os
import uuid
import datetime
from typing import Any, Dict, List, Optional

from parse_cashbook import parse_cashbook
import database


# ── Constants ─────────────────────────────────────────────────────────────────

_TRANSFER_TOLERANCE = 0.01          # |Σincome − Σexpense| < this → passthrough
_DEFAULT_COMPANY_ID = 1             # BSN — salary sheet has no company field
_SALARY_HISTORY_EFFECTIVE_DATE = "2026-01-01"


# ── Migration helper ──────────────────────────────────────────────────────────

def _ensure_migration_056(conn):
    """
    Ensure is_transfer column exists on cashbook_accounts.

    Calls run_pending_migrations so 056 is applied via the standard runner.
    Falls back to an inline ALTER if the migration file is somehow missing
    (e.g. test environment without migrations dir).
    """
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(cashbook_accounts)"
    ).fetchall()]
    if "is_transfer" not in cols:
        try:
            database.run_pending_migrations(conn, verbose=False)
        except Exception:
            pass
        # Re-check after migration attempt; apply inline if still missing
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(cashbook_accounts)"
        ).fetchall()]
        if "is_transfer" not in cols:
            conn.execute(
                "ALTER TABLE cashbook_accounts "
                "ADD COLUMN is_transfer INTEGER NOT NULL DEFAULT 0 "
                "CHECK(is_transfer IN (0,1))"
            )
            conn.commit()


# ── Account helpers ───────────────────────────────────────────────────────────

def _upsert_account(conn, code, bank_name, bank_account_no, account_owner_name, note):
    """
    Insert account if not present; update meta fields on existing row.
    Returns the account id.
    """
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code=?", (code,)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO cashbook_accounts
                 (code, bank_name, bank_account_no, account_owner_name, note)
               VALUES (?, ?, ?, ?, ?)""",
            (code, bank_name, bank_account_no, account_owner_name, note),
        )
        return cur.lastrowid
    else:
        conn.execute(
            """UPDATE cashbook_accounts
               SET bank_name=?, bank_account_no=?, account_owner_name=?,
                   note=?, updated_at=datetime('now','localtime')
               WHERE code=?""",
            (bank_name, bank_account_no, account_owner_name, note, code),
        )
        return row[0]


def _set_is_transfer(conn, account_id, heuristic_value, warnings):
    """
    Update is_transfer on an account, with clobber-guard.

    Clobber-guard: if existing is_transfer=1 and heuristic returns False,
    do NOT overwrite — leave at 1 and emit a warning.  This protects against
    a partial-month snapshot accidentally reversing a known classification.
    """
    row = conn.execute(
        "SELECT is_transfer FROM cashbook_accounts WHERE id=?", (account_id,)
    ).fetchone()
    current = row[0] if row else 0

    if current == 1 and not heuristic_value:
        # Clobber-guard: keep existing 1, warn
        code_row = conn.execute(
            "SELECT code FROM cashbook_accounts WHERE id=?", (account_id,)
        ).fetchone()
        code = code_row[0] if code_row else str(account_id)
        warnings.append(
            f"is_transfer clobber-guard: account '{code}' (id={account_id}) already "
            f"is_transfer=1 but the current file's heuristic returned False "
            f"(income ≠ expense in this snapshot). Leaving is_transfer=1 unchanged. "
            f"Override manually via SQL if this is wrong."
        )
        return

    new_val = 1 if heuristic_value else 0
    conn.execute(
        "UPDATE cashbook_accounts SET is_transfer=? WHERE id=?",
        (new_val, account_id),
    )


# ── Category helpers ──────────────────────────────────────────────────────────

def _upsert_category(conn, name, direction, source):
    """
    Insert a category if (name, direction) not already present.
    On conflict: update source only if the incoming source is 'setup'
    (setup takes precedence over 'imported').
    """
    row = conn.execute(
        "SELECT id, source FROM cashbook_categories WHERE name=? AND direction=?",
        (name, direction),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO cashbook_categories (name, direction, source)
               VALUES (?, ?, ?)""",
            (name, direction, source),
        )
    elif source == "setup" and row[1] != "setup":
        conn.execute(
            "UPDATE cashbook_categories SET source='setup' WHERE id=?",
            (row[0],),
        )


# ── Salary-advance helpers ────────────────────────────────────────────────────

def _build_nickname_map(conn):
    """
    Build a dict {nickname_lower: employee_id} from the employees table.
    Skips rows with NULL/blank nickname.
    """
    rows = conn.execute(
        "SELECT id, nickname FROM employees WHERE nickname IS NOT NULL AND nickname != ''"
    ).fetchall()
    return {r[1].strip().lower(): r[0] for r in rows}


# ── Employee sync ─────────────────────────────────────────────────────────────

def _next_emp_code(conn):
    """
    Compute the next EMP-code by finding the highest existing numeric suffix
    and incrementing by 1.  Returns 'EMP001' if no employees exist.
    """
    rows = conn.execute(
        "SELECT emp_code FROM employees WHERE emp_code LIKE 'EMP%'"
    ).fetchall()
    max_n = 0
    for (code,) in rows:
        try:
            n = int(code[3:])
            if n > max_n:
                max_n = n
        except (ValueError, TypeError):
            pass
    return "EMP{:03d}".format(max_n + 1)


def sync_salary_sheet(parsed, conn):
    """
    Upsert HR employees from parsed salary rows.

    Parameters
    ----------
    parsed : dict
        The dict returned by parse_cashbook (uses parsed['salary']).
    conn : sqlite3.Connection

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
    """
    salary_rows = parsed.get("salary", [])
    result = {
        "created": [],
        "updated": [],
        "skipped": [],
        "warnings": [],
    }

    for row in salary_rows:
        first_name = row.get("first_name") or ""
        last_name  = row.get("last_name") or ""
        if not first_name:
            continue
        full_name = (first_name + " " + last_name).strip()
        nickname  = row.get("nickname")
        bank      = row.get("bank")
        bank_acct = row.get("bank_account_no")
        salary    = row.get("salary", 0.0) or 0.0
        sso_ded   = row.get("sso_deduction", 0.0) or 0.0
        is_active = row.get("is_active", True)

        # ── Try to match existing employee ───────────────────────────────────
        # Match by full_name first, then by nickname (for cases like บอล)
        emp_row = conn.execute(
            "SELECT id, emp_code, nickname, bank_name, bank_account_no, "
            "diligence_allowance, sso_enrolled, is_active FROM employees "
            "WHERE full_name=?",
            (full_name,),
        ).fetchone()

        if emp_row is None and nickname:
            emp_row = conn.execute(
                "SELECT id, emp_code, nickname, bank_name, bank_account_no, "
                "diligence_allowance, sso_enrolled, is_active FROM employees "
                "WHERE nickname=?",
                (nickname,),
            ).fetchone()

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

        # ── New employee ─────────────────────────────────────────────────────
        emp_code   = _next_emp_code(conn)
        sso_enroll = 1 if sso_ded > 0 else 0

        conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, nickname, bank_name, bank_account_no,
                  company_id, sso_enrolled, diligence_allowance,
                  is_active, start_date, probation_days)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 90)""",
            (emp_code, full_name, nickname or None, bank or None,
             bank_acct or None, _DEFAULT_COMPANY_ID, sso_enroll,
             1 if is_active else 0),
        )
        eid = conn.execute(
            "SELECT id FROM employees WHERE emp_code=?", (emp_code,)
        ).fetchone()[0]

        # Add ONE salary_history row — idempotent: check before insert.
        # reason must be one of ('initial','post_probation','raise','adjust')
        # per the CHECK constraint in employee_salary_history.
        # Use 'initial' with a note explaining the import source.
        existing_hist = conn.execute(
            """SELECT id FROM employee_salary_history
               WHERE employee_id=? AND effective_date=? AND reason='initial'""",
            (eid, _SALARY_HISTORY_EFFECTIVE_DATE),
        ).fetchone()
        if existing_hist is None:
            conn.execute(
                """INSERT INTO employee_salary_history
                     (employee_id, effective_date, monthly_salary, reason, note)
                   VALUES (?, ?, ?, 'initial', ?)""",
                (eid, _SALARY_HISTORY_EFFECTIVE_DATE, salary,
                 "imported_salary_sheet: เงินเดือนจาก Salary_Sheet; "
                 "start_date ไม่ทราบ — Put ต้องกรอกวันเริ่มงานจริงใน HR"),
            )

        result["created"].append(emp_code)
        result["warnings"].append(
            f"New employee {emp_code} '{full_name}' created from Salary_Sheet. "
            f"start_date is unknown — Put must set the real start date and verify SSO "
            f"enrollment in HR. company_id defaulted to {_DEFAULT_COMPANY_ID} (BSN)."
        )

    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def import_cashbook(path, conn=None):
    """
    Import a cashbook Excel workbook into the Sendy ERP database.

    Parameters
    ----------
    path : str
        Absolute path to the .xlsx workbook.
    conn : sqlite3.Connection or None
        Database connection.  If None, a new connection is opened via
        database.get_connection() and closed before return.

    Returns
    -------
    dict with keys:
      accounts_created   — count of new cashbook_accounts rows
      accounts_updated   — count of updated cashbook_accounts rows
      is_transfer        — dict {account_code: bool} for all accounts
      txn_counts         — dict {account_code: int} of rows inserted
      categories_added   — count of new cashbook_categories rows
      advances           — {'inserted': int, 'matched': int, 'unmatched': int}
      employees          — result dict from sync_salary_sheet
      reconciliation     — {income, expense, balance, overview_income,
                             overview_expense, overview_balance}
      warnings           — list of human-readable messages
    """
    # ── Connection management ─────────────────────────────────────────────────
    _own_conn = conn is None
    if _own_conn:
        conn = database.get_connection()

    try:
        return _do_import(path, conn)
    finally:
        if _own_conn:
            conn.close()


def _do_import(path, conn):
    """Core import logic operating on a given connection."""

    # ── Step 1: parse workbook ────────────────────────────────────────────────
    parsed = parse_cashbook(path)
    all_warnings = list(parsed.get("warnings", []))

    # ── Step 2: ensure migration 056 ─────────────────────────────────────────
    _ensure_migration_056(conn)

    # ── Step 3: build per-account transaction sums for transfer detection ─────
    account_income  = {}   # code → Σincome
    account_expense = {}   # code → Σexpense
    for txn in parsed["transactions"]:
        code = txn["account_code"]
        amt  = txn.get("amount") or 0.0
        if txn["direction"] == "income":
            account_income[code]  = account_income.get(code, 0.0) + amt
        else:
            account_expense[code] = account_expense.get(code, 0.0) + amt

    # ── Step 4: upsert cashbook_accounts ─────────────────────────────────────
    acct_id_map       = {}   # code → id
    accounts_created  = 0
    accounts_updated  = 0
    is_transfer_flags = {}   # code → bool

    for acct in parsed["accounts"]:
        code = acct["code"]
        existed = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE code=?", (code,)
        ).fetchone() is not None

        acct_id = _upsert_account(
            conn, code,
            acct.get("bank_name"),
            acct.get("bank_account_no"),
            acct.get("account_owner_name"),
            acct.get("note"),
        )
        if existed:
            accounts_updated += 1
        else:
            accounts_created += 1

        acct_id_map[code] = acct_id

        # Transfer detection
        inc = account_income.get(code, 0.0)
        exp = account_expense.get(code, 0.0)
        is_transfer = (inc > 0) and (abs(inc - exp) < _TRANSFER_TOLERANCE)
        is_transfer_flags[code] = is_transfer
        _set_is_transfer(conn, acct_id, is_transfer, all_warnings)

    # ── Step 5: idempotent full-replace transactions per account_id ───────────
    batch_id   = str(uuid.uuid4())
    source_file = os.path.basename(path)
    txn_counts  = {}

    # Group by account_code
    txns_by_code = {}
    for txn in parsed["transactions"]:
        txns_by_code.setdefault(txn["account_code"], []).append(txn)

    for code, acct_id in acct_id_map.items():
        # Delete existing rows for this account_id
        conn.execute(
            "DELETE FROM cashbook_transactions WHERE account_id=?",
            (acct_id,),
        )
        rows = txns_by_code.get(code, [])
        for txn in rows:
            conn.execute(
                """INSERT INTO cashbook_transactions
                     (account_id, txn_date, direction, category, user_category,
                      amount, description, note, source_file,
                      source_sheet, source_row, import_batch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    acct_id,
                    txn["txn_date"],
                    txn["direction"],
                    txn.get("category"),
                    txn.get("user_category"),
                    txn["amount"],
                    txn.get("description"),
                    txn.get("note"),
                    source_file,
                    txn.get("source_sheet"),
                    txn.get("source_row"),
                    batch_id,
                ),
            )
        txn_counts[code] = len(rows)

    # ── Step 6: upsert cashbook_categories ───────────────────────────────────
    cats_before = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories"
    ).fetchone()[0]

    # From Setup sheet (source='setup')
    for cat in parsed["categories"].get("income", []):
        if cat:
            _upsert_category(conn, cat, "income", "setup")
    for cat in parsed["categories"].get("expense", []):
        if cat:
            _upsert_category(conn, cat, "expense", "setup")

    # From transaction rows not already present (source='imported')
    for txn in parsed["transactions"]:
        cat = txn.get("category")
        if cat:
            direction = txn["direction"]
            _upsert_category(conn, cat, direction, "imported")

    cats_after = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories"
    ).fetchone()[0]
    categories_added = cats_after - cats_before

    # ── Step 7: salary_advances — full-replace ───────────────────────────────
    conn.execute("DELETE FROM salary_advances")

    # Build up-to-date nickname map (after employee sync would run, but sync
    # hasn't happened yet — use pre-existing employees for nickname matching;
    # new employees from this sheet also need matching, handled below)
    # We will do a two-pass: first sync employees, then match advances.

    # ── Step 8: HR employee sync ──────────────────────────────────────────────
    sync_result = sync_salary_sheet(parsed, conn)
    all_warnings.extend(sync_result.get("warnings", []))

    # ── Step 9: insert advances with nickname matching ───────────────────────
    # Rebuild nickname map now that new employees have been created
    nickname_map = _build_nickname_map(conn)

    adv_inserted  = 0
    adv_matched   = 0
    adv_unmatched = 0

    for adv in parsed.get("advances", []):
        raw_name = adv.get("raw_name")
        emp_id   = None
        if raw_name:
            emp_id = nickname_map.get(raw_name.strip().lower())

        if emp_id is not None:
            adv_matched += 1
        else:
            adv_unmatched += 1

        conn.execute(
            """INSERT INTO salary_advances
                 (employee_id, advance_date, amount, raw_name, note,
                  source_file, import_batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                emp_id,
                adv["advance_date"],
                adv["amount"],
                raw_name,
                adv.get("note"),
                source_file,
                batch_id,
            ),
        )
        adv_inserted += 1

    conn.commit()

    # ── Step 10: reconciliation block (excluding is_transfer accounts) ────────
    non_transfer_codes = {
        code for code, is_tr in is_transfer_flags.items() if not is_tr
    }
    # Also respect any pre-existing is_transfer=1 accounts (clobber-guard accounts)
    all_acct_rows = conn.execute(
        "SELECT code, is_transfer FROM cashbook_accounts"
    ).fetchall()
    non_transfer_codes = {r[0] for r in all_acct_rows if r[1] == 0}

    rec_income  = sum(
        t["amount"]
        for t in parsed["transactions"]
        if t["direction"] == "income" and t["account_code"] in non_transfer_codes
    )
    rec_expense = sum(
        t["amount"]
        for t in parsed["transactions"]
        if t["direction"] == "expense" and t["account_code"] in non_transfer_codes
    )
    ov = parsed.get("overview", {})

    reconciliation = {
        "income":           rec_income,
        "expense":          rec_expense,
        "balance":          rec_income - rec_expense,
        "overview_income":  ov.get("income", 0.0),
        "overview_expense": ov.get("expense", 0.0),
        "overview_balance": ov.get("balance", 0.0),
    }

    # ── Assemble return dict ──────────────────────────────────────────────────
    return {
        "accounts_created": accounts_created,
        "accounts_updated": accounts_updated,
        "is_transfer":      is_transfer_flags,
        "txn_counts":       txn_counts,
        "categories_added": categories_added,
        "advances": {
            "inserted":  adv_inserted,
            "matched":   adv_matched,
            "unmatched": adv_unmatched,
        },
        "employees":      sync_result,
        "reconciliation": reconciliation,
        "warnings":       all_warnings,
    }
