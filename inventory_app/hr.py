"""HR payroll / leave engine — net pay + Thai Social Security + leave.

Mirrors `commission.py`: own module imported by `app.py` as a feature
module; raw SQL via sqlite3 (no ORM); `_connect()` reads
`config.DATABASE_PATH` so the `tmp_db` test fixture transparently swaps in
a copy of the live DB.

Schema: migration 054 (`employees`, `employee_salary_history`,
`leave_types`, `employee_leave_entitlements`, `leave_requests`,
`payroll_runs`, `payroll_items`, `hr_config`, `company_holidays`).

Money convention: amounts rounded to 2 decimals (incl. SSO — see _sso()
for why whole-baht was rejected). All knobs (sso_rate, sso_min_base,
sso_max_base, day_divisor) come from `hr_config` — never hard-coded.

WORKED-DAY / PRORATION CONVENTION (documented decision):
  For a payroll month, worked_days =
      (min(period_end, end_date or period_end)
       - max(period_start, start_date or period_start)).days + 1
  i.e. INCLUSIVE calendar days the employee is on payroll within that
  month, then CAPPED at day_divisor (30). base_amount =
  round(rate / day_divisor * worked_days, 2). Rationale: Thai monthly
  payroll conventionally divides by a fixed 30 ("วันต่อเดือน") regardless
  of 28/30/31; capping worked_days at the divisor means a full / near-full
  month never overpays. Consequence: an employee starting on the 2nd of a
  31-day month worked 30 inclusive days → capped at 30 → NO proration loss
  (full salary). A true mid-month start (e.g. day 16) DOES prorate.

Python 3.9 — Optional[...] not `X | None`.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date
from typing import Optional

from config import DATABASE_PATH


# ── DB helper ────────────────────────────────────────────────────────────────
def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _ConnCtx:
    """Use the caller's connection if given (no close); else open/own one."""

    def __init__(self, conn: Optional[sqlite3.Connection], db_path: Optional[str]):
        self._given = conn
        self._db_path = db_path
        self._owned: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        if self._given is not None:
            return self._given
        self._owned = _connect(self._db_path)
        return self._owned

    def __exit__(self, *exc):
        if self._owned is not None:
            self._owned.close()
        return False


# ── date helpers ─────────────────────────────────────────────────────────────
def _parse_ym(year_month: str):
    y, m = year_month.split("-")
    return int(y), int(m)


def _month_bounds(year_month: str):
    """Return (first_date, last_date) as datetime.date for a 'YYYY-MM'."""
    y, m = _parse_ym(year_month)
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last_day)


def previous_ym(today):
    """'YYYY-MM' of the calendar month before `today`."""
    y, m = today.year, today.month
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def payroll_reminder_month(today, conn):
    """Previous month's 'YYYY-MM' if any payroll company is missing its run for
    that month, else None. Drives the admin dashboard payroll nudge.

    'Payroll companies' = companies with active on_payroll employees (derived
    from data, not hardcoded), so a non-payroll entity never triggers it."""
    prev = previous_ym(today)
    company_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT company_id FROM employees "
        "WHERE on_payroll = 1 AND is_active = 1 AND company_id IS NOT NULL"
    )]
    for cid in company_ids:
        if not conn.execute(
            "SELECT 1 FROM payroll_runs WHERE year_month = ? AND company_id = ?",
            (prev, cid),
        ).fetchone():
            return prev
    return None


def _to_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return date.fromisoformat(str(s)[:10])


# ── config ───────────────────────────────────────────────────────────────────
def _load_config(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM hr_config").fetchall()
    cfg = {r["key"]: r["value"] for r in rows}
    return {
        "sso_rate": float(cfg.get("sso_rate", 0.05)),
        "sso_min_base": float(cfg.get("sso_min_base", 1650)),
        "sso_max_base": float(cfg.get("sso_max_base", 15000)),
        "day_divisor": float(cfg.get("day_divisor", 30)),
    }


# ── salary resolution ────────────────────────────────────────────────────────
def resolve_salary(employee_id: int, year_month: str,
                    conn: Optional[sqlite3.Connection] = None,
                    db_path: Optional[str] = None):
    """Effective salary_history row for the given month.

    Returns the row with max(effective_date) <= last day of the month
    (probation raise applies from the next full calendar month because the
    seed sets effective_date to the 1st of that month). Returns None if no
    salary row precedes the month.
    """
    _, last = _month_bounds(year_month)
    with _ConnCtx(conn, db_path) as c:
        return c.execute(
            """SELECT * FROM employee_salary_history
                 WHERE employee_id = ? AND effective_date <= ?
                 ORDER BY effective_date DESC, id DESC
                 LIMIT 1""",
            (employee_id, last.isoformat()),
        ).fetchone()


# ── leave balance ────────────────────────────────────────────────────────────
def _service_years_at(start_date: Optional[date], at: date) -> float:
    if start_date is None:
        return 0.0
    return (at - start_date).days / 365.25


def leave_balance(employee_id: int, year: int,
                  conn: Optional[sqlite3.Connection] = None,
                  db_path: Optional[str] = None) -> dict:
    """Per leave_type code: {entitlement, used, remaining, over}.

    entitlement = employee_leave_entitlements override for (emp, type, year)
                  else leave_types.default_quota_days (None → unlimited,
                  represented as float('inf')).
                  ANNUAL special case (contract 5.3 / quota_basis
                  'after_1yr'): if there is NO explicit override row and the
                  employee has < 1 year of service at year-end → 0.
    used        = SUM(days) of APPROVED leave_requests whose start_date is in
                  the calendar `year` (pending/rejected/cancelled excluded).
    remaining   = max(0, entitlement - used)  (inf if unlimited)
    over        = max(0, used - entitlement)  (0 if unlimited)
    """
    year_end = date(year, 12, 31)
    with _ConnCtx(conn, db_path) as c:
        emp = c.execute(
            "SELECT start_date FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        start_date = _to_date(emp["start_date"]) if emp else None

        types = c.execute(
            "SELECT id, code, default_quota_days, quota_basis "
            "FROM leave_types WHERE is_active = 1"
        ).fetchall()

        overrides = {
            r["leave_type_id"]: r["quota_days"]
            for r in c.execute(
                """SELECT leave_type_id, quota_days
                     FROM employee_leave_entitlements
                     WHERE employee_id = ? AND year = ?""",
                (employee_id, year),
            ).fetchall()
        }

        used_rows = c.execute(
            """SELECT leave_type_id, COALESCE(SUM(days), 0) AS d
                 FROM leave_requests
                 WHERE employee_id = ?
                   AND status = 'approved'
                   AND substr(start_date, 1, 4) = ?
                 GROUP BY leave_type_id""",
            (employee_id, f"{year:04d}"),
        ).fetchall()
        used_map = {r["leave_type_id"]: (r["d"] or 0) for r in used_rows}

    out = {}
    for t in types:
        tid = t["id"]
        if tid in overrides and overrides[tid] is not None:
            entitlement = float(overrides[tid])
        elif t["default_quota_days"] is None:
            entitlement = float("inf")  # unlimited (e.g. UNPAID)
        else:
            entitlement = float(t["default_quota_days"])
            if t["quota_basis"] == "after_1yr" and tid not in overrides:
                if _service_years_at(start_date, year_end) < 1.0:
                    entitlement = 0.0

        used = float(used_map.get(tid, 0) or 0)
        if entitlement == float("inf"):
            remaining = float("inf")
            over = 0.0
        else:
            remaining = max(0.0, entitlement - used)
            over = max(0.0, used - entitlement)

        out[t["code"]] = {
            "entitlement": entitlement,
            "used": used,
            "remaining": remaining,
            "over": over,
        }
    return out


# ── SSO ──────────────────────────────────────────────────────────────────────
def _sso(rate: float, sso_enrolled: int, cfg: dict) -> float:
    if not sso_enrolled:
        return 0.0
    base = min(max(rate, cfg["sso_min_base"]), cfg["sso_max_base"])
    # SPEC DECISION: the plan says "SSO rounded to whole baht", but task
    # case 1 asserts rate 1000 → 82.5 (= 1650 * 0.05), i.e. the satang is
    # preserved. Real Thai SSO is 5% of the capped wage base and is NOT
    # silently truncated to whole baht. The "whole baht" wording matches
    # the common cap case (15000*0.05 = 750.0). We round to 2 decimals:
    # 750.0 and 82.5 both satisfy, and no contribution is lost.
    return round(base * cfg["sso_rate"], 2)


# ── per-month unpaid-day computation ─────────────────────────────────────────
def _overlap_days(req_start: date, req_end: date, period_start: date,
                   period_end: date, total_days: float) -> float:
    """Portion of a leave request's `days` that falls in the payroll month.

    If the whole request lies inside the month → its full `days` (preserves
    half-days). If it straddles the boundary → prorate by the inclusive
    calendar-day overlap fraction.
    """
    lo = max(req_start, period_start)
    hi = min(req_end, period_end)
    if hi < lo:
        return 0.0
    span = (req_end - req_start).days + 1
    inside = (hi - lo).days + 1
    if inside >= span:
        return float(total_days)
    return round(total_days * inside / span, 4)


def _compute_unpaid_days(c: sqlite3.Connection, employee_id: int,
                         year_month: str, period_start: date,
                         period_end: date):
    """Return (unpaid_days, note_fragments).

    Unpaid days in the month =
      - all UNPAID-type leave days in the month, PLUS
      - over-quota days of paid leave types that affect the month, PLUS
      - MATERNITY days beyond max_paid_days (45).
    Over-quota is judged on the YEAR's cumulative approved usage per type;
    the excess is attributed to (and deducted in) the month whose leave
    pushed usage past the entitlement — here we attribute the whole
    over-quota amount to a run if that type's leave overlaps the month
    (sufficient for v1 single-run-per-month payroll; documented decision).
    """
    y, _ = _parse_ym(year_month)
    bal = leave_balance(employee_id, y, conn=c)

    types = {
        r["id"]: r
        for r in c.execute(
            "SELECT id, code, is_paid, max_paid_days FROM leave_types"
        ).fetchall()
    }
    code_by_id = {tid: r["code"] for tid, r in types.items()}

    reqs = c.execute(
        """SELECT leave_type_id, start_date, end_date, days
             FROM leave_requests
             WHERE employee_id = ? AND status = 'approved'""",
        (employee_id,),
    ).fetchall()

    unpaid = 0.0
    notes = []
    # codes whose leave overlaps THIS month (drives over-quota attribution)
    overlapping_codes = set()
    maternity_in_month = 0.0

    for r in reqs:
        rs, re_ = _to_date(r["start_date"]), _to_date(r["end_date"])
        portion = _overlap_days(rs, re_, period_start, period_end, r["days"])
        if portion <= 0:
            continue
        t = types[r["leave_type_id"]]
        code = t["code"]
        overlapping_codes.add(code)
        if not t["is_paid"]:
            unpaid += portion  # UNPAID type → directly unpaid
        if code == "MATERNITY":
            maternity_in_month += portion

    # over-quota of paid leave types (SICK/PERSONAL/ANNUAL ...)
    for code, b in bal.items():
        if code == "UNPAID":
            continue
        if code in overlapping_codes and b["over"] > 0:
            if code == "MATERNITY":
                continue  # handled via max_paid_days below
            unpaid += b["over"]
            notes.append(
                f"{code} เกินสิทธิ {b['over']:g} วัน → หักเป็นลาไม่รับค่าจ้าง"
            )

    # MATERNITY beyond max_paid_days (45)
    mat_type = next((r for r in types.values() if r["code"] == "MATERNITY"),
                    None)
    if maternity_in_month > 0 and mat_type and mat_type["max_paid_days"]:
        excess = max(0.0, maternity_in_month - float(mat_type["max_paid_days"]))
        if excess > 0:
            unpaid += excess
            notes.append(
                f"ลาคลอดเกิน {mat_type['max_paid_days']:g} วันที่จ่าย "
                f"→ หัก {excess:g} วัน"
            )

    return round(unpaid, 4), notes


# ── core: build one payroll_items payload ────────────────────────────────────
def _build_item(c: sqlite3.Connection, emp: sqlite3.Row, year_month: str,
                cfg: dict, run_id: Optional[int] = None):
    period_start, period_end = _month_bounds(year_month)
    divisor = cfg["day_divisor"]

    sal = resolve_salary(emp["id"], year_month, conn=c)
    rate = float(sal["monthly_salary"]) if sal else 0.0

    start_date = _to_date(emp["start_date"])
    end_date = _to_date(emp["end_date"])

    # worked-day window inside the month (see module docstring)
    eff_start = max(period_start, start_date) if start_date else period_start
    eff_end = min(period_end, end_date) if end_date else period_end
    worked_days = (eff_end - eff_start).days + 1
    if worked_days < 0:
        worked_days = 0
    capped_days = min(worked_days, divisor)
    base_amount = round(rate / divisor * capped_days, 2)

    # unpaid leave
    unpaid_days, unpaid_notes = _compute_unpaid_days(
        c, emp["id"], year_month, period_start, period_end
    )
    unpaid_deduction = round(rate / divisor * unpaid_days, 2)

    # diligence — must work the FULL month (started by period_start AND
    # still employed at period_end). Applied BEFORE leave-forfeit so the
    # existing leave logic only sees the post-partial-month allowance.
    diligence_allowance = float(emp["diligence_allowance"] or 0)
    diligence_forfeited = 0
    diligence_reason = None
    if diligence_allowance > 0:
        partial_month = (
            (start_date is not None and start_date > period_start)
            or (end_date is not None and end_date < period_end)
        )
        if partial_month:
            diligence_allowance = 0
    # leave-based forfeit only applies if any allowance survives the partial
    # check above (an employee with allowance=0 already has nothing to forfeit).
    if diligence_allowance > 0:
        hit = c.execute(
            """SELECT 1
                 FROM leave_requests lr
                 JOIN leave_types lt ON lt.id = lr.leave_type_id
                WHERE lr.employee_id = ?
                  AND lr.status = 'approved'
                  AND lt.affects_diligence = 1
                  AND lr.start_date <= ?
                  AND lr.end_date   >= ?
                LIMIT 1""",
            (emp["id"], period_end.isoformat(), period_start.isoformat()),
        ).fetchone()
        if hit:
            diligence_forfeited = 1
            diligence_reason = "leave"

    # SSO
    sso_emp = _sso(rate, emp["sso_enrolled"], cfg)
    sso_empr = sso_emp  # employer mirrors employee (informational cost)

    # commission — informational only (NOT added to gross)
    commission_amount = 0.0
    sp_code = emp["salesperson_code"]
    if sp_code:
        row = c.execute(
            """SELECT COALESCE(SUM(amount_paid), 0) AS amt
                 FROM commission_payouts
                WHERE salesperson_code = ? AND year_month = ?""",
            (sp_code, year_month),
        ).fetchone()
        commission_amount = round(row["amt"] or 0.0, 2)

    # salary advances (เบิกเงินล่วงหน้า) — sum advances dated on/before the
    # month end that are NOT yet deducted in another run. A draft includes
    # advances still un-stamped OR stamped to THIS run (re-runnable without
    # doubling); generate_run does NOT stamp — finalize_run does.
    sa_row = c.execute(
        """SELECT COALESCE(SUM(amount), 0) AS amt
             FROM salary_advances
            WHERE employee_id = ?
              AND advance_date <= ?
              AND (deducted_in_run_id IS NULL OR deducted_in_run_id = ?)""",
        (emp["id"], period_end.isoformat(), run_id),
    ).fetchone()
    salary_advance_deduction = round(sa_row["amt"] or 0.0, 2)

    note = "; ".join(unpaid_notes) if unpaid_notes else None

    return {
        "employee_id": emp["id"],
        "salary_rate": rate,
        "base_amount": base_amount,
        "unpaid_leave_days": unpaid_days,
        "unpaid_leave_deduction": unpaid_deduction,
        "diligence_allowance": diligence_allowance,
        "diligence_forfeited": diligence_forfeited,
        "diligence_forfeit_reason": diligence_reason,
        "bonus": 0.0,
        "other_additions": 0.0,
        "other_additions_note": None,
        "other_deductions": 0.0,
        "other_deductions_note": None,
        "salary_advance_deduction": salary_advance_deduction,
        "sso_employee": sso_emp,
        "sso_employer": sso_empr,
        "commission_amount": commission_amount,
        "note": note,
    }


def _recompute_totals(d: dict) -> dict:
    """Compute gross + net_pay from a payroll_items dict in place.

    gross = base + (diligence if kept) + bonus + other_additions
    net   = gross - unpaid_leave_deduction - sso_employee - other_deductions
            - salary_advance_deduction
    """
    diligence = 0.0
    if not d["diligence_forfeited"]:
        diligence = float(d["diligence_allowance"] or 0)
    gross = round(
        float(d["base_amount"])
        + diligence
        + float(d["bonus"] or 0)
        + float(d["other_additions"] or 0),
        2,
    )
    net = round(
        gross
        - float(d["unpaid_leave_deduction"] or 0)
        - float(d["sso_employee"] or 0)
        - float(d["other_deductions"] or 0)
        - float(d.get("salary_advance_deduction", 0) or 0),
        2,
    )
    d["gross"] = gross
    d["net_pay"] = net
    return d


# ── generate / upsert a payroll run ──────────────────────────────────────────
def generate_run(year_month: str, company_id: int, created_by: int,
                  conn: Optional[sqlite3.Connection] = None,
                  db_path: Optional[str] = None):
    """Upsert the draft payroll_runs row for (year_month, company_id) and
    (re)build payroll_items for every active employee of that company who is
    on payroll during the month. Returns the payroll_runs row.

    Re-runnable: existing items for the run are replaced (preserving nothing
    — generation is the baseline; admin edits happen afterwards via
    update_payroll_item). A finalized run is left untouched.
    """
    period_start, period_end = _month_bounds(year_month)

    with _ConnCtx(conn, db_path) as c:
        cfg = _load_config(c)

        run = c.execute(
            "SELECT * FROM payroll_runs WHERE year_month = ? AND company_id = ?",
            (year_month, company_id),
        ).fetchone()
        if run is None:
            cur = c.execute(
                """INSERT INTO payroll_runs
                     (year_month, company_id, status, run_date, created_by)
                   VALUES (?, ?, 'draft', date('now','localtime'), ?)""",
                (year_month, company_id, created_by),
            )
            run_id = cur.lastrowid
        else:
            run_id = run["id"]
            if run["status"] == "finalized":
                c.commit()
                return c.execute(
                    "SELECT * FROM payroll_runs WHERE id = ?", (run_id,)
                ).fetchone()

        # active employees of this company who overlap the payroll month
        emps = c.execute(
            """SELECT * FROM employees
                 WHERE company_id = ? AND is_active = 1 AND on_payroll = 1
                   AND (start_date IS NULL OR start_date <= ?)
                   AND (end_date   IS NULL OR end_date   >= ?)""",
            (company_id, period_end.isoformat(), period_start.isoformat()),
        ).fetchall()

        c.execute("DELETE FROM payroll_items WHERE run_id = ?", (run_id,))
        for emp in emps:
            d = _build_item(c, emp, year_month, cfg, run_id=run_id)
            _recompute_totals(d)
            c.execute(
                """INSERT INTO payroll_items
                     (run_id, employee_id, salary_rate, base_amount,
                      unpaid_leave_days, unpaid_leave_deduction,
                      diligence_allowance, diligence_forfeited,
                      diligence_forfeit_reason, bonus, other_additions,
                      other_additions_note, other_deductions,
                      other_deductions_note, salary_advance_deduction,
                      sso_employee, sso_employer,
                      commission_amount, gross, net_pay, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, d["employee_id"], d["salary_rate"], d["base_amount"],
                 d["unpaid_leave_days"], d["unpaid_leave_deduction"],
                 d["diligence_allowance"], d["diligence_forfeited"],
                 d["diligence_forfeit_reason"], d["bonus"],
                 d["other_additions"], d["other_additions_note"],
                 d["other_deductions"], d["other_deductions_note"],
                 d["salary_advance_deduction"],
                 d["sso_employee"], d["sso_employer"],
                 d["commission_amount"], d["gross"], d["net_pay"], d["note"]),
            )
        # Reconcile orphaned advance stamps. Scenario: run was finalized
        # (advances stamped to this run), then reopened, then regenerated
        # against a different employee set (e.g. someone went is_active=0).
        # Their advance stays stamped to this run but no item deducts it,
        # so the standard filter (`IS NULL OR = run_id`) excludes it forever.
        # Un-stamp so the advance follows the employee to their next paid run.
        c.execute(
            """UPDATE salary_advances
                  SET deducted_in_run_id = NULL
                WHERE deducted_in_run_id = ?
                  AND employee_id NOT IN (
                      SELECT employee_id FROM payroll_items WHERE run_id = ?
                  )""",
            (run_id, run_id),
        )
        c.commit()
        return c.execute(
            "SELECT * FROM payroll_runs WHERE id = ?", (run_id,)
        ).fetchone()


# ── edit one payroll item then recompute its totals ──────────────────────────
def update_payroll_item(item_id: int,
                        bonus: Optional[float] = None,
                        other_additions: Optional[float] = None,
                        other_additions_note: Optional[str] = None,
                        other_deductions: Optional[float] = None,
                        other_deductions_note: Optional[str] = None,
                        late: Optional[bool] = None,
                        conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None):
    """Admin edit of a single payroll line: bonus / other additions /
    other deductions / manual "มาสาย" (late) toggle. Recomputes gross +
    net_pay. Only the params passed (non-None) are changed.

    `late=True`  → force diligence forfeited, reason 'late'
    `late=False` → clear a 'late'-reason forfeit (a 'leave' forfeit, being
                   data-derived, is NOT cleared by toggling late off).
    Returns the updated payroll_items row.
    """
    with _ConnCtx(conn, db_path) as c:
        row = c.execute(
            "SELECT * FROM payroll_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"payroll_items id {item_id} not found")
        d = dict(row)

        if bonus is not None:
            d["bonus"] = float(bonus)
        if other_additions is not None:
            d["other_additions"] = float(other_additions)
        if other_additions_note is not None:
            d["other_additions_note"] = other_additions_note
        if other_deductions is not None:
            d["other_deductions"] = float(other_deductions)
        if other_deductions_note is not None:
            d["other_deductions_note"] = other_deductions_note

        if late is True:
            d["diligence_forfeited"] = 1
            d["diligence_forfeit_reason"] = "late"
        elif late is False:
            if d["diligence_forfeit_reason"] == "late":
                d["diligence_forfeited"] = 0
                d["diligence_forfeit_reason"] = None

        _recompute_totals(d)

        c.execute(
            """UPDATE payroll_items
                  SET bonus = ?, other_additions = ?, other_additions_note = ?,
                      other_deductions = ?, other_deductions_note = ?,
                      diligence_forfeited = ?, diligence_forfeit_reason = ?,
                      gross = ?, net_pay = ?
                WHERE id = ?""",
            (d["bonus"], d["other_additions"], d["other_additions_note"],
             d["other_deductions"], d["other_deductions_note"],
             d["diligence_forfeited"], d["diligence_forfeit_reason"],
             d["gross"], d["net_pay"], item_id),
        )
        c.commit()
        return c.execute(
            "SELECT * FROM payroll_items WHERE id = ?", (item_id,)
        ).fetchone()


# ── finalize a payroll run (stamps salary advances) ──────────────────────────
def finalize_run(run_id: int,
                  conn: Optional[sqlite3.Connection] = None,
                  db_path: Optional[str] = None):
    """Mark a draft run finalized and STAMP the salary advances it consumed.

    If the run is already finalized this is a no-op (returns the row; does
    NOT re-mark or re-stamp — safe to call twice).

    On finalize:
      1. status='finalized', finalized_at=now('localtime')
      2. every un-stamped salary_advances row for an employee in this run,
         dated on/before the run month's period_end, gets
         deducted_in_run_id = run_id so a later month never re-deducts it.
    Returns the payroll_runs row (or None if run_id unknown).
    """
    with _ConnCtx(conn, db_path) as c:
        run = c.execute(
            "SELECT * FROM payroll_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return None
        if run["status"] == "finalized":
            return run  # no-op: no re-mark, no re-stamp

        _, period_end = _month_bounds(run["year_month"])

        c.execute(
            """UPDATE payroll_runs
                  SET status = 'finalized',
                      finalized_at = datetime('now','localtime')
                WHERE id = ?""",
            (run_id,),
        )
        c.execute(
            """UPDATE salary_advances
                  SET deducted_in_run_id = :run_id
                WHERE deducted_in_run_id IS NULL
                  AND advance_date <= :period_end
                  AND employee_id IN (
                      SELECT employee_id FROM payroll_items
                       WHERE run_id = :run_id
                  )""",
            {"run_id": run_id, "period_end": period_end.isoformat()},
        )
        c.commit()
        return c.execute(
            "SELECT * FROM payroll_runs WHERE id = ?", (run_id,)
        ).fetchone()


# ── reopen a finalized run (admin escape hatch) ──────────────────────────────
def reopen_run(run_id: int, reason: str, actor: str,
               conn: Optional[sqlite3.Connection] = None,
               db_path: Optional[str] = None):
    """Un-finalize a finalized run. Records an explicit audit_log entry with
    the actor + human reason so the "why" survives (mig 071's UPDATE trigger
    captures the field diff with user=NULL).

    Does NOT un-stamp advances: the `_build_item` advance filter
    (`deducted_in_run_id IS NULL OR = run_id`) already includes
    already-stamped-to-this-run advances on regenerate. Un-stamping would
    create a foot-gun — if the admin forgets to re-finalize, the unstamped
    advances would bleed into the next month's run (the exact bug we fixed
    when reconciling 2026-05).

    Raises ValueError on empty reason. Returns the run row, or None if
    run_id not found. No-op (returns row) if run is already draft.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    with _ConnCtx(conn, db_path) as c:
        run = c.execute(
            "SELECT * FROM payroll_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return None
        if run["status"] != "finalized":
            return run
        c.execute(
            "UPDATE payroll_runs SET status='draft', finalized_at=NULL WHERE id=?",
            (run_id,),
        )
        c.execute(
            """INSERT INTO audit_log (table_name, row_id, action, changed_fields, user)
                 VALUES ('payroll_runs', ?, 'UPDATE', ?, ?)""",
            (run_id,
             json.dumps({"reopen_reason": reason.strip()}, ensure_ascii=False),
             actor),
        )
        c.commit()
        return c.execute(
            "SELECT * FROM payroll_runs WHERE id = ?", (run_id,)
        ).fetchone()
