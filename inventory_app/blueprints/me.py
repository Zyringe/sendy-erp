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

from flask import Blueprint, flash, redirect, render_template, session, url_for

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
