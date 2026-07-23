"""Self-service leave blueprint — /me/*

Each employee sees and manages ONLY their own leave data.

Security design:
- _my_employee() resolves session['user_id'] → employee row.
  Returns None for admin/shareholder (leave-exempt) and for any session
  without a linked user_id. Never reads employee_id from the URL or form.
- Every route calls _my_employee() first; None → redirect (not 403).

Python 3.9 — Optional[...] not `X | None`.
"""
from __future__ import annotations

import json
from datetime import date

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import hr as hr_mod
import hr_queries as hrq
from access_control import pw_fingerprint
from database import get_connection

bp_me = Blueprint("me", __name__, url_prefix="/me")


def _my_employee():
    """Resolve the logged-in user to their employee row, or None if exempt."""
    if session.get("role") in ("admin", "shareholder"):
        return None
    uid = session.get("user_id")
    return hrq.get_employee_by_user_id(uid) if uid else None


@bp_me.route("/leave")
def leave():
    emp = _my_employee()
    if not emp:
        flash("บัญชีนี้ไม่มีระบบลา", "info")
        return redirect(url_for("dashboard"))
    year = date.today().year
    return render_template(
        "me/leave.html",
        employee=emp,
        requests=hrq.get_leave_requests(employee_id=emp["id"]),
        balance=hr_mod.leave_balance(emp["id"], year),
        leave_types=hrq.get_leave_types(),
        year=year,
    )


# ── Self-scoped writes ────────────────────────────────────────────────────────
#
# The ownership guard is the security core: employee_id is ALWAYS taken from the
# session-resolved employee (_my_employee()), never from the form or URL. Edit
# and cancel additionally re-load the target row and assert BOTH ownership
# (row.employee_id == my id) AND status == 'pending' before mutating — any
# mismatch is a hard abort(403) with the DB left unchanged.

@bp_me.route("/leave/new", methods=["POST"])
def leave_submit():
    emp = _my_employee()
    if not emp:
        abort(403)
    data = {
        "employee_id": emp["id"],          # from session — never from the form
        "leave_type_id": request.form["leave_type_id"],
        "start_date": request.form["start_date"],
        "end_date": request.form["end_date"],
        "days": request.form.get("days", 1),
        "reason": request.form.get("reason", ""),
        "status": "pending",
    }
    new_rid = hrq.create_leave_request(data, created_by=session.get("username"))
    conn = get_connection()
    conn.execute(
        "INSERT INTO audit_log(table_name, row_id, action, changed_fields, user)"
        " VALUES(?,?,?,?,?)",
        ("leave_requests", new_rid, "INSERT",
         json.dumps({"status": "pending"}, ensure_ascii=False),
         session.get("username")),
    )
    conn.commit()
    conn.close()
    flash("ส่งคำขอลาแล้ว รออนุมัติ", "success")
    return redirect(url_for("me.leave"))


@bp_me.route("/leave/<int:rid>/edit", methods=["POST"])
def leave_edit(rid):
    emp = _my_employee()
    if not emp:
        abort(403)
    req = hrq.get_leave_request(rid)
    if not req or req["employee_id"] != emp["id"] or req["status"] != "pending":
        abort(403)
    data = {
        "employee_id": req["employee_id"],     # from the DB row — never the form
        "leave_type_id": request.form["leave_type_id"],
        "start_date": request.form["start_date"],
        "end_date": request.form["end_date"],
        "days": request.form.get("days", req["days"]),
        "reason": request.form.get("reason", req["reason"]),
        "has_medical_cert": req["has_medical_cert"],
        "status": "pending",                   # stays pending after a self-edit
    }
    hrq.update_leave_request(rid, data)
    conn = get_connection()
    conn.execute(
        "INSERT INTO audit_log(table_name, row_id, action, changed_fields, user)"
        " VALUES(?,?,?,?,?)",
        ("leave_requests", rid, "UPDATE",
         json.dumps({"edit": True}, ensure_ascii=False),
         session.get("username")),
    )
    conn.commit()
    conn.close()
    flash("แก้ไขคำขอลาแล้ว", "success")
    return redirect(url_for("me.leave"))


@bp_me.route("/payslip")
def payslip_list():
    emp = _my_employee()
    if not emp:
        flash("บัญชีนี้ไม่มีสลิปเงินเดือน", "info")
        return redirect(url_for("dashboard"))
    return render_template(
        "me/payslip_list.html",
        employee=emp,
        payslips=hrq.get_employee_payslips(emp["id"]),
    )


@bp_me.route("/payslip/<int:item_id>")
def payslip_detail(item_id):
    emp = _my_employee()
    if not emp:
        flash("บัญชีนี้ไม่มีสลิปเงินเดือน", "info")
        return redirect(url_for("dashboard"))
    item = hrq.get_payroll_item(item_id)
    # Ownership: item must belong to THIS employee (id from session — never the URL
    # beyond the lookup key). Visibility: run must be finalized (drafts are not
    # employee-visible; numbers can still change before finalize).
    if not item or item["employee_id"] != emp["id"]:
        abort(403)
    run = hrq.get_payroll_run(item["run_id"])
    if not run or run["status"] != "finalized":
        abort(403)
    from blueprints.hr import _be_year, _fmt_baht   # deferred: avoids import-cycle risk
    return render_template(
        "hr/payslip.html",
        run=run, item=item, employee=emp,
        be_year=_be_year, fmt_baht=_fmt_baht,
        back_url=url_for("me.payslip_list"), back_label="กลับ สลิปทั้งหมด",
    )


@bp_me.route("/leave/<int:rid>/cancel", methods=["POST"])
def leave_cancel(rid):
    emp = _my_employee()
    if not emp:
        abort(403)
    req = hrq.get_leave_request(rid)
    if not req or req["employee_id"] != emp["id"] or req["status"] != "pending":
        abort(403)
    data = {
        "employee_id": req["employee_id"],
        "leave_type_id": req["leave_type_id"],
        "start_date": req["start_date"],
        "end_date": req["end_date"],
        "days": req["days"],
        "reason": req["reason"],
        "has_medical_cert": req["has_medical_cert"],
        "status": "cancelled",
    }
    hrq.update_leave_request(rid, data)
    conn = get_connection()
    conn.execute(
        "INSERT INTO audit_log(table_name, row_id, action, changed_fields, user)"
        " VALUES(?,?,?,?,?)",
        ("leave_requests", rid, "UPDATE",
         json.dumps({"status": "cancelled"}, ensure_ascii=False),
         session.get("username")),
    )
    conn.commit()
    conn.close()
    flash("ยกเลิกคำขอลาแล้ว", "success")
    return redirect(url_for("me.leave"))


# ── Account settings + self-service change password ───────────────────────────
#
# Its own all-roles 'settings' module (nav.py + access_control._MODULE_DEFS), so
# EVERY logged-in role reaches this page. Identity comes from the session only —
# NOT _my_employee() — so admin/shareholder (who have no employee row) are
# included. Reset-when-locked-out is admin-driven via /users (the "ลืมรหัสผ่าน"
# note points there); this page is the everyday self-service path.

@bp_me.route("/account")
def account():
    return render_template(
        "me/account.html",
        # While impersonating, the current-password the form needs belongs to the
        # impersonated user (unknown to the real admin) — so hide the form.
        impersonating=bool(session.get("_real_role")),
    )


@bp_me.route("/change-password", methods=["POST"])
def change_password():
    # Never let an impersonating admin rewrite the impersonated user's credential.
    if session.get("_real_role"):
        flash("อยู่ในโหมดจำลองผู้ใช้ ไม่สามารถเปลี่ยนรหัสผ่านได้", "warning")
        return redirect(url_for("me.account"))

    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    conn = get_connection()
    user = conn.execute(
        "SELECT id, password_hash FROM users WHERE id=?", (session.get("user_id"),)
    ).fetchone()

    if not user or not check_password_hash(user["password_hash"], current):
        conn.close()
        flash("รหัสผ่านปัจจุบันไม่ถูกต้อง", "danger")
        return redirect(url_for("me.account"))
    if len(new) < 6:
        conn.close()
        flash("รหัสผ่านใหม่ต้องยาวอย่างน้อย 6 ตัวอักษร", "danger")
        return redirect(url_for("me.account"))
    if new != confirm:
        conn.close()
        flash("รหัสผ่านใหม่กับการยืนยันไม่ตรงกัน", "danger")
        return redirect(url_for("me.account"))

    # pbkdf2:sha256 — matches admin.py user create/edit (the local box has no scrypt).
    new_hash = generate_password_hash(new, method="pbkdf2:sha256")
    conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?", (new_hash, user["id"]),
    )
    conn.commit()
    conn.close()
    # Re-stamp THIS session so the changer stays logged in; every OTHER session
    # for this user now carries a stale pw_fp and is evicted by require_login.
    session["pw_fp"] = pw_fingerprint(new_hash)
    flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", "success")
    return redirect(url_for("me.account"))
