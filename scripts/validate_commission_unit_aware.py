"""Validate commission engine post-fix vs historical payouts.

Read-only. Walks every month that has commission_payouts rows, computes the
post-fix commission via get_commission_for_month(), and diffs it against the
actual amount_paid. Surfaces months/SPs where the previously-paid amount
deviates from the corrected calculation — i.e., where the by-code JOIN
duplication / NULL brand_kind bugs likely caused over/underpayment.

Run:
    cd sendy_erp && ~/.virtualenvs/erp/bin/python scripts/validate_commission_unit_aware.py

Output:
  1. Month-level summary (sum across all SPs that had payouts)
  2. Per-(SP, month) rows where |delta| >= 0.50 baht (skips rounding noise)
  3. Split-code impact: which 7 codes drove the duplication and by how much

Interpretation:
  - delta > 0  → paid > calculated → SP was OVERPAID by this much
  - delta < 0  → paid < calculated → SP was UNDERPAID (less common)
  - delta = 0  → unaffected (most months)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'inventory_app'))

import commission   # noqa: E402

DB = os.path.join(_HERE, '..', 'inventory_app', 'instance', 'inventory.db')
TOLERANCE = 0.50  # ignore rounding-level deltas below this


def _months_with_payouts(conn):
    return [r[0] for r in conn.execute(
        'SELECT DISTINCT year_month FROM commission_payouts ORDER BY year_month'
    ).fetchall()]


def _payouts_per_sp(conn, ym):
    return {r[0]: r[1] for r in conn.execute(
        """
        SELECT salesperson_code, ROUND(SUM(amount_paid), 2)
          FROM commission_payouts
         WHERE year_month = ?
         GROUP BY salesperson_code
        """,
        (ym,)
    ).fetchall()}


def _split_codes(conn):
    """Return (bsn_code, mapping_count, affected_sales_rows, total_net_at_risk)."""
    rows = conn.execute(
        """
        SELECT m.bsn_code, COUNT(*) AS n
          FROM product_code_mapping m
         WHERE m.product_id IS NOT NULL
         GROUP BY m.bsn_code
        HAVING n > 1
        """
    ).fetchall()
    out = []
    for code, n in rows:
        net = conn.execute(
            """
            SELECT ROUND(SUM(es.net), 2)
              FROM express_sales es
              JOIN express_payment_in_invoice_refs ref ON ref.invoice_no = es.doc_no
              JOIN express_payments_in pin ON pin.id = ref.payment_in_id
             WHERE es.product_code = ?
               AND pin.is_void = 0
               AND pin.salesperson_code <> ''
            """,
            (code,)
        ).fetchone()[0] or 0
        affected = conn.execute(
            """
            SELECT COUNT(DISTINCT es.id)
              FROM express_sales es
              JOIN express_payment_in_invoice_refs ref ON ref.invoice_no = es.doc_no
              JOIN express_payments_in pin ON pin.id = ref.payment_in_id
             WHERE es.product_code = ?
               AND pin.is_void = 0
               AND pin.salesperson_code <> ''
            """,
            (code,)
        ).fetchone()[0]
        out.append((code, n, affected, net))
    return out


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    months = _months_with_payouts(conn)
    print(f'Scanning {len(months)} months with recorded payouts '
          f'({months[0]} → {months[-1]})\n')

    # ── 1. month-level summary ─────────────────────────────────────────────
    print('═' * 80)
    print('MONTH-LEVEL SUMMARY')
    print('═' * 80)
    print(f'{"Month":<10} {"Calc (post-fix)":>18} {"Paid (historic)":>18} {"Delta":>12}')
    print('-' * 60)

    monthly_calc = {}
    monthly_paid = {}
    sp_deltas = []  # (delta, ym, sp, calc, paid)

    for ym in months:
        result = commission.get_commission_for_month(ym, db_path=DB)
        calc_by_sp = {r['salesperson_code']: r['total_commission'] for r in result}
        paid_by_sp = _payouts_per_sp(conn, ym)

        total_calc = round(sum(calc_by_sp.values()), 2)
        total_paid = round(sum(paid_by_sp.values()), 2)
        delta = round(total_paid - total_calc, 2)

        monthly_calc[ym] = total_calc
        monthly_paid[ym] = total_paid

        marker = '  ⚠' if abs(delta) >= TOLERANCE else ''
        print(f'{ym:<10} {total_calc:>18,.2f} {total_paid:>18,.2f} {delta:>+12,.2f}{marker}')

        # Collect per-SP deltas where it matters
        sps = set(calc_by_sp) | set(paid_by_sp)
        for sp in sps:
            c = calc_by_sp.get(sp, 0.0)
            p = paid_by_sp.get(sp, 0.0)
            d = round(p - c, 2)
            if abs(d) >= TOLERANCE:
                sp_deltas.append((d, ym, sp, c, p))

    total_c = round(sum(monthly_calc.values()), 2)
    total_p = round(sum(monthly_paid.values()), 2)
    total_d = round(total_p - total_c, 2)
    print('-' * 60)
    print(f'{"TOTAL":<10} {total_c:>18,.2f} {total_p:>18,.2f} {total_d:>+12,.2f}')

    # ── 2. per-(SP, month) detail ──────────────────────────────────────────
    print()
    print('═' * 80)
    print(f'PER-SP DELTAS (|delta| >= ฿{TOLERANCE}, sorted by largest overpayment)')
    print('═' * 80)
    if not sp_deltas:
        print('(no material deltas — fix has no effect on past payouts)')
    else:
        sp_deltas.sort(key=lambda r: -r[0])
        print(f'{"Month":<10} {"SP":<5} {"Calc":>12} {"Paid":>12} {"Delta":>12}  {"Sign":<10}')
        print('-' * 70)
        sp_summary = defaultdict(lambda: [0.0, 0])  # sp -> [total_delta, count]
        for d, ym, sp, c, p in sp_deltas:
            sign = 'OVERPAID' if d > 0 else 'underpaid'
            print(f'{ym:<10} {sp:<5} {c:>12,.2f} {p:>12,.2f} {d:>+12,.2f}  {sign}')
            sp_summary[sp][0] += d
            sp_summary[sp][1] += 1

        print('-' * 70)
        print('PER-SP CUMULATIVE')
        print(f'{"SP":<5} {"Months":>8} {"Cumulative Δ":>16}')
        for sp, (td, n) in sorted(sp_summary.items(), key=lambda kv: -kv[1][0]):
            print(f'{sp:<5} {n:>8} {td:>+16,.2f}')

    # ── 3. split-code impact ──────────────────────────────────────────────
    print()
    print('═' * 80)
    print('SPLIT CODES (root cause — these had multiple mapping rows)')
    print('═' * 80)
    sc = _split_codes(conn)
    if not sc:
        print('(none — mapping table is currently flat)')
    else:
        print(f'{"Code":<14} {"#Maps":>6} {"Paid sales rows":>16} {"Σ net (THB)":>14}')
        print('-' * 56)
        for code, n, affected, net in sorted(sc, key=lambda r: -r[3]):
            print(f'{code:<14} {n:>6} {affected:>16} {net:>14,.2f}')
        tot_net = sum(r[3] for r in sc)
        tot_aff = sum(r[2] for r in sc)
        print('-' * 56)
        print(f'{"TOTAL":<14} {"":>6} {tot_aff:>16} {tot_net:>14,.2f}')

    conn.close()
    print()
    print('Done. Investigate any month with |delta| ≥ ฿0.50 via /commission?ym=YYYY-MM')


if __name__ == '__main__':
    main()
