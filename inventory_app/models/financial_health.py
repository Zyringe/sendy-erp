"""Financial-health "pace panel" — v1 money math.

See `projects/financial-health-page/design.md` (repo root) for the full
design + locked decisions (Q11, S1, S2). This module answers ONE question —
"is the CURRENT month on pace to cover its fixed costs?" — via a break-even
pace check. It is NOT a P&L and does NOT project/forecast (S1: a mid-month
projection can't flip a verdict that's never close to the line). It does NOT
show cash/runway (S2: no per-account opening balance exists to derive that).

Kept separate from `models/accounting.py` (the existing, pre-existing-bug
`/accounting` P&L) — v1 does not touch that page's computation.
"""
import calendar
import statistics
from datetime import date
from typing import Optional

from database import get_connection

# กรรมการ + ผู้ถือหุ้น ที่ไม่นับใน "floor" (ขั้นต่ำไม่รวมเจ้าของ) — Put + แม่.
# Documented constant per design.md Q11 — NOT inferred from role/table data.
OWNER_EMP_CODES = ('EMP001', 'EMP008')  # Put + แม่ (กรรมการ + ผู้ถือหุ้น)

# Cashbook expense categories excluded from "operating expense" — mirrors
# the "Expense formula (opex, closed-month)" in design.md / blueprints/cashbook.py's
# TRANSFER_CATEGORIES convention. เงินทุน/เงินโอน = transfer, ซื้อสินค้า = already COGS.
_NON_OPEX_CATEGORIES = ('เงินทุน/เงินโอน', 'ซื้อสินค้า')
# "non-salary" further excludes เงินเดือน (salary is costed deterministically
# from employee_salary_history instead — avoids cashbook timing noise).
_SALARY_CATEGORY = 'เงินเดือน'

_THAI_MONTH_FULL = {
    1: 'มกราคม', 2: 'กุมภาพันธ์', 3: 'มีนาคม', 4: 'เมษายน', 5: 'พฤษภาคม',
    6: 'มิถุนายน', 7: 'กรกฎาคม', 8: 'สิงหาคม', 9: 'กันยายน', 10: 'ตุลาคม',
    11: 'พฤศจิกายน', 12: 'ธันวาคม',
}
_THAI_MONTH_ABBR = {
    1: 'ม.ค.', 2: 'ก.พ.', 3: 'มี.ค.', 4: 'เม.ย.', 5: 'พ.ค.', 6: 'มิ.ย.',
    7: 'ก.ค.', 8: 'ส.ค.', 9: 'ก.ย.', 10: 'ต.ค.', 11: 'พ.ย.', 12: 'ธ.ค.',
}


def _thai_month_label(year, month, abbr=False):
    if abbr:
        return _THAI_MONTH_ABBR[month]
    return f'{_THAI_MONTH_FULL[month]} {year + 543}'


def _month_bounds(year, month):
    """('YYYY-MM-01', 'YYYY-MM-<last day>') for the given calendar month."""
    last_day = calendar.monthrange(year, month)[1]
    return f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{last_day:02d}'


def _trailing_month_starts(as_of, n=3):
    """(year, month) tuples for the n COMPLETE calendar months immediately
    before as_of's month, oldest first. E.g. as_of=2026-07-15, n=3 →
    [(2026,4), (2026,5), (2026,6)]."""
    months = []
    y, m = as_of.year, as_of.month
    for i in range(n, 0, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append((yy, mm))
    return months


def get_trailing_months(n=3, conn=None, as_of_date=None):
    """Revenue (Σnet) for the n complete calendar months before the current
    one, oldest first.

    Returns: [{'year', 'month', 'month_label', 'revenue'}, ...]
    """
    own_conn = conn is None
    conn = conn or get_connection()
    as_of = as_of_date or date.today()
    try:
        out = []
        for (y, m) in _trailing_month_starts(as_of, n):
            d_from, d_to = _month_bounds(y, m)
            row = conn.execute(
                """SELECT COALESCE(SUM(net), 0) AS rev
                     FROM sales_transactions
                    WHERE date_iso >= ? AND date_iso <= ?""",
                (d_from, d_to)).fetchone()
            out.append({
                'year': y,
                'month': m,
                'month_label': _thai_month_label(y, m, abbr=True),
                'revenue': float(row['rev'] or 0),
            })
        return out
    finally:
        if own_conn:
            conn.close()


def get_current_month_pace(as_of_date=None, conn=None):
    """Month-to-date revenue for the CURRENT calendar month, plus the real
    data freshness (MAX(date_iso) across ALL sales_transactions — not
    windowed to the current month, so a stale import shows up immediately).

    Returns: {'month_label', 'day_of_month', 'days_in_month', 'mtd_revenue',
              'data_as_of'}
    """
    own_conn = conn is None
    conn = conn or get_connection()
    as_of = as_of_date or date.today()
    try:
        y, m = as_of.year, as_of.month
        days_in_month = calendar.monthrange(y, m)[1]
        month_start, month_end = _month_bounds(y, m)
        cutoff = min(as_of.isoformat(), month_end)

        row = conn.execute(
            """SELECT COALESCE(SUM(net), 0) AS rev
                 FROM sales_transactions
                WHERE date_iso >= ? AND date_iso <= ?""",
            (month_start, cutoff)).fetchone()
        mtd_revenue = float(row['rev'] or 0)

        fresh = conn.execute(
            "SELECT MAX(date_iso) AS mx FROM sales_transactions").fetchone()
        data_as_of = fresh['mx'] if fresh else None

        return {
            'month_label': _thai_month_label(y, m),
            'day_of_month': as_of.day,
            'days_in_month': days_in_month,
            'mtd_revenue': mtd_revenue,
            'data_as_of': data_as_of,
        }
    finally:
        if own_conn:
            conn.close()


def _trailing_margin(conn, as_of):
    """(Σnet − Σqty*cost_price) / Σnet over the 3 complete trailing months.
    Returns None when there is no trailing revenue (can't divide, and 0%
    would misleadingly read as "sells at cost" rather than "no data")."""
    months = _trailing_month_starts(as_of, 3)
    d_from = _month_bounds(*months[0])[0]
    d_to = _month_bounds(*months[-1])[1]
    row = conn.execute(
        """SELECT COALESCE(SUM(st.net), 0) AS rev,
                  COALESCE(SUM(st.qty * COALESCE(p.cost_price, 0)), 0) AS cogs
             FROM sales_transactions st
             LEFT JOIN products p ON p.id = st.product_id
            WHERE st.date_iso >= ? AND st.date_iso <= ?""",
        (d_from, d_to)).fetchone()
    rev = float(row['rev'] or 0)
    cogs = float(row['cogs'] or 0)
    if rev <= 0:
        return None
    return (rev - cogs) / rev


def _latest_salaries(conn, as_of_iso):
    """{emp_code: monthly_salary} for active, on_payroll employees, using
    each employee's most recent employee_salary_history row effective on or
    before as_of (a future-dated raise must not apply early)."""
    rows = conn.execute(
        """SELECT e.emp_code, esh.monthly_salary
             FROM employees e
             JOIN employee_salary_history esh ON esh.employee_id = e.id
            WHERE e.is_active = 1 AND e.on_payroll = 1
              AND esh.effective_date = (
                    SELECT MAX(esh2.effective_date)
                      FROM employee_salary_history esh2
                     WHERE esh2.employee_id = e.id
                       AND esh2.effective_date <= ?
              )""",
        (as_of_iso,)).fetchall()
    return {r['emp_code']: float(r['monthly_salary'] or 0) for r in rows}


def _trailing_overhead(conn, as_of):
    """MEDIAN of trailing-3-complete-months non-salary operating expense.
    Non-salary opex = cashbook_transactions.amount WHERE direction='expense'
    AND account.is_transfer=0 AND category NOT IN (transfer, COGS, salary).
    """
    months = _trailing_month_starts(as_of, 3)
    excluded = _NON_OPEX_CATEGORIES + (_SALARY_CATEGORY,)
    placeholders = ','.join('?' * len(excluded))
    totals = []
    for (y, m) in months:
        d_from, d_to = _month_bounds(y, m)
        row = conn.execute(
            f"""SELECT COALESCE(SUM(ct.amount), 0) AS total
                  FROM cashbook_transactions ct
                  JOIN cashbook_accounts ca ON ca.id = ct.account_id
                 WHERE ct.direction = 'expense'
                   AND ca.is_transfer = 0
                   AND ct.txn_date >= ? AND ct.txn_date <= ?
                   AND COALESCE(ct.category, '') NOT IN ({placeholders})""",
            (d_from, d_to, *excluded)).fetchone()
        totals.append(float(row['total'] or 0))
    return statistics.median(totals) if totals else 0.0


def get_break_even(conn=None, as_of_date=None):
    """Break-even pace numbers for the CURRENT month, per design.md Q11.

    Returns: {
      'margin':            trailing-3-mo gross margin (0..1) or None,
      'salary_floor':      Σ latest salary, active on_payroll, EXCLUDING owners,
      'salary_full':       Σ latest salary, active on_payroll, INCLUDING owners,
      'overhead':          MEDIAN trailing-3-mo non-salary opex,
      'fixed_base_floor':  salary_floor + overhead,
      'fixed_base_full':   salary_full + overhead,
      'break_even_floor':  fixed_base_floor / margin, or None if margin<=0/None,
      'break_even_full':   fixed_base_full / margin, or None if margin<=0/None,
      'trailing_months':   [{'year','month','month_label','revenue'}, ...],
    }
    """
    own_conn = conn is None
    conn = conn or get_connection()
    as_of = as_of_date or date.today()
    try:
        margin = _trailing_margin(conn, as_of)

        salaries = _latest_salaries(conn, as_of.isoformat())
        salary_full = sum(salaries.values())
        salary_floor = sum(
            v for code, v in salaries.items() if code not in OWNER_EMP_CODES)

        overhead = _trailing_overhead(conn, as_of)

        fixed_base_floor = salary_floor + overhead
        fixed_base_full = salary_full + overhead

        if margin is None or margin <= 0:
            break_even_floor = None
            break_even_full = None
        else:
            break_even_floor = fixed_base_floor / margin
            break_even_full = fixed_base_full / margin

        trailing_months = get_trailing_months(3, conn=conn, as_of_date=as_of)

        return {
            'margin': margin,
            'salary_floor': salary_floor,
            'salary_full': salary_full,
            'overhead': overhead,
            'fixed_base_floor': fixed_base_floor,
            'fixed_base_full': fixed_base_full,
            'break_even_floor': break_even_floor,
            'break_even_full': break_even_full,
            'trailing_months': trailing_months,
        }
    finally:
        if own_conn:
            conn.close()
