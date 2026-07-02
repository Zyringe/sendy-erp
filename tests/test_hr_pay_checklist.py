"""Phase 3 — transfer checklist (display) + employee default pay-from account.

Read-only / data-plumbing phase: NO money is posted here (that's Phase 4).
Covers:
1. `hr_queries.get_payroll_items` joins employee bank fields + nickname +
   default_cashbook_account_id/name (NULLs when the employee has none).
2. Setting `employees.default_cashbook_account_id` via the employee form
   persists, and a transfer account (is_transfer=1, e.g. code 904) is never
   offered by `get_active_cashbook_accounts(non_transfer_only=True)`.
3. `payroll_detail` renders the "จ่ายเงินเดือน (โอนเงิน)" checklist panel for
   a finalized run and NOT for a draft.

Fixture: `tmp_db` (copy of live DB — already carries mig 123's 4 new columns
+ the 6 seeded cashbook_accounts incl. transfer account code 904).
"""
import sqlite3

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_employee(conn, emp_code, full_name, nickname=None,
                 bank_name=None, bank_account_no=None,
                 default_cashbook_account_id=None, company_id=1):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, nickname, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active,
              bank_name, bank_account_no, default_cashbook_account_id)
           VALUES (?, ?, ?, 'M', ?, '2026-01-01', 90, 0, 0, 1, ?, ?, ?)""",
        (emp_code, full_name, nickname, company_id,
         bank_name, bank_account_no, default_cashbook_account_id),
    )
    conn.commit()
    return cur.lastrowid


def _mk_run(conn, year_month, status='finalized', company_id=1):
    cur = conn.execute(
        """INSERT INTO payroll_runs (year_month, company_id, status, run_date, created_by)
           VALUES (?, ?, ?, ?, 1)""",
        (year_month, company_id, status, f"{year_month}-28"),
    )
    conn.commit()
    return cur.lastrowid


def _mk_item(conn, run_id, employee_id, net_pay=15000.0):
    cur = conn.execute(
        """INSERT INTO payroll_items (run_id, employee_id, salary_rate, base_amount, net_pay)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, employee_id, net_pay, net_pay, net_pay),
    )
    conn.commit()
    return cur.lastrowid


def _transfer_account_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer = 1 AND is_active = 1 LIMIT 1"
    ).fetchone()
    assert row is not None, "expected a seeded is_transfer=1 active account (e.g. code 904)"
    return row[0]


# ── 1. get_payroll_items joins bank fields + nickname + default account ─────

def test_get_payroll_items_joins_bank_fields_and_default_account(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    import hr_queries as hrq

    acct_row = conn.execute(
        "SELECT id, code, display_name FROM cashbook_accounts"
        " WHERE is_transfer = 0 AND is_active = 1 LIMIT 1"
    ).fetchone()
    acct_id = acct_row['id']
    # Real seed data has display_name=NULL for every account (code is the
    # actual label) — the join must COALESCE to code so the panel never
    # silently shows "—" for an employee who DOES have a default set.
    expected_name = acct_row['display_name'] or acct_row['code']

    eid_with = _mk_employee(
        conn, 'T_PC1', 'มีบัญชี', nickname='แนน',
        bank_name='ธนาคารกสิกรไทย', bank_account_no='111-2-33334-5',
        default_cashbook_account_id=acct_id,
    )
    eid_without = _mk_employee(conn, 'T_PC2', 'ไม่มีบัญชี')

    run_id = _mk_run(conn, '2026-08')
    _mk_item(conn, run_id, eid_with, net_pay=20000.0)
    _mk_item(conn, run_id, eid_without, net_pay=18000.0)

    items = {i['emp_code']: i for i in hrq.get_payroll_items(run_id, conn=conn)}

    row_with = items['T_PC1']
    assert row_with['nickname'] == 'แนน'
    assert row_with['bank_name'] == 'ธนาคารกสิกรไทย'
    assert row_with['bank_account_no'] == '111-2-33334-5'
    assert row_with['default_cashbook_account_id'] == acct_id
    assert row_with['default_cashbook_account_name'] == expected_name

    row_without = items['T_PC2']
    assert row_without['nickname'] is None
    assert row_without['bank_name'] is None
    assert row_without['bank_account_no'] is None
    assert row_without['default_cashbook_account_id'] is None
    assert row_without['default_cashbook_account_name'] is None


# ── 2. employee form save + transfer-account exclusion ──────────────────────

def test_get_active_cashbook_accounts_excludes_transfer(tmp_db_conn):
    conn = tmp_db_conn
    import hr_queries as hrq

    transfer_id = _transfer_account_id(conn)
    accounts = hrq.get_active_cashbook_accounts(non_transfer_only=True, conn=conn)
    assert transfer_id not in {a['id'] for a in accounts}

    # sanity: without the filter the transfer account IS present (proves the
    # filter is actually doing something, not just an empty coincidence)
    unfiltered = hrq.get_active_cashbook_accounts(conn=conn)
    assert transfer_id in {a['id'] for a in unfiltered}

    # regression guard: the cashbook manual-entry + edit-modal dropdowns call
    # this with a POSITIONAL conn — get_active_cashbook_accounts(conn). That
    # must bind conn (not non_transfer_only) and therefore INCLUDE transfer
    # accounts like 904. Before the keyword-only fix, a positional conn landed
    # in non_transfer_only (truthy) and silently dropped 904 from those forms.
    positional = hrq.get_active_cashbook_accounts(conn)
    assert transfer_id in {a['id'] for a in positional}, \
        "positional conn must NOT be treated as non_transfer_only (904 dropped)"


def test_employee_form_save_sets_default_cashbook_account(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    import hr_queries as hrq

    acct_row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer = 0 AND is_active = 1 LIMIT 1"
    ).fetchone()
    acct_id = acct_row[0]

    eid = _mk_employee(conn, 'T_PC3', 'ทดสอบ')
    hrq.update_employee(eid, {
        'emp_code': 'T_PC3', 'full_name': 'ทดสอบ',
        'default_cashbook_account_id': str(acct_id),
    }, conn=conn)

    row = conn.execute(
        "SELECT default_cashbook_account_id FROM employees WHERE id=?", (eid,)
    ).fetchone()
    assert row[0] == acct_id


def test_employee_form_save_blank_default_account_coerces_to_none(tmp_db_conn_hr_clean):
    """Blank '— ไม่ระบุ —' selection must not raise an FK IntegrityError."""
    conn = tmp_db_conn_hr_clean
    import hr_queries as hrq

    acct_row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer = 0 AND is_active = 1 LIMIT 1"
    ).fetchone()
    acct_id = acct_row[0]

    eid = _mk_employee(conn, 'T_PC4', 'ทดสอบ2',
                       default_cashbook_account_id=acct_id)
    hrq.update_employee(eid, {
        'emp_code': 'T_PC4', 'full_name': 'ทดสอบ2',
        'default_cashbook_account_id': '',
    }, conn=conn)

    row = conn.execute(
        "SELECT default_cashbook_account_id FROM employees WHERE id=?", (eid,)
    ).fetchone()
    assert row[0] is None


def test_create_employee_sets_default_cashbook_account(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    import hr_queries as hrq

    acct_row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer = 0 AND is_active = 1 LIMIT 1"
    ).fetchone()
    acct_id = acct_row[0]

    new_id = hrq.create_employee({
        'emp_code': 'T_PC5', 'full_name': 'พนักงานใหม่',
        'default_cashbook_account_id': str(acct_id),
    }, conn=conn)

    row = conn.execute(
        "SELECT default_cashbook_account_id FROM employees WHERE id=?", (new_id,)
    ).fetchone()
    assert row[0] == acct_id


# ── Route-level: employee form select + POST persists (real request path) ──

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def test_employee_edit_route_persists_default_account(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    acct_id = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer = 0 AND is_active = 1 LIMIT 1"
    ).fetchone()[0]
    eid = _mk_employee(conn, 'T_PC6', 'route ทดสอบ')
    conn.close()

    resp = admin_client.post(
        f'/hr/employees/{eid}/edit',
        data={
            'emp_code': 'T_PC6', 'full_name': 'route ทดสอบ',
            'default_cashbook_account_id': str(acct_id),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.data[:500]

    row = sqlite3.connect(tmp_db).execute(
        "SELECT default_cashbook_account_id FROM employees WHERE id=?", (eid,)
    ).fetchone()
    assert row[0] == acct_id


def test_employee_form_select_excludes_transfer_account(admin_client, tmp_db):
    """The /employees/new page's select must not offer the transfer account."""
    conn = sqlite3.connect(tmp_db)
    transfer_code = conn.execute(
        "SELECT code FROM cashbook_accounts WHERE is_transfer = 1 AND is_active = 1 LIMIT 1"
    ).fetchone()[0]
    conn.close()

    resp = admin_client.get('/hr/employees/new')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'default_cashbook_account_id' in html
    # The transfer account's own display text must not appear inside the
    # default-pay-account select's option list.
    select_start = html.index('name="default_cashbook_account_id"')
    select_html = html[select_start:select_start + 2000]
    assert transfer_code not in select_html


# ── 3. payroll_detail checklist panel: finalized vs draft ───────────────────

def test_payroll_detail_shows_checklist_for_finalized_run(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    eid = _mk_employee(conn, 'T_PC7', 'checklist test', nickname='เช็คลิสต์',
                       bank_name='ธนาคารกรุงไทย', bank_account_no='999-9-99999-9')
    run_id = _mk_run(conn, '2026-08', status='finalized')
    _mk_item(conn, run_id, eid, net_pay=12345.0)
    conn.close()

    resp = admin_client.get(f'/hr/payroll/{run_id}')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'จ่ายเงินเดือน (โอนเงิน)' in html
    assert 'เช็คลิสต์' in html
    assert 'ธนาคารกรุงไทย' in html
    # Phase 4 supersedes Phase 3's read-only assertion here: the checklist is
    # now interactive for a can_edit_cashbook viewer (admin), so an unpaid
    # net_pay>0 item renders the จ่ายแล้ว pay-from form, not static text.
    # See tests/test_salary_pay_event.py for the full pay/unpay coverage.
    assert 'จ่ายแล้ว' in html


def test_payroll_detail_hides_checklist_for_draft_run(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    eid = _mk_employee(conn, 'T_PC8', 'draft test')
    run_id = _mk_run(conn, '2026-08', status='draft')
    _mk_item(conn, run_id, eid, net_pay=10000.0)
    conn.close()

    resp = admin_client.get(f'/hr/payroll/{run_id}')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'จ่ายเงินเดือน (โอนเงิน)' not in html
