"""#660 — regenerating a DRAFT payroll run must keep the admin's manual edits.

`generate_run` rebuilds every item from source (salary, leave, advances, WHT
history, carry-forward). Before #660 it also reset the fields only an admin
sets through `update_payroll_item` — bonus, other additions/deductions and
their notes, the manual "มาสาย" toggle — so an "อนุโลม" credit keyed against an
over-quota leave deduction vanished the next time leave arrived and the draft
was regenerated. These tests pin the contract: manual fields survive, derived
fields still recompute, WHT is re-derived from employee_wht_history on purpose.
"""
import hr

from tests.test_bp_hr_routes import admin_client  # noqa: F401  (fixture)
from tests.test_hr_payroll import _add_leave, _item, _mk_employee

MONTH = '2026-10'


def _gen(c):
    return hr.generate_run(MONTH, 1, created_by=1, conn=c)


def test_manual_additions_and_deductions_survive_regenerate(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_A', 'regen keeps edits', '2026-01-01',
                       monthly_salary=13000.0)
    _add_leave(c, eid, 'UNPAID', '2026-10-06', '2026-10-06', 1)
    run = _gen(c)
    it = _item(c, run['id'], eid)
    assert it['unpaid_leave_deduction'] == 433.33

    edited = hr.update_payroll_item(
        it['id'], bonus=200.0,
        other_additions=433.33, other_additions_note='อนุโลม ลาเกินสิทธิ์ 1 วัน',
        other_deductions=150.0, other_deductions_note='ค่าอุปกรณ์',
        conn=c)

    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['bonus'] == 200.0
    assert after['other_additions'] == 433.33
    assert after['other_additions_note'] == 'อนุโลม ลาเกินสิทธิ์ 1 วัน'
    assert after['other_deductions'] == 150.0
    assert after['other_deductions_note'] == 'ค่าอุปกรณ์'
    assert after['gross'] == edited['gross']
    assert after['net_pay'] == edited['net_pay']
    assert after['carried_out'] == edited['carried_out']


def test_carried_deduction_that_overdraws_keeps_the_same_carried_out(
        tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_N', 'regen overdraw', '2026-01-01',
                       monthly_salary=13000.0)
    run = _gen(c)
    edited = hr.update_payroll_item(
        _item(c, run['id'], eid)['id'], other_deductions=20000.0,
        other_deductions_note='ค่าเสียหาย', conn=c)
    assert edited['net_pay'] == 0.0
    assert edited['carried_out'] > 0

    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['other_deductions'] == 20000.0
    assert after['net_pay'] == 0.0
    assert after['carried_out'] == edited['carried_out']

def test_leave_added_after_the_edit_still_recomputes(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_B', 'regen recomputes', '2026-01-01',
                       monthly_salary=13000.0)
    _add_leave(c, eid, 'UNPAID', '2026-10-06', '2026-10-06', 1)
    run = _gen(c)
    it = _item(c, run['id'], eid)
    edited = hr.update_payroll_item(
        it['id'], other_additions=433.33,
        other_additions_note='อนุโลม', conn=c)

    _add_leave(c, eid, 'UNPAID', '2026-10-20', '2026-10-20', 1)
    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['unpaid_leave_days'] == 2
    assert after['unpaid_leave_deduction'] == 866.67
    assert after['other_additions'] == 433.33
    assert after['other_additions_note'] == 'อนุโลม'
    assert after['gross'] == edited['gross']
    assert after['net_pay'] == round(edited['net_pay'] - (866.67 - 433.33), 2)


def test_late_toggle_survives_regenerate(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_C', 'regen late', '2026-01-01',
                       monthly_salary=13000.0, diligence=500.0)
    run = _gen(c)
    it = _item(c, run['id'], eid)
    assert it['diligence_forfeited'] == 0
    edited = hr.update_payroll_item(it['id'], late=True, conn=c)

    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['diligence_forfeited'] == 1
    assert after['diligence_forfeit_reason'] == 'late'
    assert after['gross'] == edited['gross'] == after['base_amount']


def test_leave_forfeit_is_not_overwritten_to_late(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_D', 'regen leave wins', '2026-01-01',
                       monthly_salary=13000.0, diligence=500.0)
    run = _gen(c)
    hr.update_payroll_item(_item(c, run['id'], eid)['id'], late=True, conn=c)

    _add_leave(c, eid, 'SICK', '2026-10-08', '2026-10-08', 1)
    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['diligence_forfeited'] == 1
    assert after['diligence_forfeit_reason'] == 'leave'


def test_unedited_item_keeps_diligence_on_regenerate(tmp_db_conn_hr_clean):
    """The overlay must not invent a forfeit for someone never marked late."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_E', 'regen no late', '2026-01-01',
                       monthly_salary=13000.0, diligence=500.0)
    _gen(c)
    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['diligence_forfeited'] == 0
    assert after['diligence_forfeit_reason'] is None


def test_wht_is_rederived_from_history_not_carried(tmp_db_conn_hr_clean):
    """Deliberate: mig 157 made employee_wht_history the single source of
    truth, so a per-line WHT override does NOT survive a regenerate."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_F', 'regen wht', '2026-01-01',
                       monthly_salary=13000.0)
    c.execute("""INSERT INTO employee_wht_history
                   (employee_id, effective_date, monthly_wht, reason)
                 VALUES (?, '2026-01-01', 300, 'initial')""", (eid,))
    c.commit()
    run = _gen(c)
    it = _item(c, run['id'], eid)
    assert it['wht_amount'] == 300.0
    hr.update_payroll_item(it['id'], wht_amount=0.0, bonus=100.0, conn=c)

    run = _gen(c)
    after = _item(c, run['id'], eid)
    assert after['wht_amount'] == 300.0
    assert after['bonus'] == 100.0


def test_new_hire_gets_defaults_while_others_keep_edits(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    old = _mk_employee(c, 'T660_G', 'regen old', '2026-01-01',
                       monthly_salary=13000.0)
    run = _gen(c)
    hr.update_payroll_item(_item(c, run['id'], old)['id'], bonus=500.0,
                           other_deductions=50.0,
                           other_deductions_note='x', conn=c)

    newbie = _mk_employee(c, 'T660_H', 'regen new', '2026-10-01',
                          monthly_salary=13000.0, diligence=500.0)
    run = _gen(c)
    n = _item(c, run['id'], newbie)
    assert n['bonus'] == 0.0
    assert n['other_additions'] == 0.0
    assert n['other_additions_note'] is None
    assert n['other_deductions'] == 0.0
    assert n['other_deductions_note'] is None
    assert n['diligence_forfeited'] == 0
    assert _item(c, run['id'], old)['bonus'] == 500.0


KEPT_FLASH = 'คงยอดแก้มือไว้'


def _regen_via_route(client):
    resp = client.post('/hr/payroll/generate',
                       data={'year_month': MONTH, 'company_id': '1'},
                       follow_redirects=True)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_route_flashes_how_many_rows_kept_edits(tmp_db_conn_hr_clean,
                                                 admin_client):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T660_R', 'route kept', '2026-01-01',
                       monthly_salary=13000.0)
    run = _gen(c)
    hr.update_payroll_item(_item(c, run['id'], eid)['id'],
                           other_additions=433.33,
                           other_additions_note='อนุโลม', conn=c)

    html = _regen_via_route(admin_client)
    assert 'สร้าง/อัปเดต payroll run' in html
    assert f'{KEPT_FLASH} 1 แถว' in html


def test_route_does_not_flash_kept_when_nothing_was_edited(
        tmp_db_conn_hr_clean, admin_client):
    c = tmp_db_conn_hr_clean
    _mk_employee(c, 'T660_S', 'route plain', '2026-01-01')
    _gen(c)

    html = _regen_via_route(admin_client)
    assert 'สร้าง/อัปเดต payroll run' in html
    assert KEPT_FLASH not in html
