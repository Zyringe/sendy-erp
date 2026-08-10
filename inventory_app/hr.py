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
import math
import sqlite3
from datetime import date, timedelta
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


def _round_half(x: float) -> float:
    """Round to the nearest 0.5 (ties up). 4.2575 → 4.5, 5.49 → 5.5, 6.0 → 6.0."""
    return math.floor(x * 2 + 0.5) / 2.0


def _prorate_annual(start_date: Optional[date], probation_end: Optional[date],
                    probation_days: Optional[int], quota: float,
                    year: int) -> float:
    """ANNUAL entitlement — prorated from hire date, granted after probation.

    Put 2026-07-22: replaces the after_1yr all-or-nothing gate.
      - no start_date                    → 0 (cannot prorate)
      - probation not ended by year-end  → 0 (Model P: evaluated at year-end)
      - else round_half(quota × days_worked_in_year / days_in_year), counting
        from max(hire, Jan 1) to Dec 31 inclusive — so a full calendar year
        yields the full quota (6) and the first partial year is prorated.
    `probation_end` falls back to start_date + probation_days when the stored
    probation_end_date is blank.
    """
    if start_date is None:
        return 0.0
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    if probation_end is None and probation_days is not None:
        probation_end = start_date + timedelta(days=int(probation_days))
    if probation_end is not None and probation_end > year_end:
        return 0.0  # still on probation through year-end
    accrual_start = max(start_date, year_start)
    if accrual_start > year_end:
        return 0.0  # hired after this year
    days_in_year = (year_end - year_start).days + 1
    days_worked = (year_end - accrual_start).days + 1
    return _round_half(quota * days_worked / days_in_year)


def leave_balance(employee_id: int, year: int,
                  conn: Optional[sqlite3.Connection] = None,
                  db_path: Optional[str] = None) -> dict:
    """Per leave_type code: {entitlement, used, remaining, over}.

    entitlement = employee_leave_entitlements override for (emp, type, year)
                  else leave_types.default_quota_days (None → unlimited,
                  represented as float('inf')).
                  ANNUAL (quota_basis 'prorate_probation', Put 2026-07-22):
                  with NO override row → prorated from hire date via
                  _prorate_annual (0 while still on probation at year-end,
                  else round_half(6 × days_worked_in_year / days_in_year);
                  a full calendar year → 6). Legacy 'after_1yr' still honoured.
    used        = SUM(days) of APPROVED leave_requests whose start_date is in
                  the calendar `year` (pending/rejected/cancelled excluded).
    remaining   = max(0, entitlement - used)  (inf if unlimited)
    over        = max(0, used - entitlement)  (0 if unlimited)
    """
    year_end = date(year, 12, 31)
    with _ConnCtx(conn, db_path) as c:
        emp = c.execute(
            "SELECT start_date, probation_end_date, probation_days "
            "FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        start_date = _to_date(emp["start_date"]) if emp else None
        probation_end = _to_date(emp["probation_end_date"]) if emp else None
        probation_days = emp["probation_days"] if emp else None

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
            if tid not in overrides:
                if t["quota_basis"] == "after_1yr":
                    if _service_years_at(start_date, year_end) < 1.0:
                        entitlement = 0.0
                elif t["quota_basis"] == "prorate_probation":
                    entitlement = _prorate_annual(
                        start_date, probation_end, probation_days,
                        entitlement, year)

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
def _overlap_days_exact(req_start: date, req_end: date, period_start: date,
                        period_end: date, total_days: float) -> float:
    """Unrounded portion of a leave request's `days` inside a window.

    Kept separate from `_overlap_days` because rounding each portion to 4dp
    makes the portions of one request sum to slightly MORE than its `days`, and
    the allowance walk in `_allocate_excess_by_month` reads that drift as a
    real over-quota day.
    """
    lo = max(req_start, period_start)
    hi = min(req_end, period_end)
    if hi < lo:
        return 0.0
    span = (req_end - req_start).days + 1
    inside = (hi - lo).days + 1
    if inside >= span:
        return float(total_days)
    return total_days * inside / span


def _overlap_days(req_start: date, req_end: date, period_start: date,
                   period_end: date, total_days: float) -> float:
    """Portion of a leave request's `days` that falls in the payroll month.

    If the whole request lies inside the month → its full `days`. If it
    straddles the boundary → prorate by the inclusive calendar-day overlap
    fraction. Rounded to 4dp, which preserves every `days` value the app can
    actually produce (integers and half-days, and in fact anything at 4dp or
    coarser); only a value with more than 4 decimals would shift, by at most
    5e-5 of a day.
    """
    return round(
        _overlap_days_exact(req_start, req_end, period_start, period_end,
                            total_days),
        4,
    )


# Leave days below this are float noise from _split_by_month's 4dp rounding,
# not a real absence.
_DAY_EPS = 1e-6

# Maternity rows this far apart (or closer) belong to the SAME leave and share
# one paid cap; a bigger gap starts a new leave with a fresh cap. Put's call,
# 2026-08-10. The DB cannot tell "one pregnancy entered as several rows" from
# "two pregnancies", and the two need opposite treatment: sharing the cap across
# a calendar year underpaid a second pregnancy by up to 45 days of salary, while
# a per-row cap would let a leave split month-by-month escape the cap entirely.
# A pregnancy's rows are contiguous or days apart; two pregnancies are ~9 months
# apart, so the threshold only has to sit between those two scales.
_MATERNITY_EPISODE_GAP_DAYS = 30


def _maternity_episodes(requests):
    """Group maternity requests into distinct leaves — [[(start, end, days)…]…].

    `requests` may be in any order; episodes come back in date order, each
    holding the rows of one leave.
    """
    episodes = []
    running_end = None
    for req in sorted(requests, key=lambda q: (q[0], q[1])):
        start, end, _days = req
        if (running_end is not None
                and (start - running_end).days <= _MATERNITY_EPISODE_GAP_DAYS):
            episodes[-1].append(req)
            running_end = max(running_end, end)
        else:
            episodes.append([req])
            running_end = end
    return episodes


def _fmt_days(x: float) -> str:
    """Trim a day count for display: 31.0 → '31', 0.5 → '0.5'."""
    return f"{x:g}"


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _split_by_month(req_start: date, req_end: date, total_days: float):
    """One leave request's `days` as chronological [(year_month, portion)].

    Uses the same inclusive-calendar-day proration as `_overlap_days`, so a
    request sitting wholly inside one month keeps its exact `days` (half-days
    included) and a straddling one is divided by its day count in each month.
    Deliberately the UNROUNDED variant: the portions must sum back to `days`,
    or the allowance walk sees the rounding drift as an over-quota day.
    """
    out = []
    cursor = date(req_start.year, req_start.month, 1)
    while cursor <= req_end:
        last = date(cursor.year, cursor.month,
                    calendar.monthrange(cursor.year, cursor.month)[1])
        portion = _overlap_days_exact(req_start, req_end, cursor, last,
                                      total_days)
        if portion > 0:
            out.append((_month_key(cursor), portion))
        cursor = last + timedelta(days=1)
    return out


def _allocate_excess_by_month(requests, allowance: float) -> dict:
    """{year_month: days past `allowance`}, charged chronologically.

    `requests` is a chronologically ordered [(start, end, days)]. Each is split
    into its per-month portions, the portions are walked oldest-first, and the
    part of a month's portion that falls past the allowance is attributed to
    THAT month. So the allowance is consumed in date order and its excess is
    deducted exactly once, by the month whose own days crossed the line.

    This is the single mechanism behind both paid-leave allowances — the annual
    over-quota deduction and the maternity paid-day cap differ only in the
    population fed in and the size of the allowance. Charging the whole annual
    excess to every month that touched the leave type (and comparing the
    maternity cap against one month's days alone) were the two defects it
    replaced.

    `allowance` may be float('inf') for an unlimited type → nothing is unpaid.
    """
    units = []
    for req_start, req_end, days in requests:
        units.extend(_split_by_month(req_start, req_end, days))
    # Stable sort → strictly chronological across requests, while two requests
    # landing in the same month keep their own (start_date, id) order.
    units.sort(key=lambda u: u[0])

    used = 0.0
    out = {}
    for ym, portion in units:
        room = max(0.0, allowance - used)
        excess = portion - room
        # _split_by_month rounds each portion to 4dp, so the portions of a
        # straddling request can sum to `days` + 2e-4. Without the epsilon that
        # drift becomes a real deduction for someone who used EXACTLY their
        # quota (measured: 0.0001 days → ฿0.05 and a "เกินสิทธิ 0.0001 วัน" note
        # on the payslip).
        if excess > _DAY_EPS:
            out[ym] = round(out.get(ym, 0.0) + excess, 4)
        used += portion
    return out


def _compute_unpaid_days(c: sqlite3.Connection, employee_id: int,
                         year_month: str, period_start: date,
                         period_end: date):
    """Return (unpaid_days, note_fragments).

    Unpaid days in the month =
      - all UNPAID-type leave days in the month, PLUS
      - this month's share of paid-leave days past their allowance:
          * paid types (SICK / PERSONAL / ANNUAL …) → the calendar-year
            entitlement from `leave_balance`
          * MATERNITY                               → `leave_types.max_paid_days`
            (45), the employer-paid cap; the 98-day entitlement is the leave
            length, not the paid part.

    The share comes from `_allocate_excess_by_month`, so an allowance is
    consumed in date order and its excess is deducted once, in the month that
    actually crossed it.

    Both allowances are judged over a COHORT — the whole set of requests the
    allowance applies to — never over one payroll month's slice, and the excess
    is deducted in the month it actually falls in, even when that month belongs
    to the following calendar year. The two differ only in how the cohort is
    picked:
      * annual quota — the cohort is a calendar YEAR, and a request belongs to
        the year it STARTED in (exactly what `leave_balance` counts as `used`,
        so the deductions reconcile with the balance shown on /hr). A leave
        straddling 31 December is therefore judged against the quota it
        actually consumes, while its over-quota days are still deducted in
        January where they were taken.
      * maternity cap — the cohort is the leave itself, so it is every
        maternity request overlapping the year. A leave straddling 31 December
        keeps counting toward its 45 paid days instead of restarting, and a
        later pregnancy (a separate leave in a later year) gets its own 45.
        Several rows describing ONE maternity leave in the same year correctly
        share one 45-day cap.
    """
    y, _ = _parse_ym(year_month)
    year_start, year_end = date(y, 1, 1), date(y, 12, 31)

    _bal_cache = {}

    def _entitlement(code: str, year: int) -> float:
        """The `code` quota for `year` — the same figure /hr shows."""
        if year not in _bal_cache:
            _bal_cache[year] = leave_balance(employee_id, year, conn=c)
        return _bal_cache[year].get(code, {}).get("entitlement", float("inf"))

    types = {
        r["id"]: r
        for r in c.execute(
            "SELECT id, code, is_paid, max_paid_days FROM leave_types"
        ).fetchall()
    }
    type_by_code = {r["code"]: r for r in types.values()}

    reqs = c.execute(
        """SELECT leave_type_id, start_date, end_date, days
             FROM leave_requests
             WHERE employee_id = ? AND status = 'approved'
             ORDER BY start_date, id""",
        (employee_id,),
    ).fetchall()

    unpaid = 0.0
    notes = []
    by_code = {}

    for r in reqs:
        try:
            rs, re_ = _to_date(r["start_date"]), _to_date(r["end_date"])
        except ValueError:
            # A malformed date raises rather than returning None, so this MUST
            # catch: otherwise one bad legacy row 500s payroll generation for
            # every employee in the company. The write layer refuses these now
            # (hr_queries.validate_leave_payload); this covers rows that
            # predate it or were inserted directly.
            continue
        if rs is None or re_ is None or re_ < rs:
            continue
        days = float(r["days"] or 0)
        t = types[r["leave_type_id"]]
        code = t["code"]
        if not t["is_paid"]:
            # UNPAID type → unconditionally unpaid, no allowance involved.
            unpaid += _overlap_days(rs, re_, period_start, period_end, days)
            continue
        by_code.setdefault(code, []).append((rs, re_, days))

    for code in sorted(by_code):
        requests = by_code[code]
        # Only a leave touching THIS month can put unpaid days in it, so the
        # cohorts worth evaluating are the ones such a leave belongs to.
        touching = [q for q in requests
                    if q[0] <= period_end and q[1] >= period_start]
        if not touching:
            continue

        share = 0.0
        if code == "MATERNITY":
            allowance = float(type_by_code[code]["max_paid_days"] or 0) or float("inf")
            if allowance != float("inf"):
                # One cap per LEAVE. Not per calendar year (that made a second
                # pregnancy share the first one's 45 days) and not per row
                # (that let a leave split across rows escape the cap).
                for episode in _maternity_episodes(requests):
                    if not any(q[0] <= period_end and q[1] >= period_start
                               for q in episode):
                        continue
                    share += _allocate_excess_by_month(
                        episode, allowance).get(year_month, 0.0)
        else:
            for cohort_year in sorted({q[0].year for q in touching}):
                allowance = _entitlement(code, cohort_year)
                if allowance == float("inf"):
                    continue
                cohort = [q for q in requests if q[0].year == cohort_year]
                share += _allocate_excess_by_month(cohort, allowance).get(
                    year_month, 0.0)

        if share <= _DAY_EPS:
            continue
        unpaid += share
        if code == "MATERNITY":
            notes.append(
                f"ลาคลอดเกิน {allowance:g} วันที่จ่าย → หัก {share:g} วัน"
            )
        else:
            notes.append(
                f"{code} เกินสิทธิ {share:g} วัน → หักเป็นลาไม่รับค่าจ้าง"
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
    # A month cannot deduct more than it pays. unpaid_days counts CALENDAR days
    # (31 in a 31-day month) while the deduction divides by the fixed 30-day
    # divisor that also caps base_amount — so an unclamped full month of unpaid
    # leave deducted 31/30 of salary (฿15,500 against a ฿15,000 base). The
    # worst honest case is a zero-wage month.
    #
    # The cap is on the MONEY, not on unpaid_days: the day count stays a true
    # record of the absence, so it still reconciles with the leave request and
    # with the allocation total (Put's call, 2026-08-06). That does mean
    # deduction < unpaid_days × daily rate on a capped month, which would read
    # as an arithmetic error on the payslip — hence the note.
    uncapped_deduction = round(rate / divisor * unpaid_days, 2)
    unpaid_deduction = min(uncapped_deduction, base_amount)
    if uncapped_deduction > unpaid_deduction:
        unpaid_notes.append(
            f"ขาดงานทั้งเดือน — หักได้ไม่เกินฐานเงินเดือนของเดือนนี้ "
            f"({_fmt_days(unpaid_days)} วัน แต่หักเพียง {divisor:g} วัน)"
        )

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


# ── regenerate blast-radius guard ────────────────────────────────────────────
# ONE definition of "who belongs on this run", shared by generate_run and the
# reopen guard, so the two cannot drift apart (a second copy is exactly how the
# check would silently stop matching what the DELETE+INSERT actually does).
def _active_employees_for_month(c, company_id: int, period_start, period_end):
    """Employees of `company_id` on payroll during the month — the set
    generate_run rebuilds payroll_items from."""
    return c.execute(
        """SELECT * FROM employees
             WHERE company_id = ? AND is_active = 1 AND on_payroll = 1
               AND (start_date IS NULL OR start_date <= ?)
               AND (end_date   IS NULL OR end_date   >= ?)""",
        (company_id, period_end.isoformat(), period_start.isoformat()),
    ).fetchall()


def _regenerate_would_drop(c, run_id: int, active_ids):
    """employee_ids holding a payroll_items row in this run that the CURRENT
    active set no longer contains.

    generate_run rebuilds a run with `DELETE FROM payroll_items WHERE run_id=?`
    followed by an INSERT per active employee. So anyone who has since gone
    `is_active=0` / `on_payroll=0` (or acquired an end_date) loses their row for
    that month — and the orphaned-advance reconcile at the end of generate_run
    then un-stamps their advances, releasing them into a future run.

    2026-08-05 (why this exists): measured on a snapshot of prod, regenerating
    April 2026 deleted a departed employee's row, un-stamped her ฿400 advance,
    and added three people who were never on that month's payroll. The reopen
    button that leads here is live in the UI for any run with no posted payment
    rows.
    """
    existing = {r["employee_id"] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id = ?", (run_id,)
    )}
    return existing - set(active_ids)


def _dropped_employees_message(c, dropped_ids) -> str:
    rows = c.execute(
        "SELECT id, COALESCE(nickname, full_name) AS name FROM employees"
        f" WHERE id IN ({','.join('?' * len(dropped_ids))})",
        tuple(sorted(dropped_ids)),
    ).fetchall()
    names = ", ".join(f"{r['name']} (id {r['id']})" for r in rows) or \
        ", ".join(str(i) for i in sorted(dropped_ids))
    return (
        f"สร้างรอบนี้ใหม่ไม่ได้ — มีพนักงาน {len(dropped_ids)} คนที่มีรายการอยู่ในรอบนี้ "
        f"แต่ไม่อยู่ในชุดพนักงานปัจจุบันแล้ว ({names}) "
        f"การสร้างใหม่จะลบรายการเงินเดือนของเขาในเดือนนี้ทิ้ง "
        f"และปลดการหักเบิกล่วงหน้าไปงวดอื่น "
        f"· ถ้าเขาลาออกกลางเดือน ให้ใส่วันสิ้นสุดการทำงาน (end_date) แทนการปิดใช้งาน "
        f"ระบบจะคงเขาไว้ในรอบนี้และคิดเงินตามสัดส่วนวันที่ทำงานให้ "
        f"· ถ้าตั้งใจจะเอาเขาออกจากรอบนี้จริง ต้องแก้ที่ฐานข้อมูลโดยตรงและสำรองข้อมูลก่อน"
    )


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
        emps = _active_employees_for_month(c, company_id, period_start, period_end)

        # Refuse rather than silently erase history. Raised BEFORE any mutation
        # and before any commit, so _ConnCtx closing the connection rolls back a
        # freshly-inserted payroll_runs row (an empty new run has no items, so
        # this can only fire on a REgenerate).
        dropped = _regenerate_would_drop(c, run_id, {e["id"] for e in emps})
        if dropped:
            raise ValueError(_dropped_employees_message(c, dropped))

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

    Refuses when the item's parent run is finalized. The route gates that too,
    but this is the invariant that keeps an issued payslip agreeing with the
    approved payroll (and with any cashbook payment already posted against it),
    so it must hold for every caller — not only the one that goes through the
    URL. Same defense-in-depth as post_salary_payment's finalized check.
    """
    with _ConnCtx(conn, db_path) as c:
        row = c.execute(
            """SELECT pi.*, pr.status AS run_status
                 FROM payroll_items pi
                 JOIN payroll_runs pr ON pr.id = pi.run_id
                WHERE pi.id = ?""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"payroll_items id {item_id} not found")
        d = dict(row)
        if d.pop("run_status") == "finalized":
            raise ValueError(
                "ไม่สามารถแก้ไขรายการในรอบเงินเดือนที่ finalized แล้ว — "
                "ต้อง reopen รอบนี้ก่อนจึงจะแก้ไขได้"
            )

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
        paid_count = c.execute(
            "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchone()[0]
        if paid_count:
            # Never edit a salary figure while its cash is already posted —
            # the admin must ยกเลิกการจ่าย every paid item first (Phase 4).
            raise ValueError(
                f"มี {paid_count} รายการจ่ายเงินเดือนที่บันทึกไว้แล้ว — "
                f"กรุณายกเลิกการจ่ายทั้งหมดก่อนจึงจะ reopen ได้"
            )

        # Stop at the door. Reopening is only ever a prelude to regenerating,
        # and generate_run refuses when that would drop a departed employee's
        # row — so checking here too avoids stranding the run in 'draft' with
        # finalized figures, a state nothing else repairs.
        period_start, period_end = _month_bounds(run["year_month"])
        dropped = _regenerate_would_drop(
            c, run_id,
            {e["id"] for e in _active_employees_for_month(
                c, run["company_id"], period_start, period_end)},
        )
        if dropped:
            raise ValueError(_dropped_employees_message(c, dropped))

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


# ── salary pay-event posting (ADR 0006) ──────────────────────────────────────
def post_salary_payment(item_id: int, account_id: int,
                        pay_date: Optional[str], actor: str,
                        conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None) -> int:
    """Post the per-employee salary pay-event ("จ่ายแล้ว") — ONE linked
    `cashbook_transactions` expense row for `payroll_items.id = item_id`.

    Paid-state is DERIVED from this row's existence (no `paid` column), so
    this function is the single write path and must stay idempotent.

    Rejects (raises ValueError, Thai message) when:
      - the item's net_pay <= 0 (nothing to transfer)
      - a cashbook row is already linked to this item (no double-post)
      - the chosen account is inactive or is_transfer=1 (would hide the
        expense from the cashbook P&L — see cashbook.py dashboard exclusion)

    Returns the new cashbook_transactions.id.
    """
    with _ConnCtx(conn, db_path) as c:
        item = c.execute(
            "SELECT * FROM payroll_items WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise ValueError(f"ไม่พบรายการ payroll_items id {item_id}")

        if item["net_pay"] <= 0:
            raise ValueError("เงินสุทธิ <= 0 — ไม่มียอดโอน ไม่สามารถบันทึกการจ่ายได้")

        existing = c.execute(
            "SELECT id FROM cashbook_transactions WHERE payroll_item_id = ?",
            (item_id,),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"รายการนี้ถูกบันทึกจ่ายไปแล้ว (cashbook id {existing['id']}) — "
                f"ต้องยกเลิกการจ่ายก่อนจึงจะบันทึกใหม่ได้"
            )

        account = c.execute(
            "SELECT * FROM cashbook_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None or account["is_active"] != 1:
            raise ValueError("บัญชีที่เลือกไม่ถูกต้องหรือถูกปิดใช้งานแล้ว")
        if account["is_transfer"] == 1:
            raise ValueError("ไม่สามารถจ่ายเงินเดือนเข้าบัญชีประเภทเงินโอนได้")

        run = c.execute(
            "SELECT year_month, status FROM payroll_runs WHERE id = ?",
            (item["run_id"],),
        ).fetchone()
        if run is None:
            raise ValueError("ไม่พบรอบเงินเดือนของรายการนี้")
        # Defense-in-depth: the route already gates finalized, but the invariant
        # "paid rows only on finalized runs" (which keeps generate_run's item
        # DELETE off referenced rows) must hold for any caller.
        if run["status"] != "finalized":
            raise ValueError("บันทึกการจ่ายได้เฉพาะรอบเงินเดือนที่ finalized แล้วเท่านั้น")
        year_month = run["year_month"]

        emp = c.execute(
            "SELECT nickname, full_name FROM employees WHERE id = ?",
            (item["employee_id"],),
        ).fetchone()
        display_name = (emp["nickname"] or emp["full_name"]) if emp else ""

        txn_date = pay_date or date.today().isoformat()
        description = f"เงินเดือน {year_month} — {display_name}"

        try:
            cur = c.execute(
                """INSERT INTO cashbook_transactions
                     (account_id, txn_date, direction, category, amount,
                      user_category, description, created_by,
                      payroll_run_id, payroll_item_id)
                   VALUES (?, ?, 'expense', 'เงินเดือน', ?, ?, ?, ?, ?, ?)""",
                (account_id, txn_date, item["net_pay"], display_name, description,
                 actor, item["run_id"], item_id),
            )
        except sqlite3.IntegrityError:
            # UNIQUE(payroll_item_id) tripped — a concurrent request already
            # posted this item (check-then-insert race under gunicorn -w 2 or a
            # double-submit). Surface as ValueError so the route flashes instead
            # of 500ing. The DB constraint, not this Python check, is the real
            # idempotency guarantee.
            raise ValueError(
                "รายการนี้ถูกบันทึกจ่ายไปแล้ว — ต้องยกเลิกการจ่ายก่อนจึงจะบันทึกใหม่ได้"
            )
        txn_id = cur.lastrowid
        # Attribute the cash-out to the actor: the mig-076 AFTER INSERT trigger
        # stamps user=NULL, so mirror void_salary_payment's explicit audit row.
        c.execute(
            """INSERT INTO audit_log (table_name, row_id, action, changed_fields, user)
                 VALUES ('cashbook_transactions', ?, 'INSERT', ?, ?)""",
            (txn_id,
             json.dumps({"payroll_item_id": item_id, "amount": item["net_pay"],
                         "account_id": account_id}, ensure_ascii=False),
             actor),
        )
        c.commit()
        return txn_id


def void_salary_payment(item_id: int, actor: str,
                        conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None) -> None:
    """Void a salary pay-event ("ยกเลิกการจ่าย") — deletes the linked
    `cashbook_transactions` row for this payroll item (the paid-state source
    of truth). No-op if no row is linked."""
    with _ConnCtx(conn, db_path) as c:
        row = c.execute(
            "SELECT id FROM cashbook_transactions WHERE payroll_item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return
        txn_id = row["id"]
        c.execute("DELETE FROM cashbook_transactions WHERE id = ?", (txn_id,))
        c.execute(
            """INSERT INTO audit_log (table_name, row_id, action, changed_fields, user)
                 VALUES ('cashbook_transactions', ?, 'DELETE', ?, ?)""",
            (txn_id,
             json.dumps({"payroll_item_id": item_id}, ensure_ascii=False),
             actor),
        )
        c.commit()
