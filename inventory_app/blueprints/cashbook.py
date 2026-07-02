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
from datetime import date
from typing import Optional

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import database
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


def _tcat_ph():
    """Placeholder string + params for the transfer-category list."""
    return ",".join("?" * len(TRANSFER_CATEGORIES)), list(TRANSFER_CATEGORIES)


def _get_accounts_with_totals(conn):
    """
    One dict per active account. `income`/`expense` are OPERATING totals (transfer
    categories excluded) so they sum to the headline P&L. `transfer_in`/`transfer_out`
    hold the excluded capital movements. `balance` is true cash = operating + transfers.
    """
    ph, params = _tcat_ph()
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
            COALESCE(SUM(CASE WHEN t.direction='income'  AND COALESCE(t.category,'') NOT IN ({ph}) THEN t.amount ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN t.direction='expense' AND COALESCE(t.category,'') NOT IN ({ph}) THEN t.amount ELSE 0 END), 0) AS expense,
            COALESCE(SUM(CASE WHEN t.direction='income'  AND COALESCE(t.category,'') IN ({ph}) THEN t.amount ELSE 0 END), 0) AS transfer_in,
            COALESCE(SUM(CASE WHEN t.direction='expense' AND COALESCE(t.category,'') IN ({ph}) THEN t.amount ELSE 0 END), 0) AS transfer_out,
            COUNT(t.id) AS txn_count
        FROM cashbook_accounts a
        LEFT JOIN cashbook_transactions t ON t.account_id = a.id
        WHERE a.is_active = 1
        GROUP BY a.id
        ORDER BY a.is_transfer ASC, a.sort_order ASC, a.id ASC
    """, params * 4).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["balance"] = (d["income"] + d["transfer_in"]) - (d["expense"] + d["transfer_out"])
        result.append(d)
    return result


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


def _get_category_summary(conn):
    """Income and expense totals by category, excluding transfer accounts AND
    transfer categories."""
    ph, params = _tcat_ph()
    rows = conn.execute(f"""
        SELECT
            t.direction,
            COALESCE(t.category, '(ไม่ระบุ)') AS category,
            SUM(t.amount) AS total
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0 AND COALESCE(t.category,'') NOT IN ({ph})
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


def _get_tag_summary(conn):
    """Operating EXPENSE grouped by ผู้ใช้ tag (user_category). Excludes transfer
    accounts, transfer categories and untagged rows."""
    ph, params = _tcat_ph()
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
          AND t.user_category IS NOT NULL AND t.user_category != ''
        GROUP BY t.user_category
        ORDER BY total DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


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
        accounts      = _get_accounts_with_totals(conn)
        monthly       = _get_monthly_summary(conn, exclude_transfer=True)
        income_cats, expense_cats = _get_category_summary(conn)
        tag_summary   = _get_tag_summary(conn)
    finally:
        conn.close()

    # Headline P&L excludes transfer accounts AND transfer categories (income/expense
    # are already operating-only from _get_accounts_with_totals).
    op_accounts = [a for a in accounts if not a["is_transfer"]]
    tr_accounts  = [a for a in accounts if a["is_transfer"]]

    total_income  = sum(a["income"]  for a in op_accounts)
    total_expense = sum(a["expense"] for a in op_accounts)
    # คงเหลือ = actual cash on hand = sum of true-cash account balances (which include
    # capital transfers). This reconciles with the per-account balance column. It is
    # deliberately NOT income − expense: transfers fund the gap (see disclosure note).
    total_balance = sum(a["balance"] for a in op_accounts)
    # Capital/inter-account movements excluded from the P&L (disclosure figure)
    transfer_total = sum(a["transfer_in"] + a["transfer_out"] for a in op_accounts)

    return render_template(
        "cashbook/dashboard.html",
        accounts=accounts,
        op_accounts=op_accounts,
        tr_accounts=tr_accounts,
        total_income=total_income,
        total_expense=total_expense,
        total_balance=total_balance,
        transfer_total=transfer_total,
        monthly=monthly,
        income_cats=income_cats,
        expense_cats=expense_cats,
        expense_chart=_expense_topn(expense_cats),
        tag_summary=tag_summary,
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

        month_filter = request.args.get("month", "").strip()
        dir_filter   = request.args.get("dir", "").strip()
        page         = max(1, int(request.args.get("page", 1)))
        per_page     = 50

        params = [account_id]
        where  = ["t.account_id=?"]

        if month_filter:
            where.append("strftime('%Y-%m', t.txn_date)=?")
            params.append(month_filter)
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

        # Running totals for displayed page
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

        # Available months for filter dropdown
        months = conn.execute(
            """SELECT DISTINCT strftime('%Y-%m', txn_date) AS m
               FROM cashbook_transactions
               WHERE account_id=?
               ORDER BY m""",
            (account_id,),
        ).fetchall()

        # For the per-row edit modals (manual rows only — see txn_edit.html).
        accounts = hrq.get_active_cashbook_accounts(conn)
        categories_by_direction = _categories_by_direction(conn)
        known_tags = _get_known_user_tags(conn)
    finally:
        conn.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)

    return render_template(
        "cashbook/account_ledger.html",
        acct=dict(acct),
        rows=[dict(r) for r in rows],
        month_filter=month_filter,
        dir_filter=dir_filter,
        page=page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
        sum_income=sum_income,
        sum_expense=sum_expense,
        balance=sum_income - sum_expense,
        months=[r["m"] for r in months],
        accounts=accounts,
        categories_by_direction=categories_by_direction,
        known_tags=known_tags,
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
    """Distinct ผู้ใช้ tags already used, for the user_category <datalist>.
    There is no separate tags table — user_category is free text on
    cashbook_transactions."""
    rows = conn.execute(
        """SELECT DISTINCT user_category FROM cashbook_transactions
            WHERE user_category IS NOT NULL AND user_category != ''
            ORDER BY user_category"""
    ).fetchall()
    return [r["user_category"] for r in rows]


def _blank_rows(n=8):
    return [
        {"index": i, "direction": "expense", "category": "", "user_category": "",
         "amount": "", "description": "", "note": "", "errors": []}
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
            "amount": form.get(f"rows-{i}-amount", "").strip(),
            "description": form.get(f"rows-{i}-description", "").strip(),
            "note": form.get(f"rows-{i}-note", "").strip(),
            "errors": [],
        })
    return rows


def _validate_batch(rows):
    """Validate non-blank rows in place (sets row['errors']); blank rows
    (amount empty) are left untouched — they're skipped, not errors.
    Returns the list of rows that are valid AND non-blank, each with a
    parsed float `amount`."""
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
        if errors:
            r["errors"] = errors
        else:
            to_insert.append({**r, "amount": amount})
    return to_insert


def _upsert_category(conn, name, direction):
    conn.execute(
        "INSERT OR IGNORE INTO cashbook_categories(name,direction,source) VALUES(?,?,NULL)",
        (name, direction),
    )


@bp_cashbook.route("/new", methods=["GET", "POST"])
def new_transaction():
    conn = database.get_connection()
    try:
        accounts = hrq.get_active_cashbook_accounts(conn)
        account_ids = {a["id"] for a in accounts}

        if request.method == "POST":
            txn_date = request.form.get("txn_date", "").strip() or date.today().isoformat()
            account_id_raw = request.form.get("account_id", "").strip()
            account_id = int(account_id_raw) if account_id_raw.isdigit() else None

            rows = _parse_batch_rows(request.form)
            to_insert = _validate_batch(rows)

            form_errors = []
            if account_id not in account_ids:
                form_errors.append("กรุณาเลือกบัญชีที่ถูกต้องและยังใช้งานอยู่")
            row_errors = any(r["errors"] for r in rows)
            if not form_errors and not row_errors and not to_insert:
                form_errors.append("กรุณากรอกอย่างน้อย 1 รายการ")

            if form_errors or row_errors:
                for msg in form_errors:
                    flash(msg, "danger")
                return render_template(
                    "cashbook/new.html",
                    accounts=accounts,
                    txn_date=txn_date,
                    account_id=account_id_raw,
                    rows=rows,
                    categories_by_direction=_categories_by_direction(conn),
                    known_tags=_get_known_user_tags(conn),
                )

            # All rows valid — insert within one transaction (single connection,
            # single commit): upsert any brand-new category first, then the rows.
            created_by = session.get("display_name") or session.get("username")
            for r in to_insert:
                _upsert_category(conn, r["category"], r["direction"])
            for r in to_insert:
                conn.execute(
                    """INSERT INTO cashbook_transactions
                       (account_id, txn_date, direction, category, user_category,
                        amount, description, note, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (account_id, txn_date, r["direction"], r["category"],
                     r["user_category"] or None, r["amount"],
                     r["description"] or None, r["note"] or None, created_by),
                )
            conn.commit()
            flash(f"บันทึก {len(to_insert)} รายการเรียบร้อย", "success")
            return redirect(url_for("cashbook.account_ledger", account_id=account_id))

        return render_template(
            "cashbook/new.html",
            accounts=accounts,
            txn_date=date.today().isoformat(),
            account_id="",
            rows=_blank_rows(),
            categories_by_direction=_categories_by_direction(conn),
            known_tags=_get_known_user_tags(conn),
        )
    finally:
        conn.close()


def _reject_if_salary_row(row):
    """Salary pay-event rows (payroll_item_id set, posted by a later phase)
    are locked — never editable/deletable from the cashbook. Flashes + aborts
    403 if locked."""
    if row["payroll_item_id"] is not None:
        flash("รายการนี้เป็นรายการเงินเดือนที่ผูกกับ Payroll — แก้ไข/ลบที่นี่ไม่ได้", "danger")
        abort(403)


@bp_cashbook.route("/txn/<int:txn_id>/edit", methods=["POST"])
def txn_edit(txn_id):
    """Edit a manual row. Submitted from the edit modal on account_ledger.html
    (templates/cashbook/txn_edit.html) — there is no separate GET page, so on
    a validation error we flash + redirect back to the ledger (same pattern as
    hr.py::advance_edit) rather than re-rendering a form."""
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM cashbook_transactions WHERE id=?", (txn_id,)
        ).fetchone()
        if row is None:
            abort(404)
        _reject_if_salary_row(row)

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
        if amount is None or amount <= 0:
            errors.append("จำนวนเงินต้องมากกว่า 0")
        if direction not in ("income", "expense"):
            errors.append("ประเภทไม่ถูกต้อง")
        if not category:
            errors.append("กรุณาระบุหมวดหมู่")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return redirect(url_for("cashbook.account_ledger", account_id=row["account_id"]))

        _upsert_category(conn, category, direction)

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
        return redirect(url_for("cashbook.account_ledger", account_id=new_vals["account_id"]))
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

        account_id = row["account_id"]
        conn.execute("DELETE FROM cashbook_transactions WHERE id=?", (txn_id,))
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
        return redirect(url_for("cashbook.account_ledger", account_id=account_id))
    finally:
        conn.close()
