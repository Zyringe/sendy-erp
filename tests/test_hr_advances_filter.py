"""Filters + default views for HR advances & leave.

Change under test:
  - get_salary_advances(employee_id, status) filters by employee and by
    รอหัก (pending, deducted_in_run_id IS NULL) / ถูกหักแล้ว (deducted).
  - /hr/advances DEFAULTS to รอหัก (money still owed back); ?status=all shows
    every status.
  - /hr/leave DEFAULTS to the current month; ?month= (blank) shows all months.

Fixture `tmp_db_conn_hr_clean` wipes salary_advances / leave_requests /
payroll_runs from the copied live DB so counts are deterministic, while
preserving the seeded employees + leave_types.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
from datetime import date

import hr_queries as hrq


def _two_emps(conn):
    rows = conn.execute(
        "SELECT id FROM employees WHERE is_active=1 ORDER BY id LIMIT 2"
    ).fetchall()
    return rows[0][0], rows[1][0]


def _mk_run(conn, ym):
    return conn.execute(
        "INSERT INTO payroll_runs (year_month, company_id, status, created_by, created_at) "
        "VALUES (?, 1, 'finalized', 'test', datetime('now','localtime'))",
        (ym,),
    ).lastrowid


def _mk_advance(conn, emp_id, advance_date, amount, deducted_run=None, note=None):
    return conn.execute(
        "INSERT INTO salary_advances "
        "  (employee_id, advance_date, amount, deducted_in_run_id, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (emp_id, advance_date, amount, deducted_run, note),
    ).lastrowid


def _client(role='admin', user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    cl = a.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return cl


# ── query-level ──────────────────────────────────────────────────────────────

def test_get_salary_advances_status_and_employee_filter(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    a, b = _two_emps(c)
    run = _mk_run(c, '2026-05')
    pend = _mk_advance(c, a, '2026-07-01', 111.0)          # pending, emp A
    dedu = _mk_advance(c, b, '2026-05-02', 222.0, run)     # deducted, emp B
    c.commit()

    all_ids = {r['id'] for r in hrq.get_salary_advances(conn=c)}
    assert all_ids == {pend, dedu}

    pending_ids = {r['id'] for r in hrq.get_salary_advances(status='pending', conn=c)}
    assert pending_ids == {pend}

    deducted_ids = {r['id'] for r in hrq.get_salary_advances(status='deducted', conn=c)}
    assert deducted_ids == {dedu}

    a_ids = {r['id'] for r in hrq.get_salary_advances(employee_id=a, conn=c)}
    assert a_ids == {pend}

    # combined AND: emp A's only advance is pending → A+deducted = empty
    assert hrq.get_salary_advances(employee_id=a, status='deducted', conn=c) == []


# ── route defaults ───────────────────────────────────────────────────────────

def test_advances_route_defaults_to_pending(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    a, b = _two_emps(c)
    run = _mk_run(c, '2026-05')
    _mk_advance(c, a, '2026-07-01', 40197.0)          # pending, unique amount
    _mk_advance(c, b, '2026-05-02', 40286.0, run)     # deducted, unique amount
    c.commit()

    cl = _client()
    html = cl.get('/hr/advances').get_data(as_text=True)
    assert '40,197' in html, "pending advance must show in the default view"
    assert '40,286' not in html, "deducted advance must be hidden in the default (รอหัก) view"

    html_all = cl.get('/hr/advances?status=all').get_data(as_text=True)
    assert '40,197' in html_all and '40,286' in html_all, "?status=all shows every status"


def test_leave_route_defaults_to_current_month(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    a, _ = _two_emps(c)
    sick = c.execute("SELECT id FROM leave_types WHERE code='SICK'").fetchone()[0]
    cur = date.today().strftime('%Y-%m')
    c.execute(
        "INSERT INTO leave_requests (employee_id, leave_type_id, start_date, end_date, days, status, reason) "
        "VALUES (?, ?, ?, ?, 1, 'approved', 'CURMONTHMARK')",
        (a, sick, cur + '-15', cur + '-15'),
    )
    c.execute(
        "INSERT INTO leave_requests (employee_id, leave_type_id, start_date, end_date, days, status, reason) "
        "VALUES (?, ?, '2020-01-10', '2020-01-10', 1, 'approved', 'OLDMONTHMARK')",
        (a, sick),
    )
    c.commit()

    cl = _client()
    html = cl.get('/hr/leave').get_data(as_text=True)
    assert 'CURMONTHMARK' in html, "current-month leave must show by default"
    assert 'OLDMONTHMARK' not in html, "past-month leave must be hidden by the current-month default"

    html_all = cl.get('/hr/leave?month=').get_data(as_text=True)
    assert 'CURMONTHMARK' in html_all and 'OLDMONTHMARK' in html_all, "?month= (blank) shows all months"


# ── account name falls back to the code ─────────────────────────────────────

def test_advance_account_name_falls_back_to_the_account_code(tmp_db_conn_hr_clean):
    """`cashbook_accounts.display_name` is NULL for every account on prod, and
    the /admin form has no field that writes it — so this query, the only
    consumer that read it WITHOUT a fallback, rendered '—' in the โอนจากบัญชี
    column for all 28 advances (verified in the browser 2026-08-05).

    Everywhere else already falls back: COALESCE(ca.display_name, ca.code) in
    get_employees, `{{ acct.display_name or acct.code }}` in the cashbook
    templates. Both directions are asserted so the test cannot pass by always
    returning one of them.

    BOTH consumers are asserted: the production fix touched `get_salary_advances`
    AND `get_salary_advance`, and a test covering only the list query would let
    the single-row one be reverted without going red (Codex review of PR #367).
    """
    c = tmp_db_conn_hr_clean
    emp, _ = _two_emps(c)
    acct = c.execute(
        "SELECT id, code FROM cashbook_accounts ORDER BY id LIMIT 1").fetchone()
    assert acct is not None, "fixture must have an account, or this pins nothing"
    c.execute("UPDATE cashbook_accounts SET display_name=NULL WHERE id=?",
              (acct["id"],))
    adv_id = c.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id)"
        " VALUES (?, '2026-10-05', 500, ?)", (emp, acct["id"])).lastrowid
    c.commit()

    row = next(r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id)
    assert row["account_name"] == acct["code"], "list query"
    assert hrq.get_salary_advance(adv_id, conn=c)["account_name"] == acct["code"], \
        "single-row query"

    # ...and a real display_name still wins over the code
    c.execute("UPDATE cashbook_accounts SET display_name='ชื่อจริงของบัญชี' WHERE id=?",
              (acct["id"],))
    c.commit()
    row = next(r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id)
    assert row["account_name"] == 'ชื่อจริงของบัญชี', "list query"
    assert hrq.get_salary_advance(adv_id, conn=c)["account_name"] == 'ชื่อจริงของบัญชี', \
        "single-row query"


def test_advance_with_no_account_still_reports_no_account_name(tmp_db_conn_hr_clean):
    """`from_account_id IS NULL` must keep `account_name` NULL so /hr/advances
    still renders '—' for an advance that has no cash account behind it.

    COALESCE could plausibly have been read as "always produce something", and
    I asserted this behaviour to the reviewer from the LEFT JOIN semantics
    without anything pinning it — the join miss makes both `ca.display_name`
    and `ca.code` NULL, so COALESCE returns NULL. That reasoning is right, but
    reasoning is not a test: nothing would have caught a later change to
    `COALESCE(ca.display_name, ca.code, sa.id)` or a switch to an inner join.

    The 16 rows linked on prod on 2026-08-05 all carry an account, so this
    state is currently rare — which is exactly when an invariant rots unnoticed.
    """
    c = tmp_db_conn_hr_clean
    emp, _ = _two_emps(c)
    adv_id = c.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id)"
        " VALUES (?, '2026-10-06', 700, NULL)", (emp,)).lastrowid
    c.commit()

    row = next(r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id)
    assert row["from_account_id"] is None, "fixture precondition"
    assert row["account_name"] is None, "list query"
    assert hrq.get_salary_advance(adv_id, conn=c)["account_name"] is None, \
        "single-row query"

    # the row itself must still be returned — a NULL account cannot make an
    # advance disappear from the page (an inner join would do exactly that)
    assert adv_id in {r["id"] for r in hrq.get_salary_advances(conn=c)}


# ── รายละเอียด: the linked cashbook row's description ────────────────────────

def _mk_linked_cashbook_row(conn, adv_id, acct_id, description, note=None,
                            txn_date='2026-10-07', amount=500.0):
    """Mirror what /cashbook/new writes: the advance's partner expense row.

    blueprints/cashbook.py::_resolve_advance_rows inserts BOTH rows in one
    commit — salary_advances (which stores `note` only) and this row (which
    stores `description` AND `note`) — linked by salary_advance_id.
    """
    return conn.execute(
        "INSERT INTO cashbook_transactions "
        "  (account_id, txn_date, direction, category, amount, description, note,"
        "   salary_advance_id) "
        "VALUES (?, ?, 'expense', 'เงินเดือน (เบิกล่วงหน้า)', ?, ?, ?, ?)",
        (acct_id, txn_date, amount, description, note, adv_id),
    ).lastrowid


def test_get_salary_advances_exposes_the_linked_cashbook_description(
        tmp_db_conn_hr_clean):
    """/hr/advances is a read-only mirror (CONTEXT.md) and `salary_advances`
    has no description column — the รายละเอียด the user typed lives on the
    LINKED cashbook row, one join away, and the page showed only หมายเหตุ.

    Measured on the local dev snapshot 2026-09-08: 23 of 28 advances carried a
    linked description that the page never rendered, and 0 advances lacked a
    partner row.

    Only the LIST query is changed. `get_salary_advance` (singular) has no
    production consumer — grep 2026-09-08 finds only tests — so giving it the
    field too would be speculative. That is deliberate, not an oversight; the
    account_name pair above is different because that was a shared fallback bug.
    """
    c = tmp_db_conn_hr_clean
    emp, _ = _two_emps(c)
    acct = c.execute(
        "SELECT id FROM cashbook_accounts ORDER BY id LIMIT 1").fetchone()
    assert acct is not None, "fixture must have an account, or this pins nothing"

    adv_id = _mk_advance(c, emp, '2026-10-07', 500, note='NOTEMARK')
    _mk_linked_cashbook_row(c, adv_id, acct["id"],
                            description='DESCMARK', note='NOTEMARK')
    c.commit()

    rows = [r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id]
    assert len(rows) == 1, "exactly one row for this advance, or the rest is vacuous"
    row = rows[0]
    # CONTROL: the advance's own note still arrives — proves the fixture reaches
    # the query and that a broken join did not just blank the whole row.
    assert row["note"] == 'NOTEMARK'
    assert row["cashbook_description"] == 'DESCMARK'


def test_advance_without_a_linked_cashbook_row_still_lists_with_no_description(
        tmp_db_conn_hr_clean):
    """The join must be a LEFT join. Every advance on the local snapshot has a
    partner row today, which is exactly when this invariant rots unnoticed: an
    inner join would silently drop any advance created by a path that does not
    write the cashbook side (the pre-mig-128 rows, or a future importer).
    """
    c = tmp_db_conn_hr_clean
    emp, _ = _two_emps(c)
    adv_id = _mk_advance(c, emp, '2026-10-08', 700, note='LONEMARK')
    c.commit()

    ids = {r["id"] for r in hrq.get_salary_advances(conn=c)}
    assert adv_id in ids, "an advance with no cashbook partner must not disappear"
    row = next(r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id)
    assert row["note"] == 'LONEMARK'          # CONTROL
    assert row["cashbook_description"] is None


def test_advances_page_renders_the_description_column(tmp_db_conn_hr_clean):
    """Both columns must reach the HTML, and the empty-state colspan must track
    the header count — adding a <th> without widening the colspan leaves a
    visibly broken 'ไม่มีรายการ' row that no substring assertion would catch.
    """
    c = tmp_db_conn_hr_clean
    emp, _ = _two_emps(c)
    acct = c.execute(
        "SELECT id FROM cashbook_accounts ORDER BY id LIMIT 1").fetchone()
    assert acct is not None, "fixture must have an account, or this pins nothing"
    adv_id = _mk_advance(c, emp, '2026-10-09', 900, note='หมายเหตุทดสอบ')
    _mk_linked_cashbook_row(c, adv_id, acct["id"],
                            description='รายละเอียดทดสอบ', note='หมายเหตุทดสอบ')
    c.commit()

    cl = _client()
    html = cl.get('/hr/advances').get_data(as_text=True)
    assert 'หมายเหตุทดสอบ' in html, "CONTROL: the note column still renders"
    assert 'รายละเอียดทดสอบ' in html, "the linked description must reach the page"
    # header label, asserted as an element so it cannot match a longer word
    assert '>รายละเอียด<' in html

    # empty state: no pending advances at all → the placeholder row must span
    # every column
    c.execute("DELETE FROM cashbook_transactions WHERE salary_advance_id=?", (adv_id,))
    c.execute("DELETE FROM salary_advances WHERE id=?", (adv_id,))
    c.commit()
    empty = cl.get('/hr/advances').get_data(as_text=True)
    assert 'ไม่มีรายการเบิกล่วงหน้า' in empty, "CONTROL: this really is the empty state"
    # Derive the width from the header rather than restating a literal, or a
    # future 9th <th> with the colspan left at 8 would still pass — the exact
    # bug this test exists to catch. `<th(\s|>)` cannot match `<thead>`.
    head = re.search(r'<thead>.*?</thead>', empty, re.S)
    assert head, "CONTROL: the advances table head must be in the page"
    n_cols = len(re.findall(r'<th(?:\s[^>]*)?>', head.group(0)))
    assert n_cols == 8, f"header should carry 8 columns, found {n_cols}"
    assert f'colspan="{n_cols}"' in empty, \
        f"placeholder must span all {n_cols} header columns"
