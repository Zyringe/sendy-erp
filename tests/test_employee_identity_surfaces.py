"""Per-surface render tests for the employee identity display (#463).

The filters themselves are pinned as pure functions in
`test_identity_filters.py`. THIS file asserts that each page actually renders
its values THROUGH them — a filter nothing calls is not a display fix.

Rendered at the TEMPLATE layer with no DB, for the reason
`test_customer_phone_surfaces.py` gives: a route test calls `get_connection()`
and creates `inventory_app/instance/inventory.db` inside the worktree, which
poisons every later run.

Every test carries a CONTROL asserting the fixture reached the region under
test, because an assertion over a page that never rendered pins nothing.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

NID = '1234567890123'
NID_MASKED = 'x-xxxx-xxxx0-12-3'


def _app():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    return flask_app


def _render(template, path='/', **ctx):
    app = _app()
    with app.test_request_context(path):
        return app.jinja_env.get_template(template).render(**ctx)


def _be_year(v):
    """The list still receives this helper by hand; #463 moves only the
    masking. Passing a stub keeps this test about the masking."""
    return str(v or '-')


# ── employee list — behaves exactly as before the helper moved ───────────────

def _employee_list(**over):
    emp = {'id': 1, 'emp_code': 'EMP001', 'full_name': 'ทดสอบ ระบบ',
           'nickname': None, 'position': 'ช่าง', 'company_name': 'BSN',
           'national_id': NID, 'start_date': '2024-01-01',
           'probation_end_date': None, 'is_active': 1}
    emp.update(over)
    # mask_nid is deliberately NOT passed: the page must reach the shared
    # filter itself.
    return _render('hr/employees.html', path='/hr/employees',
                   employees=[emp], show_inactive=False, be_year=_be_year)


def test_employee_list_masks_the_national_id_through_the_shared_filter():
    html = _employee_list()
    assert 'ทดสอบ ระบบ' in html, "CONTROL — the fixture never reached the page"
    assert NID_MASKED in html
    assert NID not in html, "the full number must not sit in the markup"


def test_employee_list_renders_a_missing_national_id_as_not_recorded():
    html = _employee_list(national_id=None)
    assert 'ทดสอบ ระบบ' in html, "CONTROL"
    assert NID_MASKED not in html


# ── employee detail ──────────────────────────────────────────────────────────

def _employee(**over):
    emp = {'id': 1, 'emp_code': 'EMP001', 'full_name': 'ทดสอบ ระบบ',
           'nickname': None, 'national_id': NID, 'gender': 'M',
           'phone': '0812345678', 'address': '1 ถนนทดสอบ', 'position': 'ช่าง',
           'company_id': 1, 'company_name': 'BSN', 'employment_type': 'monthly',
           'start_date': '2024-01-01', 'probation_days': 90,
           'probation_end_date': None, 'end_date': None, 'sso_enrolled': 1,
           'diligence_allowance': 0, 'bank_name': 'ธนาคารกสิกรไทย',
           'bank_branch': 'สาขาทดสอบ', 'bank_account_no': '1234567890',
           'bank_account_name': 'ทดสอบ ระบบ', 'salesperson_code': None,
           'user_id': None, 'is_active': 1, 'note': None,
           'current_salary': 0, 'on_payroll': 1, 'sort_order': 100}
    emp.update(over)
    return emp


def _detail(**over):
    return _render('hr/employee_detail.html', path='/hr/employees/1',
                   employee=_employee(**over), salary_history=[], wht_history=[],
                   leave_balance={}, leave_types=[], banks=[], year=2026,
                   be_year=_be_year, fmt_baht=lambda v: str(v),
                   linked_account=None, cashbook_accounts=[])


def test_detail_page_masks_the_national_id_by_default():
    html = _detail()
    assert 'ทดสอบ ระบบ' in html, "CONTROL — the fixture never reached the page"
    assert '>' + NID_MASKED + '<' in html, "the masked value is what is displayed"


def test_detail_page_carries_a_reveal_control_for_the_full_number():
    html = _detail()
    assert 'ทดสอบ ระบบ' in html, "CONTROL"
    assert 'data-nid-reveal' in html, "no control = masking blocks a real HR need"
    assert 'data-nid-full' in html


def test_the_reveal_control_carries_a_visible_affordance():
    """`<details>` is the native toggle, but `display:inline` on its <summary>
    removes the disclosure triangle in every WebKit/Blink browser — and the
    summary MUST be inline to sit beside the number in a <dd>. Without an
    explicit icon the control is invisible: the page would look like a plain
    masked number with nothing to click, which is not 'an explicit reveal
    control'."""
    html = _detail()
    assert 'data-nid-reveal' in html, "CONTROL"
    assert 'bi-eye' in html


def test_an_employee_with_no_national_id_gets_no_reveal_control():
    html = _detail(national_id=None)
    assert 'ทดสอบ ระบบ' in html, "CONTROL"
    assert 'data-nid-reveal' not in html
    assert NID_MASKED not in html


def test_detail_page_renders_the_phone_in_thai_blocks():
    html = _detail()
    assert 'ทดสอบ ระบบ' in html, "CONTROL"
    assert '>081-234-5678<' in html


def test_detail_page_groups_the_bank_account_for_a_known_bank():
    html = _detail()
    assert 'สาขาทดสอบ' in html, "CONTROL — the bank card rendered"
    assert '>123-4-56789-0<' in html


def test_detail_page_leaves_the_bank_account_plain_when_no_bank_is_recorded():
    """The other branch of the rule, asserted on the page and not only on the
    filter — a template that hard-coded the bank would pass the test above."""
    html = _detail(bank_name=None)
    assert 'สาขาทดสอบ' in html, "CONTROL"
    assert '>1234567890<' in html
    assert '123-4-56789-0' not in html


# ── payslip — the same shape as the employee record, or the two disagree ─────

def _payslip_item(**over):
    item = {'emp_code': 'EMP001', 'full_name': 'ทดสอบ ระบบ',
            'bank_name': 'ธนาคารกสิกรไทย', 'bank_branch': 'สาขาทดสอบ',
            'bank_account_no': '1234567890', 'bank_account_name': 'ทดสอบ ระบบ',
            'base_amount': 0, 'bonus': 0, 'carried_in': 0, 'carried_out': 0,
            'commission_amount': 0, 'diligence_allowance': 0,
            'diligence_forfeit_reason': None, 'diligence_forfeited': 0,
            'gross': 0, 'net_pay': 0, 'note': None, 'other_additions': 0,
            'other_additions_note': None, 'other_deductions': 0,
            'other_deductions_note': None, 'salary_advance_deduction': 0,
            'sso_employee': 0, 'sso_employer': 0, 'unpaid_leave_days': 0,
            'unpaid_leave_deduction': 0, 'wht_amount': 0}
    item.update(over)
    return item


def _payslip(**over):
    return _render('hr/payslip.html', path='/hr/payslip/1',
                   run={'id': 1, 'year_month': '2026-09', 'status': 'draft',
                        'finalized_at': None},
                   item=_payslip_item(**over),
                   employee={'position': 'ช่าง', 'start_date': '2024-01-01'},
                   be_year=_be_year, fmt_baht=lambda v: str(v))


def test_payslip_groups_the_bank_account_the_same_way_the_record_does():
    html = _payslip()
    assert 'สาขาทดสอบ' in html, "CONTROL — the bank block rendered"
    assert '>123-4-56789-0<' in html


def test_payslip_leaves_the_bank_account_plain_when_no_bank_is_recorded():
    html = _payslip(bank_name=None)
    assert 'สาขาทดสอบ' in html, "CONTROL"
    assert '>1234567890<' in html
    assert '123-4-56789-0' not in html


# ── payroll run detail — the ninth rendering this ticket exists to prevent ───

def _payroll_detail(**over):
    return _render('hr/payroll_detail.html', path='/hr/payroll/1',
                   # The transfer checklist — the only place this page shows a
                   # bank account — renders for a FINALIZED run only. A draft
                   # fixture made the CONTROL fail, which is what it is for.
                   run={'id': 1, 'year_month': '2026-09', 'status': 'finalized',
                        'finalized_at': '2026-09-30 10:00:00', 'note': None},
                   items=[_payslip_item(nickname='ทด', employee_id=1, id=1,
                                        default_cashbook_account_name=None,
                                        paid_at=None, **over)],
                   pay_accounts=[], dup_manual_salary_count=0, any_paid=False,
                   roster_drift_note=None, carry_consumed_note=None,
                   pending_advance_note=None, carry_forward_note=None,
                   today_iso='2026-09-10', be_year=_be_year,
                   fmt_baht=lambda v: str(v))


def test_payroll_detail_groups_the_bank_account_the_same_way():
    html = _payroll_detail()
    assert 'ธนาคารกสิกรไทย' in html, "CONTROL — the row rendered"
    assert '>123-4-56789-0<' in html


def test_payroll_detail_leaves_the_bank_account_plain_when_no_bank_is_recorded():
    html = _payroll_detail(bank_name=None)
    assert 'ทด' in html, "CONTROL"
    assert '123-4-56789-0' not in html
