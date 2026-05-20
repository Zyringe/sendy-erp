"""Isolate the issue-#30 commission delta (pre-fix vs post-fix).

Runs commission.get_commission_for_month() TWICE against the current DB:
once with the pre-fix by-code JOIN, once with the post-fix unit-aware
resolver. Diff = pure issue-#30 impact, with all other variables held
constant (brand_kind values, overrides, tier rates, payouts table).

Unlike validate_commission_unit_aware.py (which diffs post-fix calc vs
historic payouts — many confounders over 2.5 years), this gives a clean
answer: "how much commission did the by-code duplication bug add per
(SP, month)?"

Run:
    cd sendy_erp && ~/.virtualenvs/erp/bin/python scripts/isolate_issue30_impact.py

Output:
  Per-(SP, month) rows where |buggy − fixed| >= ฿0.50, sorted by largest
  buggy-over-fixed (i.e. SPs who would have been OVERPAID by the bug).

Caveats:
  - Brand_kind on express_sales rows is the CURRENT (post-mig-063/064)
    value. The buggy query reads the same brand_kind column, so this
    diff isolates ONLY the by-code JOIN duplication, not the historical
    brand_kind drift caused by the pre-063 per-code trigger.
  - import_express.py fix is unrelated (affects future imports only).
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'inventory_app'))

import commission   # noqa: E402

DB = os.path.join(_HERE, '..', 'inventory_app', 'instance', 'inventory.db')
TOLERANCE = 0.50


# The PRE-FIX query — by-code JOIN that duplicates each sale line by the
# number of product_code_mapping rows. Verbatim from commission.py before
# the issue-#30 fix.
_BUGGY_QUERY = """
    SELECT rcv.salesperson_code,
           rcv.receipt_no,
           rcv.receipt_date,
           rcv.customer_name,
           rcv.invoice_no,
           rcv.ref_amount,
           es.product_code,
           es.product_name_raw,
           es.brand_kind         AS brand_kind,
           es.net                AS line_net,
           es.total              AS line_total,
           es.qty                AS qty,
           es.unit_price         AS unit_price,
           pcm.product_id        AS sendy_product_id,
           p.brand_id            AS sendy_brand_id,
           b.code                AS sendy_brand_code,
           b.name                AS sendy_brand_name
      FROM (
          SELECT pin.salesperson_code,
                 ref.invoice_no,
                 MIN(pin.doc_no)        AS receipt_no,
                 MIN(pin.date_iso)      AS receipt_date,
                 MIN(pin.customer_name) AS customer_name,
                 SUM(ref.amount)        AS ref_amount
            FROM express_payments_in pin
            JOIN express_payment_in_invoice_refs ref ON ref.payment_in_id = pin.id
           WHERE pin.is_void = 0
             AND pin.date_iso BETWEEN ? AND ?
             AND pin.salesperson_code <> ''
           GROUP BY pin.salesperson_code, ref.invoice_no
      ) rcv
      JOIN express_sales          es   ON es.doc_no = rcv.invoice_no
      LEFT JOIN product_code_mapping pcm ON pcm.bsn_code = es.product_code
      LEFT JOIN products          p    ON p.id = pcm.product_id
      LEFT JOIN brands            b    ON b.id = p.brand_id
     WHERE 1=1
"""


def _months_with_payouts(conn):
    return [r[0] for r in conn.execute(
        'SELECT DISTINCT year_month FROM commission_payouts ORDER BY year_month'
    ).fetchall()]


def _run_with_query(query_sql, ym):
    """Monkey-patch commission._BASE_QUERY, run get_commission_for_month,
    return {sp: total_commission}. Restores the original query after."""
    orig = commission._BASE_QUERY
    try:
        commission._BASE_QUERY = query_sql
        result = commission.get_commission_for_month(ym, db_path=DB)
    finally:
        commission._BASE_QUERY = orig
    return {r['salesperson_code']: r['total_commission'] for r in result}


def main():
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    months = _months_with_payouts(conn)
    print(f'Running both query versions across {len(months)} months '
          f'({months[0]} → {months[-1]})\n')

    # Sanity check: confirm post-fix query in commission.py matches the
    # "fixed" path we expect. If someone reverts later this catches it.
    if 'product_code_mapping m' not in commission._BASE_QUERY:
        print('WARNING: commission._BASE_QUERY does not look post-fix; '
              'expected the unit-aware resolver subquery.')

    rows = []  # (delta, ym, sp, buggy, fixed)
    monthly_buggy = 0.0
    monthly_fixed = 0.0

    for ym in months:
        buggy = _run_with_query(_BUGGY_QUERY, ym)
        fixed = _run_with_query(commission._BASE_QUERY, ym)
        sps = set(buggy) | set(fixed)
        for sp in sps:
            b = buggy.get(sp, 0.0)
            f = fixed.get(sp, 0.0)
            d = round(b - f, 2)
            monthly_buggy += b
            monthly_fixed += f
            if abs(d) >= TOLERANCE:
                rows.append((d, ym, sp, b, f))

    # ── Output ──────────────────────────────────────────────────────────
    print('═' * 80)
    print('CUMULATIVE (all 28 months)')
    print('═' * 80)
    print(f'Buggy total commission  : {monthly_buggy:>14,.2f}')
    print(f'Fixed total commission  : {monthly_fixed:>14,.2f}')
    print(f'Bug overstated by       : {monthly_buggy - monthly_fixed:>+14,.2f}')

    print()
    print('═' * 80)
    print(f'PER-(SP, MONTH) WHERE BUG MATTERED (|delta| >= ฿{TOLERANCE})')
    print('═' * 80)
    if not rows:
        print('(no material deltas — issue-#30 bug had no effect on past months)')
    else:
        rows.sort(key=lambda r: -r[0])  # largest overstatement first
        print(f'{"Month":<10} {"SP":<5} {"Buggy":>12} {"Fixed":>12} {"Δ":>12}')
        print('-' * 55)
        sp_totals = defaultdict(lambda: [0.0, 0])
        for d, ym, sp, b, f in rows:
            print(f'{ym:<10} {sp:<5} {b:>12,.2f} {f:>12,.2f} {d:>+12,.2f}')
            sp_totals[sp][0] += d
            sp_totals[sp][1] += 1

        print('-' * 55)
        print('PER-SP CUMULATIVE (bug-caused overstatement)')
        print(f'{"SP":<5} {"Months":>8} {"Cumulative bug Δ":>20}')
        for sp, (td, n) in sorted(sp_totals.items(), key=lambda kv: -kv[1][0]):
            print(f'{sp:<5} {n:>8} {td:>+20,.2f}')

    conn.close()
    print()
    print('Done. This is the PURE issue-#30 impact — '
          'the validate_commission_unit_aware.py wider delta has other causes.')


if __name__ == '__main__':
    main()
