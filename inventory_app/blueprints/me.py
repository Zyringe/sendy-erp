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

from datetime import date

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)

import hr as hr_mod
import hr_queries as hrq

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
    hrq.create_leave_request(data, created_by=session.get("username"))
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
    flash("แก้ไขคำขอลาแล้ว", "success")
    return redirect(url_for("me.leave"))


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
    flash("ยกเลิกคำขอลาแล้ว", "success")
    return redirect(url_for("me.leave"))
