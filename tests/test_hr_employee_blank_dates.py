"""TDD — employee form blank dates must store NULL, never '' (empty string).

Bug (2026-07-06): the employee create/edit form posts blank date inputs as
'' and create_employee/update_employee stored that verbatim. The payroll
generate filter `(end_date IS NULL OR end_date >= ?)` reads '' as an
always-past end date, silently dropping the employee from every payroll
run (hit EMP001 + EMP008 on prod). Mig 131 cleans existing rows;
hr_queries normalizes on save. Written RED first.
"""
import hr
import hr_queries as hrq

DATE_FIELDS = ("start_date", "end_date", "probation_end_date")


def _dates(conn, eid):
    return conn.execute(
        "SELECT start_date, end_date, probation_end_date FROM employees WHERE id=?",
        (eid,),
    ).fetchone()


def test_create_employee_blank_dates_store_null(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = hrq.create_employee({
        'emp_code': 'T_BD1', 'full_name': 'ทดสอบวันที่ว่าง',
        'start_date': '', 'end_date': '', 'probation_end_date': '',
    }, conn=conn)
    row = _dates(conn, eid)
    for f in DATE_FIELDS:
        assert row[f] is None, f"{f} should be NULL, got {row[f]!r}"


def test_update_employee_blank_end_date_stores_null(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = hrq.create_employee({
        'emp_code': 'T_BD2', 'full_name': 'ทดสอบแก้ไข',
        'start_date': '2026-01-01',
    }, conn=conn)
    # mimic the edit form: every field posted, blank dates as ''
    hrq.update_employee(eid, {
        'emp_code': 'T_BD2', 'full_name': 'ทดสอบแก้ไข', 'company_id': 1,
        'start_date': '2026-01-01', 'end_date': '', 'probation_end_date': '',
    }, conn=conn)
    row = _dates(conn, eid)
    assert row['start_date'] == '2026-01-01'
    assert row['end_date'] is None, f"end_date should be NULL, got {row['end_date']!r}"
    assert row['probation_end_date'] is None


def test_form_saved_employee_still_included_in_payroll_generate(tmp_db_conn_hr_clean):
    """The money-path regression: a form re-save with a blank end date must
    not knock the employee out of payroll generation."""
    conn = tmp_db_conn_hr_clean
    eid = hrq.create_employee({
        'emp_code': 'T_BD3', 'full_name': 'ทดสอบ payroll',
        'company_id': 1, 'start_date': '2026-01-01', 'sso_enrolled': 0,
    }, conn=conn)
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2026-01-01', 12000, 'initial')""",
        (eid,),
    )
    conn.commit()
    hrq.update_employee(eid, {
        'emp_code': 'T_BD3', 'full_name': 'ทดสอบ payroll', 'company_id': 1,
        'start_date': '2026-01-01', 'end_date': '', 'sso_enrolled': 0,
    }, conn=conn)

    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    item = conn.execute(
        "SELECT net_pay FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid),
    ).fetchone()
    assert item is not None, "employee with blank-form end_date dropped from payroll generate"
    assert item['net_pay'] == 12000
