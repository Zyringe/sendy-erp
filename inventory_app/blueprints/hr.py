"""HR blueprint — employees, leave, payroll routes.

Access control (enforced at two levels):
  1. app.py before_request: staff → redirect to dashboard for ANY hr.* endpoint.
  2. Each write route: abort(403) unless role == 'admin'.

Manager: full read (GET), no writes (POST blocked by before_request rule
         — manager is NOT in _MANAGER_POST_OK for hr.*, so any POST
         falls through to the "ต้องใช้บัญชี Admin" redirect).
Admin  : full read + write.
Staff  : blocked at GET level by before_request staff check.

Python 3.9 — Optional[...] not `X | None`.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for, make_response)

import hr as hr_mod
import hr_queries as hrq

bp_hr = Blueprint("hr", __name__, url_prefix="/hr")

# Canonical Thai bank names for the employee bank dropdown. Stored value =
# the clean Thai name so payslips/exports stay tidy.
BANK_OPTIONS = [
    "ธนาคารกสิกรไทย",
    "ธนาคารไทยพาณิชย์",
    "ธนาคารกรุงเทพ",
    "ธนาคารกรุงไทย",
    "ธนาคารกรุงศรีอยุธยา",
    "ธนาคารทหารไทยธนชาต",
    "ธนาคารยูโอบี",
    "ธนาคารเกียรตินาคินภัทร",
    "ธนาคารทิสโก้",
    "ธนาคารซีไอเอ็มบีไทย",
    "ธนาคารออมสิน",
    "ธนาคารเพื่อการเกษตรและสหกรณ์การเกษตร",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_admin():
    if session.get("role") != "admin":
        abort(403)


def _require_admin_or_manager():
    if session.get("role") not in ("admin", "manager"):
        abort(403)


def _be_year(iso_date: Optional[str]) -> str:
    """Convert 'YYYY-MM-DD' to 'DD/MM/พ.ศ.' Thai display string."""
    if not iso_date:
        return "-"
    try:
        d = date.fromisoformat(str(iso_date)[:10])
        be_year = d.year + 543
        return f"{d.day:02d}/{d.month:02d}/{be_year}"
    except ValueError:
        return str(iso_date)


def _mask_national_id(nid: Optional[str]) -> str:
    """Show only last 4 digits: 'x-xxxx-xxxxx-xx-x' style."""
    if not nid:
        return "-"
    digits = "".join(c for c in str(nid) if c.isdigit())
    if len(digits) < 4:
        return "xxxx"
    masked = "x" * (len(digits) - 4) + digits[-4:]
    # format as Thai 13-digit blocks: x-xxxx-xxxxx-xx-x
    if len(masked) == 13:
        return (f"{masked[0]}-{masked[1:5]}-{masked[5:10]}"
                f"-{masked[10:12]}-{masked[12]}")
    return masked


def _fmt_baht(value) -> str:
    try:
        return f"฿{float(value):,.2f}"
    except (TypeError, ValueError):
        return "฿0.00"


def _current_year_month() -> str:
    today = date.today()
    return today.strftime("%Y-%m")


# ── Dashboard ────────────────────────────────────────────────────────────────

@bp_hr.route("/")
def dashboard():
    today = date.today()
    today_iso = today.isoformat()
    cutoff_iso = (today + timedelta(days=14)).isoformat()

    headcount = hrq.get_headcount()
    on_leave = hrq.get_on_leave_today(today_iso)
    probation_ending = hrq.get_probation_ending(cutoff_iso, today_iso)
    payroll_runs = hrq.get_payroll_runs()
    pending_leave_count = hrq.get_leave_count_by_status("pending")

    # Stale-draft alert: any draft run whose year_month is strictly before
    # the current month. A draft for the current month is normal mid-prep;
    # a draft for a past month means someone forgot to finalize/regenerate.
    this_ym = today.strftime("%Y-%m")
    stale_drafts = [r for r in payroll_runs
                    if r["status"] == "draft" and r["year_month"] < this_ym]

    # Over-quota alerts: scan all active employees, current year
    year = today.year
    employees = hrq.get_employees(active_only=True)
    over_quota_alerts = []
    for emp in employees:
        try:
            bal = hr_mod.leave_balance(emp["id"], year)
            for code, b in bal.items():
                if b["over"] > 0:
                    over_quota_alerts.append({
                        "emp_code": emp["emp_code"],
                        "full_name": emp["full_name"],
                        "leave_code": code,
                        "over": b["over"],
                    })
        except Exception:
            pass

    return render_template(
        "hr/dashboard.html",
        headcount=headcount,
        on_leave=on_leave,
        probation_ending=probation_ending,
        payroll_runs=payroll_runs,
        stale_drafts=stale_drafts,
        over_quota_alerts=over_quota_alerts,
        pending_leave_count=pending_leave_count,
        today_iso=today_iso,
        be_year=_be_year,
        fmt_baht=_fmt_baht,
    )


# ── Employees ────────────────────────────────────────────────────────────────

@bp_hr.route("/employees")
def employee_list():
    show_inactive = request.args.get("inactive") == "1"
    employees = hrq.get_employees(active_only=not show_inactive)
    return render_template(
        "hr/employees.html",
        employees=employees,
        show_inactive=show_inactive,
        mask_nid=_mask_national_id,
        be_year=_be_year,
    )


@bp_hr.route("/employees/new", methods=["GET", "POST"])
def employee_new():
    _require_admin()
    companies = hrq.get_companies()
    if request.method == "POST":
        data = request.form.to_dict()
        if data.get("bank_name") == "__other__":
            data["bank_name"] = (request.form.get("bank_name_other") or "").strip()
        # Calculate probation_end_date if start_date provided
        _fill_probation_end(data)
        try:
            emp_id = hrq.create_employee(data)
            # Seed initial salary if provided
            salary = request.form.get("initial_salary", "").strip()
            if salary:
                hrq.add_salary_history(
                    emp_id,
                    data.get("start_date") or date.today().isoformat(),
                    float(salary),
                    "initial",
                )
            flash("เพิ่มพนักงานเรียบร้อย", "success")
            return redirect(url_for("hr.employee_detail", id=emp_id))
        except Exception as e:
            flash(f"ไม่สามารถบันทึก: {e}", "danger")
    return render_template(
        "hr/employee_form.html",
        employee=None,
        companies=companies,
        form=request.form if request.method == "POST" else {},
        action_url=url_for("hr.employee_new"),
        page_title="เพิ่มพนักงาน",
        banks=BANK_OPTIONS,
        next_emp_code=hrq.next_emp_code(),
        linkable_users=hrq.get_linkable_users(),
    )


@bp_hr.route("/employees/<int:id>")
def employee_detail(id: int):
    emp = hrq.get_employee(id)
    if not emp:
        abort(404)
    salary_history = hrq.get_employee_salary_history(id)
    year = date.today().year
    try:
        leave_bal = hr_mod.leave_balance(id, year)
    except Exception:
        leave_bal = {}
    leave_types = hrq.get_leave_types()
    return render_template(
        "hr/employee_detail.html",
        employee=emp,
        salary_history=salary_history,
        leave_balance=leave_bal,
        leave_types=leave_types,
        banks=BANK_OPTIONS,
        year=year,
        be_year=_be_year,
        fmt_baht=_fmt_baht,
        linkable_users=hrq.get_linkable_users(employee_id=id),
    )


@bp_hr.route("/employees/<int:id>/edit", methods=["POST"])
def employee_edit(id: int):
    _require_admin()
    emp = hrq.get_employee(id)
    if not emp:
        abort(404)
    data = request.form.to_dict()
    if data.get("bank_name") == "__other__":
        data["bank_name"] = (request.form.get("bank_name_other") or "").strip()
    _fill_probation_end(data)
    try:
        hrq.update_employee(id, data)
        flash("อัปเดตข้อมูลพนักงานเรียบร้อย", "success")
    except Exception as e:
        flash(f"ไม่สามารถบันทึก: {e}", "danger")
    return redirect(url_for("hr.employee_detail", id=id))


@bp_hr.route("/employees/<int:id>/salary", methods=["POST"])
def employee_salary_add(id: int):
    _require_admin()
    emp = hrq.get_employee(id)
    if not emp:
        abort(404)
    effective_date = request.form.get("effective_date", "").strip()
    salary = request.form.get("monthly_salary", "").strip()
    reason = request.form.get("reason", "raise").strip()
    note = request.form.get("note", "").strip() or None
    if not effective_date or not salary:
        flash("กรุณาระบุวันที่มีผลและเงินเดือน", "danger")
        return redirect(url_for("hr.employee_detail", id=id))
    try:
        hrq.add_salary_history(id, effective_date, float(salary), reason, note)
        flash("บันทึกประวัติเงินเดือนเรียบร้อย", "success")
    except Exception as e:
        flash(f"ไม่สามารถบันทึก: {e}", "danger")
    return redirect(url_for("hr.employee_detail", id=id))


@bp_hr.route("/employees/<int:id>/entitlements", methods=["GET", "POST"])
def employee_entitlements(id: int):
    emp = hrq.get_employee(id)
    if not emp:
        abort(404)
    year = int(request.args.get("year") or date.today().year)
    if request.method == "POST":
        _require_admin()
        year = int(request.form.get("year", year))
        leave_types = hrq.get_leave_types()
        for lt in leave_types:
            key = f"quota_{lt['id']}"
            val = request.form.get(key, "").strip()
            note_key = f"note_{lt['id']}"
            note = request.form.get(note_key, "").strip() or None
            if val == "":
                continue  # don't insert blank rows
            quota = float(val) if val else None
            hrq.upsert_entitlement(id, lt["id"], year, quota, note)
        flash(f"บันทึกสิทธิ์ลา ปี {year + 543} เรียบร้อย", "success")
        return redirect(url_for("hr.employee_entitlements", id=id, year=year))

    leave_types = hrq.get_leave_types()
    entitlements = hrq.get_employee_entitlements(id, year)
    ent_by_type = {e["leave_type_id"]: e for e in entitlements}
    try:
        leave_bal = hr_mod.leave_balance(id, year)
    except Exception:
        leave_bal = {}
    return render_template(
        "hr/employee_form.html",
        employee=emp,
        companies=hrq.get_companies(),
        form={},
        action_url=url_for("hr.employee_entitlements", id=id, year=year),
        page_title=f"สิทธิ์ลา — {emp['full_name']} ปี {year + 543}",
        mode="entitlements",
        leave_types=leave_types,
        ent_by_type=ent_by_type,
        leave_balance=leave_bal,
        year=year,
        be_year=_be_year,
        banks=BANK_OPTIONS,
    )


# ── Leave ────────────────────────────────────────────────────────────────────

@bp_hr.route("/leave")
def leave_list():
    emp_id = request.args.get("employee_id")
    ym = request.args.get("month") or ""
    lt_id = request.args.get("leave_type_id")

    requests = hrq.get_leave_requests(
        employee_id=int(emp_id) if emp_id else None,
        year_month=ym or None,
        leave_type_id=int(lt_id) if lt_id else None,
    )
    employees = hrq.get_employees(active_only=True)
    leave_types = hrq.get_leave_types()
    # Pending section: always show ALL pending (unfiltered) for admin/manager to action.
    role = session.get("role", "")
    pending_requests = (
        hrq.get_leave_requests(status="pending")
        if role in ("admin", "manager")
        else []
    )
    return render_template(
        "hr/leave.html",
        requests=requests,
        employees=employees,
        leave_types=leave_types,
        filter_emp=emp_id or "",
        filter_month=ym,
        filter_lt=lt_id or "",
        pending_requests=pending_requests,
        be_year=_be_year,
    )


@bp_hr.route("/leave/new", methods=["GET", "POST"])
def leave_new():
    _require_admin()
    employees = hrq.get_employees(active_only=True)
    leave_types = hrq.get_leave_types()
    if request.method == "POST":
        data = request.form.to_dict()
        created_by = session.get("user_id", 0)
        try:
            req_id = hrq.create_leave_request(data, created_by)
            flash("บันทึกการลาเรียบร้อย", "success")
            return redirect(url_for("hr.leave_list"))
        except Exception as e:
            flash(f"ไม่สามารถบันทึก: {e}", "danger")
    return render_template(
        "hr/leave_form.html",
        req=None,
        employees=employees,
        leave_types=leave_types,
        form=request.form if request.method == "POST" else {},
        action_url=url_for("hr.leave_new"),
        page_title="บันทึกการลา",
    )


@bp_hr.route("/leave/<int:id>/edit", methods=["POST"])
def leave_edit(id: int):
    _require_admin()
    req = hrq.get_leave_request(id)
    if not req:
        abort(404)
    data = request.form.to_dict()
    try:
        hrq.update_leave_request(id, data)
        flash("อัปเดตการลาเรียบร้อย", "success")
    except Exception as e:
        flash(f"ไม่สามารถบันทึก: {e}", "danger")
    return redirect(url_for("hr.leave_list"))


@bp_hr.route("/leave/<int:rid>/approve", methods=["POST"])
def leave_approve(rid: int):
    _require_admin_or_manager()
    req = hrq.get_leave_request(rid)
    if not req or req["status"] != "pending":
        flash("ไม่พบคำขอหรือสถานะไม่ถูกต้อง", "warning")
        return redirect(url_for("hr.leave_list"))
    hrq.update_leave_request(rid, {
        "status": "approved",
        "approved_by": session.get("username"),
        "approved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    flash("อนุมัติคำขอลาแล้ว", "success")
    return redirect(url_for("hr.leave_list"))


@bp_hr.route("/leave/<int:rid>/reject", methods=["POST"])
def leave_reject(rid: int):
    _require_admin_or_manager()
    req = hrq.get_leave_request(rid)
    if not req or req["status"] != "pending":
        flash("ไม่พบคำขอหรือสถานะไม่ถูกต้อง", "warning")
        return redirect(url_for("hr.leave_list"))
    hrq.update_leave_request(rid, {
        "status": "rejected",
        "approved_by": session.get("username"),
        "approved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    flash("ปฏิเสธคำขอลาแล้ว", "warning")
    return redirect(url_for("hr.leave_list"))


@bp_hr.route("/leave/<int:id>/delete", methods=["POST"])
def leave_delete(id: int):
    _require_admin()
    try:
        hrq.delete_leave_request(id)
        flash("ลบรายการลาเรียบร้อย", "success")
    except Exception as e:
        flash(f"ไม่สามารถลบ: {e}", "danger")
    return redirect(url_for("hr.leave_list"))


# ── Payroll ──────────────────────────────────────────────────────────────────

@bp_hr.route("/payroll")
def payroll_list():
    runs = hrq.get_payroll_runs()
    companies = hrq.get_companies()
    return render_template(
        "hr/payroll.html",
        runs=runs,
        companies=companies,
        current_ym=_current_year_month(),
        be_year=_be_year,
        fmt_baht=_fmt_baht,
    )


@bp_hr.route("/payroll/generate", methods=["POST"])
def payroll_generate():
    _require_admin()
    year_month = request.form.get("year_month", "").strip()
    company_id = request.form.get("company_id", "").strip()
    if not year_month or not company_id:
        flash("กรุณาระบุเดือนและบริษัท", "danger")
        return redirect(url_for("hr.payroll_list"))
    created_by = session.get("user_id", 0)
    try:
        run = hr_mod.generate_run(year_month, int(company_id), created_by)
        flash(f"สร้าง/อัปเดต payroll run #{run['id']} เรียบร้อย", "success")
        return redirect(url_for("hr.payroll_detail", run_id=run["id"]))
    except Exception as e:
        flash(f"ไม่สามารถสร้าง payroll: {e}", "danger")
        return redirect(url_for("hr.payroll_list"))


@bp_hr.route("/payroll/<int:run_id>")
def payroll_detail(run_id: int):
    run = hrq.get_payroll_run(run_id)
    if not run:
        abort(404)
    items = hrq.get_payroll_items(run_id)
    return render_template(
        "hr/payroll_detail.html",
        run=run,
        items=items,
        be_year=_be_year,
        fmt_baht=_fmt_baht,
    )


@bp_hr.route("/payroll/<int:run_id>/item/<int:item_id>", methods=["POST"])
def payroll_item_edit(run_id: int, item_id: int):
    _require_admin()
    run = hrq.get_payroll_run(run_id)
    if not run or run["status"] == "finalized":
        flash("ไม่สามารถแก้ไข payroll ที่ finalized แล้ว", "danger")
        return redirect(url_for("hr.payroll_detail", run_id=run_id))

    def _float_or_none(key):
        v = request.form.get(key, "").strip()
        return float(v) if v else None

    def _bool_or_none(key):
        v = request.form.get(key, "")
        if v == "1":
            return True
        if v == "0":
            return False
        return None

    try:
        hr_mod.update_payroll_item(
            item_id,
            bonus=_float_or_none("bonus"),
            other_additions=_float_or_none("other_additions"),
            other_additions_note=request.form.get("other_additions_note") or None,
            other_deductions=_float_or_none("other_deductions"),
            other_deductions_note=request.form.get("other_deductions_note") or None,
            late=_bool_or_none("late"),
        )
        flash("อัปเดตรายการเรียบร้อย", "success")
    except Exception as e:
        flash(f"ไม่สามารถบันทึก: {e}", "danger")
    return redirect(url_for("hr.payroll_detail", run_id=run_id))


@bp_hr.route("/payroll/<int:run_id>/finalize", methods=["POST"])
def payroll_finalize(run_id: int):
    _require_admin()
    run = hrq.get_payroll_run(run_id)
    if not run:
        abort(404)
    if run["status"] == "finalized":
        flash("Run นี้ finalized แล้ว", "warning")
        return redirect(url_for("hr.payroll_detail", run_id=run_id))
    try:
        hr_mod.finalize_run(run_id)
        flash(f"Finalized payroll run #{run_id} เรียบร้อย", "success")
    except Exception as e:
        flash(f"ไม่สามารถ finalize: {e}", "danger")
    return redirect(url_for("hr.payroll_detail", run_id=run_id))


@bp_hr.route("/payroll/<int:run_id>/reopen", methods=["POST"])
def payroll_reopen(run_id: int):
    _require_admin()
    run = hrq.get_payroll_run(run_id)
    if not run:
        abort(404)
    if run["status"] != "finalized":
        flash("Run นี้เป็น draft อยู่แล้ว ไม่ต้อง reopen", "warning")
        return redirect(url_for("hr.payroll_detail", run_id=run_id))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("ต้องระบุเหตุผลในการ reopen run นี้", "danger")
        return redirect(url_for("hr.payroll_detail", run_id=run_id))
    try:
        hr_mod.reopen_run(run_id, reason=reason,
                          actor=session.get("username") or "unknown")
        flash(f"Reopened run #{run_id} แล้ว — แก้ไขเสร็จอย่าลืม finalize ใหม่", "success")
    except Exception as e:
        flash(f"ไม่สามารถ reopen: {e}", "danger")
    return redirect(url_for("hr.payroll_detail", run_id=run_id))


@bp_hr.route("/payroll/<int:run_id>/export.csv")
def payroll_export(run_id: int):
    run = hrq.get_payroll_run(run_id)
    if not run:
        abort(404)
    items = hrq.get_payroll_items(run_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "รหัสพนักงาน", "ชื่อ-นามสกุล",
        "เงินเดือน", "ฐานคำนวณ",
        "วันลาไม่รับค่าจ้าง", "หักลา",
        "เบี้ยขยัน", "โบนัส", "รายการเพิ่มอื่น",
        "หักอื่น", "ประกันสังคม (ลูกจ้าง)",
        "เบิกล่วงหน้า",
        "รวมก่อนหัก", "เงินสุทธิ",
        "หมายเหตุ",
    ])
    for item in items:
        writer.writerow([
            item["emp_code"], item["full_name"],
            item["salary_rate"], item["base_amount"],
            item["unpaid_leave_days"], item["unpaid_leave_deduction"],
            item["diligence_allowance"] if not item["diligence_forfeited"] else 0,
            item["bonus"], item["other_additions"],
            item["other_deductions"], item["sso_employee"],
            item["salary_advance_deduction"],
            item["gross"], item["net_pay"],
            item["note"] or "",
        ])

    output = make_response(buf.getvalue())
    output.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    ym = run["year_month"].replace("-", "")
    output.headers["Content-Disposition"] = (
        f'attachment; filename="payroll_{ym}_run{run_id}.csv"'
    )
    return output


@bp_hr.route("/payroll/<int:run_id>/payslip/<int:item_id>")
def payslip(run_id: int, item_id: int):
    run = hrq.get_payroll_run(run_id)
    if not run:
        abort(404)
    item = hrq.get_payroll_item(item_id)
    if not item or item["run_id"] != run_id:
        abort(404)
    emp = hrq.get_employee(item["employee_id"])
    return render_template(
        "hr/payslip.html",
        run=run,
        item=item,
        employee=emp,
        be_year=_be_year,
        fmt_baht=_fmt_baht,
    )


# ── Private helpers ──────────────────────────────────────────────────────────

def _fill_probation_end(data: dict):
    """Calculate probation_end_date from start_date + probation_days if not set."""
    if data.get("probation_end_date"):
        return
    start = data.get("start_date", "").strip()
    days_str = data.get("probation_days", "90")
    try:
        d = date.fromisoformat(start)
        days = int(days_str or 90)
        end = d + timedelta(days=days - 1)
        data["probation_end_date"] = end.isoformat()
    except (ValueError, TypeError):
        pass
