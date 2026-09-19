"""Cashbook blueprint — รายรับ/รายจ่าย dashboard, ledger, manual entry.

Access control
--------------
  admin / manager / shareholder : full read + manual add/edit/delete
                                   (POST whitelisted in app.py; see
                                   `_MANAGER_POST_OK` + the shareholder
                                   POST set).
  staff   : blocked entirely — before_request redirects any cashbook.* endpoint.

Manual rows (payroll_item_id IS NULL) can be edited/deleted here. Salary
pay-event rows (payroll_item_id set, added by a later phase) are locked —
see `_reject_if_salary_row`.

Python 3.9 — no `X | None` union syntax.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Optional

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import access_control
import database
from paging import paging
import hr as hr_mod
import hr_queries as hrq

bp_cashbook = Blueprint("cashbook", __name__, url_prefix="/cashbook")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_baht(val) -> str:
    try:
        return f"฿{float(val):,.2f}"
    except (TypeError, ValueError):
        return "฿0.00"


# Categories that are capital / inter-account movements, NOT operating income or
# expense. Excluded from the headline P&L, category summary, monthly chart and the
# by-tag report. They are still real money (so they DO count toward account balance)
# and remain visible in the per-account ledger.
TRANSFER_CATEGORIES = ("เงินทุน/เงินโอน",)

# The one expense category whose rows are cashbook-SOURCED salary advances
# (plan.md decision C5): saving one writes back to HR salary_advances and links
# the two rows (salary_advance_id). Seeded by mig 128. A row in this category is
# treated as an advance iff it also resolves a valid employee — see
# `_resolve_advance_rows`. Excluded from overspend flags (advances are lumpy,
# finding #5); still counts in the P&L / category summary.
ADVANCE_CATEGORY = "เงินเดือน (เบิกล่วงหน้า)"

# Phase 3 (plan.md decisions C1-C3): the other two salary-family categories,
# each hard-blocked from MANUAL cashbook entry because they are sourced
# elsewhere and auto-post their own linked+locked row there:
#   - SALARY_CATEGORY: ALWAYS blocked — sourced in HR payroll (ADR 0006);
#     "จ่ายแล้ว" already auto-posts a locked row via hr.post_salary_payment.
#   - COMMISSION_CATEGORY: HYBRID blocked — only for an "in-engine" recipient
#     (see `_is_in_engine_commission_recipient`); an off-system rep (not in
#     `salespersons`) keeps the cashbook as its manual home (ADR 0008).
SALARY_CATEGORY = "เงินเดือน"
COMMISSION_CATEGORY = "จ่ายค่าคอมมิชชั่น"


def _tcat_ph():
    """Placeholder string + params for the transfer-category list."""
    return ",".join("?" * len(TRANSFER_CATEGORIES)), list(TRANSFER_CATEGORIES)


def _get_accounts_with_totals(conn, month: Optional[str] = None):
    """
    One dict per active account. `income`/`expense` are OPERATING totals (transfer
    categories excluded) so they sum to the headline P&L. `transfer_in`/`transfer_out`
    hold the excluded capital movements. `balance` is true cash = operating + transfers.

    `month` (optional, 'YYYY-MM') scopes every total to that calendar month.
    ⚠ This is a LEFT JOIN (idle accounts with zero txns must still appear), so the
    month filter is applied INSIDE each CASE WHEN, never in the WHERE clause — a
    WHERE-based filter would silently turn this into an inner join and drop any
    account with no activity that month.
    """
    ph, tcat_params = _tcat_ph()
    month_sql = " AND strftime('%Y-%m', t.txn_date) = ?" if month else ""
    clause_params = tcat_params + [month] if month else tcat_params
    count_sql = " AND strftime('%Y-%m', t.txn_date) = ?" if month else ""
    count_params = [month] if month else []

    rows = conn.execute(f"""
        SELECT
            a.id,
            a.code,
            a.display_name,
            a.account_owner_name,
            a.bank_name,
            a.bank_account_no,
            a.note AS account_note,
            a.is_transfer,
            a.income_recorded_elsewhere,
            MAX(t.txn_date) AS last_txn_date,
            COALESCE(SUM(CASE WHEN t.direction='income'  AND COALESCE(t.category,'') NOT IN ({ph}){month_sql} THEN t.amount ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN t.direction='expense' AND COALESCE(t.category,'') NOT IN ({ph}){month_sql} THEN t.amount ELSE 0 END), 0) AS expense,
            COALESCE(SUM(CASE WHEN t.direction='income'  AND COALESCE(t.category,'') IN ({ph}){month_sql} THEN t.amount ELSE 0 END), 0) AS transfer_in,
            COALESCE(SUM(CASE WHEN t.direction='expense' AND COALESCE(t.category,'') IN ({ph}){month_sql} THEN t.amount ELSE 0 END), 0) AS transfer_out,
            COUNT(CASE WHEN 1=1{count_sql} THEN t.id END) AS txn_count
        FROM cashbook_accounts a
        LEFT JOIN cashbook_transactions t ON t.account_id = a.id
        WHERE a.is_active = 1
        GROUP BY a.id
        ORDER BY a.is_transfer ASC, a.sort_order ASC, a.id ASC
    """, clause_params * 4 + count_params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["balance"] = (d["income"] + d["transfer_in"]) - (d["expense"] + d["transfer_out"])
        result.append(d)
    return result


def _get_operating_totals(conn, month: Optional[str] = None):
    """The headline รายรับ/รายจ่าย and the transfer-category figure disclosed
    beside them, over EVERY non-transfer account, active or not — the same
    population as `_get_category_summary`, so the cards and the breakdown under
    them cannot disagree (#594). A closed account's history still happened
    (ADR 0017); only the per-account table (`_get_accounts_with_totals`) is
    active-only, so the closed accounts that moved money in scope are returned
    by name for the page to disclose.

    `month` (optional, 'YYYY-MM') scopes to that calendar month."""
    ph, params = _tcat_ph()
    month_sql = ""
    month_params = []
    if month:
        month_sql = " AND strftime('%Y-%m', t.txn_date) = ?"
        month_params = [month]
    rows = conn.execute(f"""
        SELECT
            COALESCE(a.display_name, a.code) AS label,
            a.is_active,
            COALESCE(SUM(CASE WHEN t.direction='income'  AND COALESCE(t.category,'') NOT IN ({ph}) THEN t.amount END), 0) AS income,
            COALESCE(SUM(CASE WHEN t.direction='expense' AND COALESCE(t.category,'') NOT IN ({ph}) THEN t.amount END), 0) AS expense,
            COALESCE(SUM(CASE WHEN COALESCE(t.category,'') IN ({ph}) THEN t.amount END), 0) AS transfer
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0{month_sql}
        GROUP BY a.id
        ORDER BY a.sort_order, a.id
    """, params * 3 + month_params).fetchall()
    return {
        "income": sum(r["income"] for r in rows),
        "expense": sum(r["expense"] for r in rows),
        "transfer_total": sum(r["transfer"] for r in rows),
        "closed_accounts": [r["label"] for r in rows
                            if not r["is_active"] and (r["income"] or r["expense"])],
    }


def _get_monthly_summary(conn, exclude_transfer: bool = True):
    """Monthly operating income/expense (transfer categories always excluded;
    transfer accounts excluded when exclude_transfer)."""
    ph, params = _tcat_ph()
    acct_clause = "AND a.is_transfer = 0" if exclude_transfer else ""
    rows = conn.execute(f"""
        SELECT
            strftime('%Y-%m', t.txn_date) AS month,
            SUM(CASE WHEN t.direction='income'  THEN t.amount ELSE 0 END) AS income,
            SUM(CASE WHEN t.direction='expense' THEN t.amount ELSE 0 END) AS expense
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE COALESCE(t.category,'') NOT IN ({ph}) {acct_clause}
        GROUP BY month
        ORDER BY month ASC
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_category_summary(conn, month: Optional[str] = None):
    """Income and expense totals by category, excluding transfer accounts AND
    transfer categories. `month` (optional, 'YYYY-MM') scopes to that calendar
    month; `None` (default) = all-time, unchanged from the original behavior.
    Inner join, so a plain WHERE is correct here (categories absent that month
    are meant to drop out)."""
    ph, params = _tcat_ph()
    month_sql = ""
    if month:
        month_sql = " AND strftime('%Y-%m', t.txn_date) = ?"
        params = params + [month]
    rows = conn.execute(f"""
        SELECT
            t.direction,
            COALESCE(t.category, '(ไม่ระบุ)') AS category,
            SUM(t.amount) AS total
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0 AND COALESCE(t.category,'') NOT IN ({ph}){month_sql}
        GROUP BY t.direction, category
        ORDER BY t.direction DESC, total DESC
    """, params).fetchall()
    income_cats = [dict(r) for r in rows if r["direction"] == "income"]
    expense_cats = [dict(r) for r in rows if r["direction"] == "expense"]
    return income_cats, expense_cats


def _expense_topn(expense_cats, n=7):
    """Top-n expense categories by total (input already sorted desc); the rest
    folded into a single 'อื่นๆ' row. Grand total preserved. Pure/testable."""
    out = [{"category": c["category"], "total": c["total"]} for c in expense_cats[:n]]
    rest = expense_cats[n:]
    if rest:
        out.append({"category": "อื่นๆ", "total": sum(c["total"] for c in rest)})
    return out


def _get_tag_summary(conn, month: Optional[str] = None):
    """Operating EXPENSE grouped by ผู้ใช้ tag (user_category). Excludes transfer
    accounts, transfer categories and untagged rows. `month` (optional, 'YYYY-MM')
    scopes to that calendar month; `None` (default) = all-time, unchanged from
    the original behavior."""
    ph, params = _tcat_ph()
    month_sql = ""
    if month:
        month_sql = " AND strftime('%Y-%m', t.txn_date) = ?"
        params = params + [month]
    rows = conn.execute(f"""
        SELECT
            t.user_category AS tag,
            SUM(t.amount)    AS total,
            COUNT(*)         AS n
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0
          AND t.direction = 'expense'
          AND COALESCE(t.category,'') NOT IN ({ph})
          AND t.user_category IS NOT NULL AND t.user_category != ''{month_sql}
        GROUP BY t.user_category
        ORDER BY total DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


# ── Month-scope helpers (default month, overspend flags) ───────────────────────

# Overspend flag thresholds (decision 5, plan.md) — named constants, not inline
# magic numbers, so they're easy to tune later.
_OVERSPEND_PCT_THRESHOLD = 0.20     # this >= prev * 1.20
_OVERSPEND_DIFF_FLOOR = 1000.0      # AND (this - prev) >= ฿1,000

# The "show everything" token in `?month=`, shared by the dashboard and the
# account-ledger page (#524) so a link from one to the other means the same
# thing on both sides.
_ALL_TIME = "ทั้งหมด"


def _default_month(conn, account_id: Optional[int] = None) -> Optional[str]:
    """Most recent calendar month ('YYYY-MM') that has any cashbook transaction,
    or None if the ledger has no transactions at all (the dashboard falls back
    to all-time mode in that case). Used as the default when the dashboard
    route receives no `?month=` — NOT `strftime('now')`: entry lags, so the
    strict current month is often empty and would render a misleading ฿0 page.

    `account_id`, when given, scopes the same rule to one account's own rows
    (ticket #524's account-ledger page default) — a busy account can have a
    much older "latest month" than the ledger as a whole, and the reverse."""
    if account_id is None:
        row = conn.execute(
            "SELECT MAX(strftime('%Y-%m', txn_date)) AS m FROM cashbook_transactions"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(strftime('%Y-%m', txn_date)) AS m FROM cashbook_transactions"
            " WHERE account_id=?",
            (account_id,),
        ).fetchone()
    return row["m"] if row and row["m"] else None


def _resolve_month_scope(conn, raw_month: Optional[str], account_id: Optional[int] = None):
    """The `?month=` decision, shared by dashboard() and account_ledger()
    (#524) so the two copies of this logic can't drift apart:

      raw_month is None            -> the default: `_default_month`, scoped
                                       to `account_id` when given
      raw_month in ("", _ALL_TIME) -> all-time (month=None)
      raw_month == 'YYYY-MM'       -> that exact month, caller's own choice
                                       (even with zero rows in it)

    Returns (month, is_all_time, selected_month) — `month` is the value SQL
    filters on (None means no filter), `selected_month` is what the template
    renders (`_ALL_TIME` in place of None)."""
    if raw_month is None:
        month = _default_month(conn, account_id=account_id)
    elif raw_month in ("", _ALL_TIME):
        month = None
    else:
        month = raw_month
    is_all_time = month is None
    selected_month = _ALL_TIME if is_all_time else month
    return month, is_all_time, selected_month


def _prev_month(month: str) -> str:
    """'YYYY-MM' -> the previous calendar month's 'YYYY-MM'."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _month_day_count(month: str) -> int:
    """Number of calendar days in 'YYYY-MM'."""
    y, m = int(month[:4]), int(month[5:7])
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return (nxt - date(y, m, 1)).days


def _month_bounds(month: str, day_limit: Optional[int] = None):
    """(start_date, end_date) date-strings spanning 'YYYY-MM', inclusive.

    Without `day_limit`, spans the whole month. With `day_limit`, the end date
    is clipped to day 1..day_limit, CLAMPED to the month's actual length — e.g.
    `_month_bounds('2026-02', 31)` clips to '2026-02-28' (February has no 31st),
    not a nonexistent '2026-02-31'. This is the MTD prev-month clamp (plan.md
    "Partial month MTD": today Mar 31 -> prev Feb clip = Feb 1..28)."""
    days_in_month = _month_day_count(month)
    d = min(day_limit, days_in_month) if day_limit else days_in_month
    return f"{month}-01", f"{month}-{d:02d}"


def _expense_by_category_range(conn, start_date: str, end_date: str):
    """{category: total} of OPERATING expense within [start_date, end_date]
    inclusive (txn_date is a zero-padded 'YYYY-MM-DD' string, so lexical
    comparison matches calendar order). Same operating scope as
    `_get_category_summary`: transfer accounts + transfer category excluded."""
    ph, params = _tcat_ph()
    rows = conn.execute(f"""
        SELECT COALESCE(t.category, '(ไม่ระบุ)') AS category, SUM(t.amount) AS total
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0
          AND t.direction = 'expense'
          AND COALESCE(t.category,'') NOT IN ({ph})
          AND t.txn_date >= ? AND t.txn_date <= ?
        GROUP BY category
    """, params + [start_date, end_date]).fetchall()
    return {r["category"]: r["total"] for r in rows}


def _overspend_flags(conn, month: str, today: Optional[date] = None):
    """Per-category expense overspend flags for `month`, vs the previous
    calendar month (decision 4+5, plan.md). One dict per operating expense
    category PRESENT in `month`:

        {category, this, prev, pct, diff, flagged, is_new}

    - `flagged`: prev > 0 AND this >= prev * (1+_OVERSPEND_PCT_THRESHOLD)
                 AND (this - prev) >= _OVERSPEND_DIFF_FLOOR
    - `is_new` (prev == 0, category absent/zero last month): never flagged.
    - MTD rule: if `month` is the CURRENT calendar month (derived from `today`,
      default `date.today()` — inject for deterministic tests), both `this` and
      `prev` are clipped to day 1..D (D = today's day-of-month), with the prev
      side clamped to the previous month's length (see `_month_bounds`). A
      fully-past month compares full-month totals on both sides.

    Pure/unit-testable aside from the two SELECTs (conn is the only I/O).
    """
    if today is None:
        today = date.today()
    current_month = today.strftime("%Y-%m")
    prev_month = _prev_month(month)

    day_limit = today.day if month == current_month else None
    this_start, this_end = _month_bounds(month, day_limit)
    prev_start, prev_end = _month_bounds(prev_month, day_limit)

    this_map = _expense_by_category_range(conn, this_start, this_end)
    prev_map = _expense_by_category_range(conn, prev_start, prev_end)

    out = []
    for cat in sorted(this_map.keys()):
        if cat == ADVANCE_CATEGORY:
            continue  # advances are lumpy — never an operating overspend (finding #5)
        this_v = this_map[cat]
        prev_v = prev_map.get(cat, 0.0)
        diff = this_v - prev_v
        is_new = prev_v == 0
        pct = None if prev_v == 0 else (diff / prev_v) * 100.0
        flagged = (
            not is_new
            and this_v >= prev_v * (1 + _OVERSPEND_PCT_THRESHOLD)
            and diff >= _OVERSPEND_DIFF_FLOOR
        )
        out.append({
            "category": cat, "this": this_v, "prev": prev_v,
            "pct": pct, "diff": diff, "flagged": flagged, "is_new": is_new,
        })
    return out


# ── Drill-down detail ──────────────────────────────────────────────────────────

_DETAIL_DIMS = ("income_category", "expense_category", "user_tag", "month")


def _get_detail_rows(conn, dim, key):
    """Transactions behind a dashboard summary figure, in the SAME operating
    scope as the dashboard (transfer accounts + transfer categories excluded).

    Returns (rows, summary). Raises ValueError on an unknown dim.
    """
    if dim not in _DETAIL_DIMS:
        raise ValueError(f"unknown detail dim: {dim!r}")

    ph, params = _tcat_ph()
    if dim == "income_category":
        where = " AND t.direction='income' AND COALESCE(t.category,'(ไม่ระบุ)') = ?"
        params = params + [key]
    elif dim == "expense_category":
        where = " AND t.direction='expense' AND COALESCE(t.category,'(ไม่ระบุ)') = ?"
        params = params + [key]
    elif dim == "user_tag":
        where = " AND t.direction='expense' AND t.user_category = ?"
        params = params + [key]
    else:  # month
        where = " AND strftime('%Y-%m', t.txn_date) = ?"
        params = params + [key]

    sql_rows = conn.execute(f"""
        SELECT t.txn_date, a.code AS account_code, a.account_owner_name,
               t.direction, t.category, t.user_category, t.amount, t.note
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0
          AND COALESCE(t.category,'') NOT IN ({ph})
          {where}
        ORDER BY t.txn_date DESC, t.id DESC
    """, params).fetchall()

    rows = []
    for r in sql_rows:
        d = dict(r)
        d["amount_display"] = _fmt_baht(d["amount"])
        rows.append(d)

    if dim == "month":
        income = sum(r["amount"] for r in rows if r["direction"] == "income")
        expense = sum(r["amount"] for r in rows if r["direction"] == "expense")
        summary = {
            "count": len(rows),
            "income": income, "income_display": _fmt_baht(income),
            "expense": expense, "expense_display": _fmt_baht(expense),
        }
    else:
        total = sum(r["amount"] for r in rows)
        summary = {"count": len(rows), "total": total, "total_display": _fmt_baht(total)}

    return rows, summary


# ── Routes ────────────────────────────────────────────────────────────────────

@bp_cashbook.route("/")
def dashboard():
    conn = database.get_connection()
    try:
        # Month-scope resolution (decision 2+6, plan.md; shared with
        # account_ledger() via _resolve_month_scope, #524):
        #   absent ?month=      -> resolve to the most-recent month with data
        #   ?month=  or ทั้งหมด  -> all-time (month=None)
        #   ?month=YYYY-MM      -> that month
        month, is_all_time, selected_month = _resolve_month_scope(
            conn, request.args.get("month")
        )

        accounts      = _get_accounts_with_totals(conn, month)
        totals        = _get_operating_totals(conn, month)
        monthly       = _get_monthly_summary(conn, exclude_transfer=True)  # never scoped (trend chart)
        income_cats, expense_cats = _get_category_summary(conn, month)
        tag_summary   = _get_tag_summary(conn, month)

        today = date.today()
        is_current_month = (not is_all_time) and month == today.strftime("%Y-%m")
        overspend = _overspend_flags(conn, month, today=today) if not is_all_time else []

        available_months = [
            r["m"] for r in conn.execute(
                """SELECT DISTINCT strftime('%Y-%m', txn_date) AS m
                   FROM cashbook_transactions
                   ORDER BY m DESC"""
            ).fetchall()
        ]
    finally:
        conn.close()

    # Headline P&L excludes transfer accounts AND transfer categories. It reads
    # every non-transfer account, closed ones included — the same population
    # as the category summary under it (#594); the per-account table below it
    # stays active-only.
    op_accounts = [a for a in accounts if not a["is_transfer"]]
    tr_accounts  = [a for a in accounts if a["is_transfer"]]

    total_income  = totals["income"]
    total_expense = totals["expense"]
    # #534: an account flagged income_recorded_elsewhere (e.g. ชฎามาศ) never
    # has its income keyed here, so its all-history "balance" is a meaningless
    # negative (−฿1.74M on prod for ชฎามาศ) — excluded from the คงเหลือ
    # headline ONLY. Its income/expense stay summed into total_income/
    # total_expense above (unchanged) — the issue's "expenses still count
    # everywhere" — this exclusion touches nothing but the balance sum below.
    balance_accounts = [a for a in op_accounts if not a["income_recorded_elsewhere"]]
    flagged_accounts = [a for a in op_accounts if a["income_recorded_elsewhere"]]
    # คงเหลือ = actual cash on hand = sum of true-cash account balances (which include
    # capital transfers). This reconciles with the per-account balance column. It is
    # deliberately NOT income − expense: transfers fund the gap (see disclosure note).
    total_balance = sum(a["balance"] for a in balance_accounts)
    # Capital/inter-account movements excluded from the P&L (disclosure figure),
    # over the same population as total_income/total_expense.
    transfer_total = totals["transfer_total"]

    # Card 3 (decision 3, plan.md "Card-3 semantics"): meaning changes by mode.
    #   Month mode : สุทธิเดือนนี้ = income − expense (operating P&L net for the
    #                month). Must NOT reuse total_balance — that folds in transfers.
    #   All-time   : คงเหลือ = true cash on hand (unchanged from today).
    if is_all_time:
        card3_value = total_balance
        card3_is_net = False
    else:
        card3_value = total_income - total_expense
        card3_is_net = True

    return render_template(
        "cashbook/dashboard.html",
        accounts=accounts,
        op_accounts=op_accounts,
        tr_accounts=tr_accounts,
        total_income=total_income,
        total_expense=total_expense,
        total_balance=total_balance,
        flagged_accounts=flagged_accounts,
        closed_accounts=totals["closed_accounts"],
        transfer_total=transfer_total,
        monthly=monthly,
        income_cats=income_cats,
        expense_cats=expense_cats,
        expense_chart=_expense_topn(expense_cats),
        tag_summary=tag_summary,
        selected_month=selected_month,
        is_all_time=is_all_time,
        is_current_month=is_current_month,
        card3_value=card3_value,
        card3_is_net=card3_is_net,
        overspend=overspend,
        available_months=available_months,
    )


@bp_cashbook.route("/api/detail")
def detail_api():
    dim = request.args.get("dim", "")
    key = request.args.get("key", "")
    if dim not in _DETAIL_DIMS or key == "":
        abort(400)
    conn = database.get_connection()
    try:
        rows, summary = _get_detail_rows(conn, dim, key)
    finally:
        conn.close()
    return jsonify(rows=rows, summary=summary, dim=dim, key=key)


@bp_cashbook.route("/advance-history/<int:employee_id>")
def advance_history(employee_id):
    """Read-only JSON for the advance "ดูประวัติ" modal (plan.md C6): an
    employee's advances in `month` (?month=YYYY-MM, default this month), their
    TOTAL still-outstanding advances (not-yet-deducted, across all months — the
    "don't over-advance" figure), and that month's net salary if a run exists."""
    month = request.args.get("month", "").strip() or date.today().strftime("%Y-%m")
    conn = database.get_connection()
    try:
        emp = conn.execute(
            "SELECT id, COALESCE(nickname, full_name) AS name FROM employees WHERE id=?",
            (employee_id,),
        ).fetchone()
        if emp is None:
            abort(404)
        adv_rows = conn.execute(
            """SELECT id, advance_date, amount, deducted_in_run_id
                 FROM salary_advances
                WHERE employee_id=? AND strftime('%Y-%m', advance_date)=?
                ORDER BY advance_date, id""",
            (employee_id, month),
        ).fetchall()
        advances = [
            {"id": r["id"], "advance_date": r["advance_date"], "amount": r["amount"],
             "deducted": r["deducted_in_run_id"] is not None}
            for r in adv_rows
        ]
        month_total = sum(r["amount"] for r in adv_rows)
        outstanding_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM salary_advances"
            " WHERE employee_id=? AND deducted_in_run_id IS NULL",
            (employee_id,),
        ).fetchone()[0]
        net_row = conn.execute(
            """SELECT COALESCE(SUM(pi.net_pay),0) AS np, COUNT(*) AS n
                 FROM payroll_items pi JOIN payroll_runs pr ON pr.id = pi.run_id
                WHERE pi.employee_id=? AND pr.year_month=?""",
            (employee_id, month),
        ).fetchone()
        net_pay = net_row["np"] if net_row["n"] else None

        # Advance cap warning (plan.md P2 step 2): the SAME collectable_this_
        # month ceiling the server-side guard checks against, surfaced here
        # so the "ดูประวัติ" modal can show a yellow/red advisory before
        # submit. Advisory ONLY — this JSON read is on a separate, already-
        # closed connection by the time the form is submitted, so it can be
        # stale; the real guard is the server-side check in new_transaction.
        cfg = hr_mod._load_config(conn)
        cap_status = hr_mod.collectable_this_month(conn, employee_id, month, cfg=cfg)
        target_finalized = conn.execute(
            """SELECT 1 FROM payroll_runs
                WHERE year_month=? AND company_id=(SELECT company_id FROM employees WHERE id=?)
                  AND status='finalized'
                LIMIT 1""",
            (month, employee_id),
        ).fetchone() is not None
    finally:
        conn.close()
    return jsonify(
        employee={"id": emp["id"], "name": emp["name"]},
        month=month,
        advances=advances,
        month_total=month_total,
        outstanding_total=outstanding_total,
        net_pay=net_pay,
        salary_rate=cap_status["salary_rate"] if cap_status else None,
        base_amount=cap_status["base_amount"] if cap_status else None,
        warn_pct=cfg["advance_warn_pct"],
        month_advance_total=month_total,
        collectable=cap_status["collectable"] if cap_status else None,
        target_month=month,
        target_month_finalized=target_finalized,
    )


@bp_cashbook.route("/account/<int:account_id>")
def account_ledger(account_id):
    conn = database.get_connection()
    try:
        acct = conn.execute(
            "SELECT * FROM cashbook_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if acct is None:
            flash("ไม่พบบัญชีนี้ในระบบ", "danger")
            return redirect(url_for("cashbook.dashboard"))

        # Month-scope resolution (ticket #524 — ADR 0007's rule, scoped to
        # this one account; shared with dashboard() via _resolve_month_scope
        # so the two can't drift):
        #   absent ?month=      -> most-recent month THIS account has data in
        #                          (None if the account has none at all)
        #   ?month=ทั้งหมด or "" -> all history (reuses the dashboard's own
        #                          all-time token, so both pages speak the
        #                          same `month` values)
        #   ?month=YYYY-MM      -> that exact month, even with zero rows — the
        #                          caller (a dashboard link, or a redirect
        #                          after add/edit/delete) picked it on purpose.
        month, is_all_time, selected_month = _resolve_month_scope(
            conn, request.args.get("month"), account_id=account_id
        )

        dir_filter = request.args.get("dir", "").strip()
        page, per_page = paging(request.args, per_page=50)

        params = [account_id]
        where  = ["t.account_id=?"]

        if month:
            where.append("strftime('%Y-%m', t.txn_date)=?")
            params.append(month)
        if dir_filter in ("income", "expense"):
            where.append("t.direction=?")
            params.append(dir_filter)

        where_sql = " AND ".join(where)
        total_count = conn.execute(
            f"SELECT COUNT(*) FROM cashbook_transactions t WHERE {where_sql}",
            params,
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(
            f"""SELECT t.* FROM cashbook_transactions t
                WHERE {where_sql}
                ORDER BY t.txn_date ASC, t.id ASC
                LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()

        # Running totals for the displayed (filtered) page — "รายรับ/รายจ่าย
        # (ที่กรอง)" cards, meant to reflect whatever รายรับ/รายจ่าย filter is
        # active (so filtering to รายจ่าย correctly shows ฿0.00 รายรับ).
        sum_income  = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM cashbook_transactions t "
            f"WHERE {where_sql} AND direction='income'",
            params,
        ).fetchone()[0]
        sum_expense = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM cashbook_transactions t "
            f"WHERE {where_sql} AND direction='expense'",
            params,
        ).fetchone()[0]

        # Card 3 (คงเหลือ / เข้า-ออกสุทธิ, #524) is the account's real net
        # movement and must NOT be narrowed by the รายรับ/รายจ่าย filter —
        # it needs its own account+month-only WHERE, never `where_sql`
        # (which already carries `direction=?` when dir_filter is set; on
        # top of `AND direction='expense'` that becomes a self-contradicting
        # `direction='income' AND direction='expense'`, silently zeroing the
        # other side and corrupting the net figure).
        balance_where = ["t.account_id=?"]
        balance_params = [account_id]
        if month:
            balance_where.append("strftime('%Y-%m', t.txn_date)=?")
            balance_params.append(month)
        balance_where_sql = " AND ".join(balance_where)
        balance_income = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM cashbook_transactions t "
            f"WHERE {balance_where_sql} AND direction='income'",
            balance_params,
        ).fetchone()[0]
        balance_expense = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM cashbook_transactions t "
            f"WHERE {balance_where_sql} AND direction='expense'",
            balance_params,
        ).fetchone()[0]

        # Available months for filter dropdown — always includes the
        # RESOLVED month even when this account has zero rows in it (ticket
        # #524: a dashboard link or a redirect can land here on a month this
        # account never touched, and the dropdown must still show it as
        # selected rather than silently reverting to "ทุกเดือน"). `r["m"]`
        # can be SQL NULL (a txn_date strftime can't parse) — drop it, or
        # `sorted()` crashes mixing None with strings.
        months = {
            r["m"] for r in conn.execute(
                """SELECT DISTINCT strftime('%Y-%m', txn_date) AS m
                   FROM cashbook_transactions
                   WHERE account_id=?""",
                (account_id,),
            ).fetchall()
            if r["m"] is not None
        }
        if month:
            months.add(month)
        months = sorted(months)

        # For the per-row edit modals (manual rows only — see txn_edit.html).
        accounts = hrq.get_active_cashbook_accounts(conn)
        categories_by_direction = _categories_by_direction(conn)
        known_tags = _get_known_user_tags(conn)
    finally:
        conn.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)

    # Whether the CURRENT session's role could go cancel a commission-linked
    # row at /commission — same rule that gates commission.commission_delete_payout
    # (access_control.role_can_post), never a hand-typed role tuple (#542).
    # Drives the lock tooltip's wording on a commission-linked row.
    can_cancel_commission_payout = access_control.role_can_post(
        session.get('role', ''), 'commission.commission_delete_payout')

    return render_template(
        "cashbook/account_ledger.html",
        acct=dict(acct),
        rows=[dict(r) for r in rows],
        selected_month=selected_month,
        is_all_time=is_all_time,
        dir_filter=dir_filter,
        page=page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
        sum_income=sum_income,
        sum_expense=sum_expense,
        balance=balance_income - balance_expense,
        months=months,
        accounts=accounts,
        categories_by_direction=categories_by_direction,
        known_tags=known_tags,
        can_cancel_commission_payout=can_cancel_commission_payout,
    )


# ── Manual entry (batch add / edit / delete) ──────────────────────────────────
#
# Write access is gated in app.py: `_MANAGER_POST_OK` and the shareholder POST
# set both whitelist `cashbook.new_transaction` / `.txn_edit` / `.txn_delete`.
# Staff is blocked entirely by the before_request cashbook.* check above.

_ROW_AMOUNT_RE = re.compile(r"^rows-(\d+)-amount$")


def _categories_by_direction(conn):
    """{'income': [name, ...], 'expense': [name, ...]} for the category
    <datalist>s, active categories only."""
    rows = conn.execute(
        """SELECT name, direction FROM cashbook_categories
            WHERE is_active = 1
            ORDER BY direction, sort_order, name"""
    ).fetchall()
    out = {"income": [], "expense": []}
    for r in rows:
        out.setdefault(r["direction"], []).append(r["name"])
    return out


def _get_known_user_tags(conn):
    """ผู้ใช้ tag suggestions for the user_category <datalist>: every
    employee's SYSTEM name (nickname, or full_name when blank) first — issue
    #532 §2, so a keyer sees and picks the name the system itself posts —
    then every other tag already used on a transaction, in existing order.
    Deduped; there is no separate tags table, user_category is free text.
    `COALESCE(nickname, full_name)` (not NULLIF-blank-aware) matches
    `_resolve_advance_rows`'s existing convention below — one rule for what
    "the employee's display name" means, not two slightly different ones."""
    emp_rows = conn.execute(
        "SELECT COALESCE(nickname, full_name) AS name FROM employees ORDER BY name"
    ).fetchall()
    seen = set()
    tags = []
    for r in emp_rows:
        if r["name"] and r["name"] not in seen:
            seen.add(r["name"])
            tags.append(r["name"])
    other_rows = conn.execute(
        """SELECT DISTINCT user_category FROM cashbook_transactions
            WHERE user_category IS NOT NULL AND user_category != ''
            ORDER BY user_category"""
    ).fetchall()
    for r in other_rows:
        if r["user_category"] not in seen:
            seen.add(r["user_category"])
            tags.append(r["user_category"])
    return tags


def _match_employee_tag(employees, typed):
    """Pure matching logic (no DB access) against a PRE-FETCHED list of
    employee rows (each carrying full_name/nickname) — the actual rule
    behind `_resolve_employee_tag`. Split out so a batch caller
    (`_resolve_person_tags`) can fetch `employees` ONCE per request instead
    of once per row (an N+1 re-fetch found in code review).

    `typed` unambiguously names exactly one employee by their full name OR
    the first word of it (issue #532 §2 — Put's rule: a person's tag is the
    name the system itself posts).

    Returns (resolved, notice): `resolved` is `typed` unchanged when there is
    no match, an AMBIGUOUS match (two+ employees share the same first word),
    or the system name already equals what was typed; `notice` is a Thai
    flash message (or None) naming the change. Matches against ALL employees,
    not just active ones — a manual row can legitimately name someone who has
    since left. Salesperson real-name aliases (off-system reps like บ่าว/แต/
    อัคเรศ included) are a separate, out-of-scope concept (ADR 0008) — never
    matched here; an employee record that happens to collide with one of
    those alias strings would still resolve (accepted residual risk, no
    off-system-alias registry exists to check against)."""
    typed = (typed or "").strip()
    if not typed:
        return typed, None
    matches = []
    for r in employees:
        full = r["full_name"] or ""
        first = full.split()[0] if full.split() else full
        if typed == full or typed == first:
            matches.append(r)
    if len(matches) != 1:
        return typed, None
    system_name = matches[0]["nickname"] or matches[0]["full_name"]
    if system_name == typed:
        return typed, None
    return system_name, f"เปลี่ยนป้าย '{typed}' เป็น '{system_name}' ตามชื่อเล่นใน HR"


def _resolve_employee_tag(conn, typed):
    """Single-tag convenience wrapper around `_match_employee_tag` — fetches
    `employees` fresh on every call. Fine for `txn_edit`, which resolves at
    most one tag per request; a batch caller fetches once and calls
    `_match_employee_tag` directly (see `_resolve_person_tags`)."""
    rows = conn.execute("SELECT full_name, nickname FROM employees").fetchall()
    return _match_employee_tag(rows, typed)


def _resolve_person_tags(conn, to_insert):
    """Apply `_match_employee_tag` to every NON-advance row's user_category
    in `to_insert`, mutating it in place (advance rows already carry the
    employee's system name from `_resolve_advance_rows`). Fetches `employees`
    ONCE for the whole batch. Returns the list of distinct Thai notices to
    flash, in first-seen order.

    ⚠ Caller must run this AFTER `_apply_policy_blocks` — the in-engine
    commission double-book guard (`_is_in_engine_commission_recipient`) must
    see the RAW typed tag, not an already-resolved employee nickname, or an
    employee whose name happens to collide with a salesperson's real-name
    alias would silently defeat the guard (issue #532 review)."""
    employees = conn.execute("SELECT full_name, nickname FROM employees").fetchall()
    notices = []
    seen = set()
    for item in to_insert:
        if item.get("is_advance"):
            continue
        resolved, notice = _match_employee_tag(employees, item.get("user_category") or "")
        item["user_category"] = resolved
        if notice and notice not in seen:
            seen.add(notice)
            notices.append(notice)
    return notices


def _category_lookup(conn):
    """{(name, direction): is_active} for every row in `cashbook_categories`
    — fetched ONCE per request and shared by `_reject_inactive_categories`
    and `_find_new_categories` (issue #532 review: avoids a per-row/per-pair
    re-query of this small table)."""
    rows = conn.execute("SELECT name, direction, is_active FROM cashbook_categories").fetchall()
    return {(r["name"], r["direction"]): r["is_active"] for r in rows}


def _find_new_categories(cat_lookup, to_insert):
    """Distinct (name, direction) pairs among `to_insert` absent from
    `cat_lookup` — these need explicit confirmation before they're created
    (issue #532 §1). Returns a list of {"name", "direction"} dicts,
    first-seen order."""
    new_cats = []
    checked = set()
    for item in to_insert:
        key = (item["category"], item["direction"])
        if key in checked:
            continue
        checked.add(key)
        if key not in cat_lookup:
            new_cats.append({"name": key[0], "direction": key[1]})
    return new_cats


def _reject_inactive_categories(cat_lookup, rows, to_insert):
    """Append a row error for any `to_insert` row whose category EXISTS in
    `cat_lookup` but is inactive (retired) — refused, never silently saved
    (issue #532 §1). Mutates `rows` in place; the caller's existing
    `row_errors` check (any row now carrying an error) blocks the whole
    submission, the same as every other basic-validation failure."""
    rows_by_index = {r["index"]: r for r in rows}
    for item in to_insert:
        is_active = cat_lookup.get((item["category"], item["direction"]))
        if is_active == 0:
            rows_by_index[item["index"]]["errors"].append(
                "หมวดหมู่นี้ถูกปิดใช้งานแล้ว กรุณาเลือกหมวดหมู่ที่ใช้งานอยู่"
            )


def _blank_rows(n=1):
    return [
        {"index": i, "direction": "expense", "category": "", "user_category": "",
         "employee_id": "", "amount": "", "description": "", "note": "",
         "txn_date": "", "errors": []}
        for i in range(n)
    ]


def _parse_batch_rows(form):
    """Parse indexed `rows-<i>-*` fields from a submitted batch form into an
    ordered list of dicts (one per submitted row slot). Purely mechanical —
    validation happens in the caller."""
    indices = sorted(int(m.group(1)) for k in form.keys()
                      for m in [_ROW_AMOUNT_RE.match(k)] if m)
    rows = []
    for i in indices:
        rows.append({
            "index": i,
            "direction": form.get(f"rows-{i}-direction", "expense").strip(),
            "category": form.get(f"rows-{i}-category", "").strip(),
            "user_category": form.get(f"rows-{i}-user_category", "").strip(),
            "employee_id": form.get(f"rows-{i}-employee_id", "").strip(),
            "amount": form.get(f"rows-{i}-amount", "").strip(),
            "description": form.get(f"rows-{i}-description", "").strip(),
            "note": form.get(f"rows-{i}-note", "").strip(),
            "txn_date": form.get(f"rows-{i}-txn_date", "").strip(),
            "errors": [],
        })
    return rows


def _resolve_advance_rows(conn, rows, to_insert):
    """Enrich every advance-category row in `to_insert` in place and validate
    its employee (plan.md decision C5). An advance row (category ==
    ADVANCE_CATEGORY) requires a valid active employee: on success the row is
    marked `is_advance`, its `employee_id` set, `direction` forced to 'expense',
    and `user_category` overridden with the employee's display name (nickname or
    full_name — the auto-filled ผู้ใช้ tag). An invalid/missing employee appends
    an error to the ORIGINAL `rows` entry (so the form re-renders it) and the row
    is dropped from the returned insert list. Non-advance rows pass through
    untouched. Returns the filtered insert list."""
    rows_by_index = {r["index"]: r for r in rows}
    kept = []
    for item in to_insert:
        if item["category"] != ADVANCE_CATEGORY:
            kept.append(item)
            continue
        emp_id_raw = str(item.get("employee_id") or "").strip()
        emp = None
        if emp_id_raw.isdigit():
            emp = conn.execute(
                "SELECT id, COALESCE(nickname, full_name) AS display"
                "  FROM employees WHERE id=? AND is_active=1",
                (int(emp_id_raw),),
            ).fetchone()
        if emp is None:
            rows_by_index[item["index"]]["errors"].append(
                "กรุณาเลือกพนักงานสำหรับรายการเบิกล่วงหน้า"
            )
            continue
        item["is_advance"] = True
        item["employee_id"] = emp["id"]
        item["direction"] = "expense"
        item["user_category"] = emp["display"]
        kept.append(item)
    return kept


def _salespersons_with_real_name(conn, where_sql):
    """SELECT code, name, real_name FROM salespersons {where_sql}, tolerating
    a DB that predates mig 129 (no real_name column yet — e.g. a `tmp_db`
    clone of a live DB not yet migrated). Mirrors `_advance_link_id`'s
    pre-mig fallback: absent column == every real_name is NULL, the correct
    "no aliases known yet" default."""
    try:
        return conn.execute(
            f"SELECT code, name, real_name FROM salespersons {where_sql}"
        ).fetchall()
    except sqlite3.OperationalError:
        return conn.execute(
            f"SELECT code, name, NULL AS real_name FROM salespersons {where_sql}"
        ).fetchall()


def _is_in_engine_commission_recipient(conn, recipient):
    """True if `recipient` (a ผู้ใช้ tag, trimmed) matches an in-engine
    salesperson's code, name, or real_name alias (plan.md D3 gate — the
    hard-confirmed double-book risk: เจียรนัย=ต๋อ/06(-L), ทวีเกียรติ=ท/03).
    Active salespersons are the practical in-engine set. Off-system reps
    (อัคเรศ, แต, บ่าว, ...) match nothing here and stay manual (hybrid)."""
    recipient = (recipient or "").strip()
    if not recipient:
        return False
    rows = _salespersons_with_real_name(conn, "WHERE is_active = 1")
    for r in rows:
        idents = {r["code"], r["name"]}
        if r["real_name"]:
            idents.add(r["real_name"])
        if recipient in idents:
            return True
    return False


def _policy_blocked_reason(conn, item):
    """Thai block-reason string for `item` (a `to_insert` row dict), or None
    if it's allowed. ONE "linked & locked" concept covering both hard-blocked
    families (finding #6) — salary is unconditional; commission is hybrid
    (only an in-engine recipient blocks)."""
    if item["category"] == SALARY_CATEGORY:
        return "เงินเดือนบันทึกที่หน้าเงินเดือน (HR) เท่านั้น"
    if item["category"] == COMMISSION_CATEGORY and _is_in_engine_commission_recipient(
        conn, item.get("user_category") or ""
    ):
        return "คอมมิชชั่นของเซลส์ในระบบบันทึกที่หน้าคอมมิชชั่นเท่านั้น"
    return None


def _apply_policy_blocks(conn, rows, to_insert):
    """Remove hard-blocked rows (manual เงินเดือน; in-engine จ่ายค่าคอมมิชชั่น)
    from `to_insert` (plan.md decisions C1-C3, D1, findings #1/#3/#6). Each
    blocked row's reason is appended to the ORIGINAL `rows` entry (so a
    re-render highlights it) and the row is reported back separately so the
    caller can summarize it (bulk: skip + flash summary, still save the rest;
    single / all-blocked: nothing to save, re-render like a validation error).
    Off-system commission rows and every other category pass through
    untouched. Returns (kept, blocked) where `blocked` is a list of
    {index, category, reason}."""
    rows_by_index = {r["index"]: r for r in rows}
    kept = []
    blocked = []
    for item in to_insert:
        reason = _policy_blocked_reason(conn, item)
        if reason is None:
            kept.append(item)
            continue
        rows_by_index[item["index"]]["errors"].append(reason)
        blocked.append({"index": item["index"], "category": item["category"], "reason": reason})
    return kept, blocked


def _commission_reps_for_picker(conn):
    """Active salespersons for the commission recipient picker (plan.md C7):
    each carries its real_name alias (if any, seeded by mig 129's D3 gate) so
    Put recognizes an in-engine rep instead of typing them as free-text
    off-system — which would double-book against the /commission auto-post.
    `value` is what actually gets submitted as the ผู้ใช้ tag on selection, so
    it MUST be one of the identifiers `_is_in_engine_commission_recipient`
    matches against (the real_name alias if set, else the salesperson's own
    `name`); `label` is the human-readable alias shown in the dropdown.
    `code`/`name`/`real_name` are also returned (raw) so the template's JS can
    build the SAME in-engine identifier set the server checks, for a live
    redirect-button hint without a round-trip."""
    rows = _salespersons_with_real_name(conn, "WHERE is_active=1 ORDER BY code")
    out = []
    for r in rows:
        value = r["real_name"] or r["name"]
        label = f'{r["real_name"]} ({r["name"]})' if r["real_name"] else r["name"]
        out.append({"code": r["code"], "name": r["name"], "real_name": r["real_name"],
                    "value": value, "label": label})
    return out


def _validate_batch(rows, default_date):
    """Validate non-blank rows in place (sets row['errors']); blank rows
    (amount empty) are left untouched — they're skipped, not errors.

    Each row's EFFECTIVE date (decision B2, plan.md) is its own `txn_date`
    if set (bulk mode), else `default_date` (the shared top-of-form date —
    single mode, or any row left blank in bulk mode). Returns the list of
    rows that are valid AND non-blank, each with a parsed float `amount`
    and a validated `effective_date`.
    """
    to_insert = []
    for r in rows:
        if not r["amount"]:
            continue  # blank row — silently skipped
        errors = []
        try:
            amount = float(r["amount"])
        except ValueError:
            amount = None
        if amount is None or amount <= 0:
            errors.append("จำนวนเงินต้องมากกว่า 0")
        if r["direction"] not in ("income", "expense"):
            errors.append("ประเภทไม่ถูกต้อง")
        if not r["category"]:
            errors.append("กรุณาระบุหมวดหมู่")
        effective_date = r["txn_date"] or default_date
        if not effective_date:
            errors.append("กรุณาระบุวันที่")
        else:
            try:
                date.fromisoformat(effective_date)
            except ValueError:
                errors.append("รูปแบบวันที่ไม่ถูกต้อง")
        if errors:
            r["errors"] = errors
        else:
            to_insert.append({**r, "amount": amount, "effective_date": effective_date})
    return to_insert


def _find_duplicate_indices(conn, account_id, to_insert):
    """Row `index` values in `to_insert` that exactly match — on (account_id,
    effective date, direction, category, user_category, amount) — either an
    already-saved `cashbook_transactions` row or another row in the SAME
    submitted batch (decision D2, plan.md). Both rows of an in-batch
    duplicate pair are flagged, not just the second one."""
    dup_indices = set()
    seen_in_batch = {}
    for r in to_insert:
        key = (r["effective_date"], r["direction"], r["category"],
               r["user_category"] or "", r["amount"])
        existing = conn.execute(
            """SELECT 1 FROM cashbook_transactions
               WHERE account_id=? AND txn_date=? AND direction=? AND category=?
                 AND COALESCE(user_category,'')=? AND amount=?
               LIMIT 1""",
            (account_id,) + key,
        ).fetchone()
        if existing:
            dup_indices.add(r["index"])
        if key in seen_in_batch:
            dup_indices.add(r["index"])
            dup_indices.add(seen_in_batch[key])
        else:
            seen_in_batch[key] = r["index"]
    return dup_indices


def _upsert_category(conn, name, direction):
    conn.execute(
        "INSERT OR IGNORE INTO cashbook_categories(name,direction,source) VALUES(?,?,NULL)",
        (name, direction),
    )


def _default_account_id_for_user(conn, user_id):
    """The logged-in user's `users.default_cashbook_account_id` (mig 126,
    Phase 1a) — a pre-selection only, still changeable per entry (decision
    A3, plan.md). None if unset or user_id is falsy."""
    if not user_id:
        return None
    row = conn.execute(
        "SELECT default_cashbook_account_id FROM users WHERE id=?", (user_id,)
    ).fetchone()
    return row["default_cashbook_account_id"] if row else None


def _new_form_ctx(conn, accounts, txn_date, account_id_raw, bulk_mode, rows, employees,
                   confirm_duplicates=False, confirm_advance_cap=False,
                   confirm_new_categories=False, **extra):
    """Shared render context for every `cashbook/new.html` re-render inside
    `new_transaction()` (issue #532 review: was 5 near-identical
    render_template calls, each hand-copying the same 3 helper queries).

    Also carries the CURRENT request's three confirm flags into the
    template unconditionally — even the ones NOT being confirmed on this
    particular render — so the page can echo them back as hidden fields.
    Without this, tripping two confirm gates in one submission (e.g. a
    brand-new category on a duplicate row) loses the FIRST confirmation the
    moment the SECOND gate's screen renders (its form only has its own
    checkbox), and the user can never get past either gate (issue #532
    review finding: infinite ping-pong, confirmed empirically)."""
    ctx = dict(
        accounts=accounts, txn_date=txn_date, account_id=account_id_raw,
        bulk_mode=bulk_mode, rows=rows,
        categories_by_direction=_categories_by_direction(conn),
        known_tags=_get_known_user_tags(conn),
        employees=employees,
        advance_category=ADVANCE_CATEGORY, salary_category=SALARY_CATEGORY,
        commission_category=COMMISSION_CATEGORY,
        commission_reps=_commission_reps_for_picker(conn),
        confirm_duplicates=confirm_duplicates,
        confirm_advance_cap=confirm_advance_cap,
        confirm_new_categories=confirm_new_categories,
    )
    ctx.update(extra)
    return ctx


@bp_cashbook.route("/new", methods=["GET", "POST"])
def new_transaction():
    conn = database.get_connection()
    try:
        accounts = hrq.get_active_cashbook_accounts(conn)
        account_ids = {a["id"] for a in accounts}
        # Active employees for the advance-row employee picker (category
        # ADVANCE_CATEGORY swaps the ผู้ใช้ cell to this dropdown, plan.md C5).
        employees = hrq.get_employees(active_only=True)

        if request.method == "POST":
            txn_date = request.form.get("txn_date", "").strip() or date.today().isoformat()
            account_id_raw = request.form.get("account_id", "").strip()
            account_id = int(account_id_raw) if account_id_raw.isdigit() else None
            bulk_mode = request.form.get("bulk_mode") == "1"
            confirm_duplicates = request.form.get("confirm_duplicates") == "1"
            confirm_advance_cap = request.form.get("confirm_advance_cap") == "1"
            confirm_new_categories = request.form.get("confirm_new_categories") == "1"

            rows = _parse_batch_rows(request.form)
            to_insert = _validate_batch(rows, txn_date)
            # Advance rows (category == ADVANCE_CATEGORY) resolve + require an
            # employee; invalid ones get a row error here and drop out of
            # to_insert (plan.md C5). Must run BEFORE row_errors is computed.
            to_insert = _resolve_advance_rows(conn, rows, to_insert)
            # A category that EXISTS but is retired is refused outright — a
            # row error, so it blocks the whole batch the same as any other
            # basic-validation failure (issue #532 §1). Must also run BEFORE
            # row_errors is computed. cat_lookup is fetched once and reused
            # by the new-category-confirm gate further down.
            cat_lookup = _category_lookup(conn)
            _reject_inactive_categories(cat_lookup, rows, to_insert)

            form_errors = []
            if account_id not in account_ids:
                form_errors.append("กรุณาเลือกบัญชีที่ถูกต้องและยังใช้งานอยู่")
            row_errors = any(r["errors"] for r in rows)
            if not form_errors and not row_errors and not to_insert:
                form_errors.append("กรุณากรอกอย่างน้อย 1 รายการ")

            if form_errors or row_errors:
                for msg in form_errors:
                    flash(msg, "danger")
                return render_template("cashbook/new.html", **_new_form_ctx(
                    conn, accounts, txn_date, account_id_raw, bulk_mode, rows, employees,
                    confirm_duplicates, confirm_advance_cap, confirm_new_categories,
                ))

            # Policy blocks (plan.md C1-C3, D1, findings #1/#3/#6): manual
            # เงินเดือน is ALWAYS blocked; manual จ่ายค่าคอมมิชชั่น is blocked only
            # for an in-engine recipient (hybrid — off-system reps pass through).
            # Blocked rows drop out of to_insert; genuine validation errors
            # above already rejected the whole batch, so anything remaining
            # here is otherwise-valid and safe to keep saving.
            #
            # ⚠ Must run BEFORE ผู้ใช้ tag resolution below — the in-engine
            # check matches the RAW typed tag against salesperson aliases
            # (plan.md D3, ADR 0008); resolving it to an employee nickname
            # first could silently defeat the double-book guard if an
            # employee's name ever collides with a salesperson alias (issue
            # #532 review — confirmed empirically, this was a real ordering
            # bug in an earlier version of this change).
            to_insert, policy_blocked = _apply_policy_blocks(conn, rows, to_insert)
            if policy_blocked and not to_insert:
                # Nothing left to save (single-mode block, or a bulk batch
                # that was ENTIRELY policy-blocked) — re-render like a
                # validation error, no insert.
                for b in policy_blocked:
                    flash(b["reason"], "danger")
                return render_template("cashbook/new.html", **_new_form_ctx(
                    conn, accounts, txn_date, account_id_raw, bulk_mode, rows, employees,
                    confirm_duplicates, confirm_advance_cap, confirm_new_categories,
                ))
            if policy_blocked:
                # Bulk mode with at least one valid row left (decision D1):
                # skip the blocked rows + summarize, still save the rest.
                from collections import Counter
                for reason, n in Counter(b["reason"] for b in policy_blocked).items():
                    flash(f"ข้าม {n} แถว: {reason}", "warning")

            # ผู้ใช้ tag normalization (issue #532 §2): map a typed real name
            # to the employee's system name BEFORE duplicate detection and
            # insertion, so both see the canonical tag. Advance rows are
            # skipped (already carry the employee's system name). Notices
            # flash HERE — past every hard block above, so "we renamed your
            # tag" is never shown on a page where nothing was actually saved
            # (issue #532 review).
            tag_notices = _resolve_person_tags(conn, to_insert)
            for notice in tag_notices:
                flash(notice, "info")

            # New-category confirmation (issue #532 §1): a category that does
            # not exist yet needs explicit confirmation before it's created —
            # warn-then-confirm, same shape as the duplicate-row guard below.
            # Never silently creates one.
            if not confirm_new_categories:
                new_cats = _find_new_categories(cat_lookup, to_insert)
                if new_cats:
                    names = ", ".join(
                        f"{c['name']} ({'รายรับ' if c['direction'] == 'income' else 'รายจ่าย'})"
                        for c in new_cats
                    )
                    flash(f"หมวดหมู่ใหม่ที่ยังไม่มีในระบบ: {names} — ยืนยันเพื่อสร้างหมวดหมู่และบันทึกรายการ", "warning")
                    return render_template("cashbook/new.html", **_new_form_ctx(
                        conn, accounts, txn_date, account_id_raw, bulk_mode, rows, employees,
                        confirm_duplicates, confirm_advance_cap, confirm_new_categories,
                        show_new_category_confirm=True, new_categories=new_cats,
                    ))

            # Duplicate-row guard (decision D2, plan.md): warn-then-confirm,
            # never silently block or silently double-insert.
            if not confirm_duplicates:
                dup_indices = _find_duplicate_indices(conn, account_id, to_insert)
                if dup_indices:
                    for r in rows:
                        if r["index"] in dup_indices:
                            r["errors"].append("รายการนี้ซ้ำกับรายการที่มีอยู่แล้ว")
                    flash(f"พบรายการซ้ำ {len(dup_indices)} รายการ กรุณาตรวจสอบและยืนยัน", "warning")
                    return render_template("cashbook/new.html", **_new_form_ctx(
                        conn, accounts, txn_date, account_id_raw, bulk_mode, rows, employees,
                        confirm_duplicates, confirm_advance_cap, confirm_new_categories,
                        show_duplicate_confirm=True,
                    ))

            # Advance cap warning (plan.md P2): each advance row is checked
            # against what ITS OWN month can actually pay (Blocker C — keyed
            # off advance_date's month via r["effective_date"], never "today"
            # or the form's top txn_date), summed per (employee, target
            # month) across the whole submitted batch — plan.md step 5: two
            # rows individually under the ceiling can together exceed it.
            # Warn-then-confirm, same shape as the duplicate guard above;
            # never a hard block (Put: he is the only person who keys
            # advances, so there is no second approver to gate on).
            #
            # The lock is taken HERE, before _upsert_category below (the
            # first DML in this function) — hr_mod._begin_immediate refuses a
            # transaction already in flight, and the read (existing month
            # total + the ceiling) + the decision + the insert must be ONE
            # BEGIN IMMEDIATE transaction, or a concurrent worker can insert
            # a competing advance between the read and the write (Blocker A
            # — gunicorn -w 2 on Railway makes the second worker real).
            advance_rows = [r for r in to_insert if r.get("is_advance")]
            if advance_rows:
                hr_mod._begin_immediate(conn)
                groups = {}
                for r in advance_rows:
                    key = (r["employee_id"], r["effective_date"][:7])
                    groups[key] = groups.get(key, 0.0) + r["amount"]
                cap_warnings = []
                for (emp_id, target_month), amount in groups.items():
                    try:
                        hr_mod.check_advance_cap(conn, emp_id, target_month, amount)
                    except hr_mod.AdvanceCapWarning as w:
                        cap_warnings.append(str(w))
                if cap_warnings and not confirm_advance_cap:
                    # Release the lock before re-rendering — nothing was
                    # written, so there is nothing to keep it open for.
                    conn.rollback()
                    for w in cap_warnings:
                        flash(w, "warning")
                    return render_template("cashbook/new.html", **_new_form_ctx(
                        conn, accounts, txn_date, account_id_raw, bulk_mode, rows, employees,
                        confirm_duplicates, confirm_advance_cap, confirm_new_categories,
                        show_advance_cap_confirm=True,
                    ))

            # All rows valid (and no unconfirmed duplicates or advance-cap
            # warnings) — insert within one transaction (single connection,
            # single commit; if the advance-cap check above took the lock,
            # this reuses that SAME open transaction): upsert any brand-new
            # category first, then the rows, each at ITS OWN effective date
            # (decision B2, plan.md — single mode: the shared top date; bulk
            # mode: the row's own date, or the top date if the row was left
            # blank).
            created_by = session.get("display_name") or session.get("username")
            for r in to_insert:
                _upsert_category(conn, r["category"], r["direction"])
            for r in to_insert:
                if r.get("is_advance"):
                    # Cashbook-sourced advance: write the HR salary_advances row
                    # first (get its id), then the linked cashbook row — same
                    # conn, same commit, so the pair is atomic (finding #2). The
                    # cashbook row carries salary_advance_id; the ผู้ใช้ tag was
                    # set to the employee display in _resolve_advance_rows.
                    adv_id = conn.execute(
                        """INSERT INTO salary_advances
                           (employee_id, advance_date, amount, from_account_id, note)
                           VALUES (?,?,?,?,?)""",
                        (r["employee_id"], r["effective_date"], r["amount"],
                         account_id, r["note"] or None),
                    ).lastrowid
                    conn.execute(
                        """INSERT INTO cashbook_transactions
                           (account_id, txn_date, direction, category, user_category,
                            amount, description, note, created_by, salary_advance_id)
                           VALUES (?,?,'expense',?,?,?,?,?,?,?)""",
                        (account_id, r["effective_date"], r["category"],
                         r["user_category"] or None, r["amount"],
                         r["description"] or None, r["note"] or None, created_by,
                         adv_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO cashbook_transactions
                           (account_id, txn_date, direction, category, user_category,
                            amount, description, note, created_by)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (account_id, r["effective_date"], r["direction"], r["category"],
                         r["user_category"] or None, r["amount"],
                         r["description"] or None, r["note"] or None, created_by),
                    )
            conn.commit()
            flash(f"บันทึก {len(to_insert)} รายการเรียบร้อย", "success")
            # Land on the month of the LATEST row just saved (ticket #524) —
            # every row in a batch shares one account_id, but bulk mode lets
            # each row carry its own date, so "latest" is a real max, not
            # just the top-of-form date.
            latest_month = max(r["effective_date"] for r in to_insert)[:7]
            return redirect(url_for(
                "cashbook.account_ledger", account_id=account_id, month=latest_month,
            ))

        # Preselect the account the caller asked for (ticket #524 — the
        # account-ledger page's own "เพิ่มรายการ" button carries
        # ?account_id=<this account>), if it's still active; otherwise fall
        # back to the user's own default, same as when nothing is passed.
        requested_account_id_raw = request.args.get("account_id", "").strip()
        _requested_account_id = (
            int(requested_account_id_raw) if requested_account_id_raw.isdigit() else None
        )
        preselected_account_id = (
            _requested_account_id if _requested_account_id in account_ids else None
        )
        default_account_id = preselected_account_id or _default_account_id_for_user(
            conn, session.get("user_id")
        )
        return render_template("cashbook/new.html", **_new_form_ctx(
            conn, accounts, date.today().isoformat(),
            (str(default_account_id) if default_account_id else ""),
            False, _blank_rows(), employees,
        ))
    finally:
        conn.close()


def _reject_if_salary_row(row):
    """Salary pay-event rows (payroll_item_id set, posted by the HR pay-event)
    are locked — never editable/deletable from the cashbook. Flashes + aborts
    403 if locked."""
    if row["payroll_item_id"] is not None:
        flash("รายการนี้เป็นรายการเงินเดือนที่ผูกกับ Payroll — แก้ไข/ลบที่นี่ไม่ได้", "danger")
        abort(403)


def _advance_link_id(row):
    """salary_advance_id of a cashbook row, or None if that column is absent
    (a DB predating mig 128) or NULL. A pre-mig DB has no advances, so absent ==
    "not an advance row" — the correct fallback. sqlite3.Row raises
    IndexError/KeyError on a missing key, hence the guard."""
    try:
        return row["salary_advance_id"]
    except (IndexError, KeyError):
        return None


def _edit_policy_blocked_reason(conn, category, user_category):
    """Thai reason why `category` may not be set via txn_edit, or None.

    `_reject_if_advance_edit` only guards rows that are ALREADY linked, so
    without this an ORDINARY unlinked row could be edited INTO the advance
    category and would sit there with `salary_advance_id` NULL — invisible to
    payroll, never deducted. That is precisely how a ฿1,000 advance escaped
    run 6 (2026-08-05 clean-up). Edit deliberately does NOT write back:
    /cashbook/new stays the SOLE live writer of salary_advances (plan.md C5c /
    finding #4), so the answer is to refuse and point at delete + re-add —
    the same correction path an already-linked row gets.

    The salary / commission families defer to the create path's own gate so the
    two routes cannot drift.
    """
    if category == ADVANCE_CATEGORY:
        return ("เปลี่ยนเป็นหมวดเบิกล่วงหน้าที่นี่ไม่ได้ — ให้ลบรายการนี้แล้ว"
                "เพิ่มใหม่ที่หน้าบันทึกรายการ เพื่อให้ระบบผูกกับรายการเบิกของพนักงานให้")
    return _policy_blocked_reason(
        conn, {"category": category, "user_category": user_category}
    )


def _reject_if_advance_edit(row):
    """Advance-linked rows (salary_advance_id set) are NOT editable in place —
    the cashbook is their source of truth, and a correction is delete + re-add
    while still un-deducted (plan.md decision A / C5d). Deletion is handled
    separately (with a cascade to salary_advances). Flashes + aborts 403."""
    if _advance_link_id(row) is not None:
        flash("รายการเบิกล่วงหน้าแก้ไขที่นี่ไม่ได้ — ให้ลบแล้วเพิ่มใหม่ "
              "(ทำได้ก่อนถูกหักในรอบเงินเดือน)", "danger")
        abort(403)


def _commission_link_id(row):
    """commission_payout_id of a cashbook row, or None if that column is
    absent (a DB predating mig 129) or NULL. Mirrors _advance_link_id."""
    try:
        return row["commission_payout_id"]
    except (IndexError, KeyError):
        return None


def _reject_if_commission_row(row):
    """Commission-linked rows (commission_payout_id set, posted by the
    /commission auto-post — plan.md decision C1/C4, finding #6) are locked —
    never editable/deletable from the cashbook, for EVERY role including
    admin. They are removed only via /commission's "ยกเลิกการจ่าย"
    (commission.delete_payout), which cascades the linked row itself —
    mirror of _reject_if_salary_row for payroll_item_id. Flashes + aborts 403
    if locked.

    The flash wording is role-aware (#542): whether the CURRENT session's
    role could actually go cancel it at /commission is derived from the same
    rule that gates commission.commission_delete_payout
    (access_control.role_can_post) — never a hand-typed role list here, so a
    future change to who may cancel doesn't leave this flash stale."""
    if _commission_link_id(row) is not None:
        can_cancel = access_control.role_can_post(
            session.get('role', ''), 'commission.commission_delete_payout')
        cancel_clause = ('ยกเลิกได้ที่หน้าคอมมิชชั่นเท่านั้น' if can_cancel
                         else 'ให้แอดมินยกเลิกที่หน้าคอมมิชชั่น')
        flash("รายการนี้เป็นรายการคอมมิชชั่นที่ผูกกับหน้าคอมมิชชั่น — "
              f"แก้ไข/ลบที่นี่ไม่ได้ ({cancel_clause})", "danger")
        abort(403)


def _is_payout_row(row):
    """True for a marketplace-payout-sourced row (payout_platform set, added
    by cashbook_payout_mirror.mirror_platform — issue #533). Absent column
    (a DB predating mig 182) or NULL both mean "not a payout row"."""
    try:
        return row["payout_platform"] is not None
    except (IndexError, KeyError):
        return False


def _reject_if_payout_row(row):
    """Marketplace-payout-sourced rows (payout_platform set) are locked —
    never editable/deletable from the cashbook. They are kept in sync by
    cashbook_payout_mirror.mirror_platform on every marketplace import;
    correcting one means fixing the source payout on the marketplace pages,
    not hand-editing the mirror. Mirror of _reject_if_salary_row, keyed by
    value (marketplace_payouts has no stable row id to link) instead of an
    FK. Flashes + aborts 403 if locked."""
    if _is_payout_row(row):
        flash("รายการนี้เป็นยอดโอนจากมาร์เก็ตเพลสที่ระบบลงให้อัตโนมัติ — "
              "แก้ไข/ลบที่นี่ไม่ได้ (แก้ที่หน้ามาร์เก็ตเพลสแทน)", "danger")
        abort(403)


@bp_cashbook.route("/txn/<int:txn_id>/edit", methods=["POST"])
def txn_edit(txn_id):
    """Edit a manual row. Submitted from the edit modal on account_ledger.html
    (templates/cashbook/txn_edit.html) — there is no separate GET page, so on
    a validation error we flash + redirect back to the ledger rather than
    re-rendering a form. Salary and advance-linked rows are rejected (locked)."""
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM cashbook_transactions WHERE id=?", (txn_id,)
        ).fetchone()
        if row is None:
            abort(404)
        _reject_if_salary_row(row)
        _reject_if_advance_edit(row)
        _reject_if_commission_row(row)
        _reject_if_payout_row(row)

        account_id_raw = request.form.get("account_id", "").strip()
        txn_date = request.form.get("txn_date", "").strip()
        direction = request.form.get("direction", "").strip()
        category = request.form.get("category", "").strip()
        user_category = request.form.get("user_category", "").strip()
        amount_raw = request.form.get("amount", "").strip()
        description = request.form.get("description", "").strip()
        note = request.form.get("note", "").strip()

        account = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE id=? AND is_active=1",
            (account_id_raw,),
        ).fetchone() if account_id_raw.isdigit() else None
        try:
            amount = float(amount_raw)
        except ValueError:
            amount = None

        errors = []
        if account is None:
            errors.append("กรุณาเลือกบัญชีที่ถูกต้องและยังใช้งานอยู่")
        if not txn_date:
            errors.append("กรุณาระบุวันที่")
        else:
            try:
                date.fromisoformat(txn_date)
            except ValueError:
                errors.append("รูปแบบวันที่ไม่ถูกต้อง")
        if amount is None or amount <= 0:
            errors.append("จำนวนเงินต้องมากกว่า 0")
        if direction not in ("income", "expense"):
            errors.append("ประเภทไม่ถูกต้อง")
        if not category:
            errors.append("กรุณาระบุหมวดหมู่")

        # Both refusal paths below land back on the row's ORIGINAL month
        # (nothing was saved, so nothing moved) — never the default, which
        # under #524's own scoping can silently jump the admin to a
        # different month and hide both the flash and the row they were
        # trying to fix.
        if errors:
            for msg in errors:
                flash(msg, "danger")
            return redirect(url_for(
                "cashbook.account_ledger",
                account_id=row["account_id"], month=row["txn_date"][:7],
            ))

        blocked = _edit_policy_blocked_reason(conn, category, user_category)
        if blocked:
            flash(blocked, "danger")
            return redirect(url_for(
                "cashbook.account_ledger",
                account_id=row["account_id"], month=row["txn_date"][:7],
            ))

        # Category guard (issue #532 §1): edit may only set an ACTIVE existing
        # category, or leave the row's current (category, direction) pair
        # unchanged — rows 723/664/665 sit in retired categories on purpose,
        # and their OTHER fields must stay editable. Edit never creates one
        # (unlike /cashbook/new, which upserts after confirmation).
        category_changed = category != row["category"] or direction != row["direction"]
        if category_changed:
            active_cat = conn.execute(
                "SELECT 1 FROM cashbook_categories WHERE name=? AND direction=? AND is_active=1",
                (category, direction),
            ).fetchone()
            if active_cat is None:
                flash("หมวดหมู่นี้ไม่มีอยู่หรือถูกปิดใช้งานแล้ว กรุณาเลือกหมวดหมู่ที่ใช้งานอยู่", "danger")
                return redirect(url_for(
                    "cashbook.account_ledger",
                    account_id=row["account_id"], month=row["txn_date"][:7],
                ))

        # ผู้ใช้ tag normalization (issue #532 §2) — ONLY when the tag field
        # was actually changed: editing a row without touching its tag must
        # never change it, even if the stored value happens to look like an
        # employee's real name.
        if user_category != (row["user_category"] or ""):
            resolved, notice = _resolve_employee_tag(conn, user_category)
            user_category = resolved
            if notice:
                flash(notice, "info")

        new_vals = {
            "account_id": int(account_id_raw), "txn_date": txn_date, "direction": direction,
            "category": category, "user_category": user_category or None,
            "amount": amount, "description": description or None, "note": note or None,
        }
        changed = {
            field: [row[field], new_v]
            for field, new_v in new_vals.items() if row[field] != new_v
        }

        conn.execute(
            """UPDATE cashbook_transactions
               SET account_id=?, txn_date=?, direction=?, category=?, user_category=?,
                   amount=?, description=?, note=?
               WHERE id=?""",
            (*new_vals.values(), txn_id),
        )
        if changed:
            # The mig 076 AFTER UPDATE trigger already writes a field-diff
            # audit_log row for this UPDATE (user=NULL — triggers have no
            # session). This explicit row attributes the change to the
            # actor, same pattern as hr.py::reopen_run.
            conn.execute(
                "INSERT INTO audit_log(table_name, row_id, action, changed_fields, user)"
                " VALUES(?,?,?,?,?)",
                ("cashbook_transactions", txn_id, "UPDATE",
                 json.dumps(changed, ensure_ascii=False),
                 session.get("display_name") or session.get("username")),
            )
        conn.commit()
        flash("แก้ไขรายการเรียบร้อย", "success")
        # Land on the month of the row's NEW date, on its NEW account if it
        # moved (ticket #524) — never the default, which could hide the very
        # row just saved.
        return redirect(url_for(
            "cashbook.account_ledger",
            account_id=new_vals["account_id"], month=new_vals["txn_date"][:7],
        ))
    finally:
        conn.close()


@bp_cashbook.route("/txn/<int:txn_id>/delete", methods=["POST"])
def txn_delete(txn_id):
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM cashbook_transactions WHERE id=?", (txn_id,)
        ).fetchone()
        if row is None:
            abort(404)
        _reject_if_salary_row(row)
        _reject_if_commission_row(row)
        _reject_if_payout_row(row)

        account_id = row["account_id"]
        adv_id = _advance_link_id(row)
        # Fast-path reject for an advance already deducted by a payroll run
        # (the common locked case) — avoids deleting the cashbook row then
        # rolling it back.
        if adv_id is not None:
            adv = conn.execute(
                "SELECT deducted_in_run_id FROM salary_advances WHERE id=?", (adv_id,)
            ).fetchone()
            if adv is not None and adv["deducted_in_run_id"] is not None:
                flash("รายการเบิกล่วงหน้านี้ถูกหักในรอบเงินเดือนแล้ว — ลบไม่ได้", "danger")
                abort(403)

        # Delete the cashbook row (child) FIRST — the salary_advances FK forbids
        # dropping the parent while this row still references it.
        conn.execute("DELETE FROM cashbook_transactions WHERE id=?", (txn_id,))
        if adv_id is not None:
            # Cascade-delete the advance, but ONLY if still un-deducted. The
            # WHERE ... IS NULL makes this atomic against the gunicorn -w 2 race
            # (a payroll run could set deducted_in_run_id between the read above
            # and here): 0 rows matched -> a run just deducted it -> roll the
            # whole txn back (undoing the cashbook delete too) and abort.
            cur = conn.execute(
                "DELETE FROM salary_advances WHERE id=? AND deducted_in_run_id IS NULL",
                (adv_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                flash("รายการเบิกล่วงหน้านี้ถูกหักในรอบเงินเดือนแล้ว — ลบไม่ได้", "danger")
                abort(403)
        # The mig 076 BEFORE DELETE trigger already writes an audit_log DELETE
        # row (user=NULL). This explicit row attributes it to the actor, same
        # pattern as hr.py::reopen_run / the mig 076 UPDATE trigger above.
        conn.execute(
            "INSERT INTO audit_log(table_name, row_id, action, changed_fields, user)"
            " VALUES(?,?,?,?,?)",
            ("cashbook_transactions", txn_id, "DELETE",
             json.dumps({
                 "account_id": account_id, "txn_date": row["txn_date"],
                 "direction": row["direction"], "category": row["category"],
                 "amount": row["amount"],
             }, ensure_ascii=False),
             session.get("display_name") or session.get("username")),
        )
        conn.commit()
        flash("ลบรายการเรียบร้อย", "success")
        # Land on the deleted row's own month (ticket #524), not the default —
        # e.g. keying August rows into an account in mid-September and then
        # correcting one must not silently jump to September.
        return redirect(url_for(
            "cashbook.account_ledger", account_id=account_id, month=row["txn_date"][:7],
        ))
    finally:
        conn.close()
