"""CLI helper to view commission for a given month.

Usage:
    python scripts/commission_check.py 2026-04
    python scripts/commission_check.py 2026-04 --sp 06
    python scripts/commission_check.py --all-months         # iterate every month with data
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'inventory_app'))

from commission import get_commission_for_month  # noqa: E402


def _print_table(rows, year_month):
    print(f'\n=== Commission for {year_month} ===')
    if not rows:
        print('(no activity)')
        return
    print(f'{"sp":<6} {"tier":<5} {"own_net":>12} {"third_net":>12} {"total":>12} '
          f'{"comm_below":>11} {"comm_above":>11} {"TOTAL":>10} '
          f'{"recv":>5} {"inv":>5} {"lines":>6}')
    for r in rows:
        comm_above = r['commission_above_own'] + r['commission_above_third']
        print(f'{r["salesperson_code"]:<6} {r["tier_code"]:<5} '
              f'{r["own_net"]:>12,.2f} {r["third_net"]:>12,.2f} {r["total_net"]:>12,.2f} '
              f'{r["commission_below"]:>11,.2f} {comm_above:>11,.2f} '
              f'{r["total_commission"]:>10,.2f} '
              f'{r["receipts_count"]:>5d} {r["invoices_seen"]:>5d} {r["lines_attributed"]:>6d}')
    print(f'  {"":>5} {"TOTAL":<5} '
          f'{sum(r["own_net"] for r in rows):>12,.2f} '
          f'{sum(r["third_net"] for r in rows):>12,.2f} '
          f'{sum(r["total_net"] for r in rows):>12,.2f} '
          f'{sum(r["commission_below"] for r in rows):>11,.2f} '
          f'{sum(r["commission_above_own"]+r["commission_above_third"] for r in rows):>11,.2f} '
          f'{sum(r["total_commission"] for r in rows):>10,.2f}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('year_month', nargs='?', help='YYYY-MM (e.g. 2026-04)')
    ap.add_argument('--sp', help='filter to one salesperson code')
    ap.add_argument('--all-months', action='store_true',
                    help='iterate every month with payment activity')
    args = ap.parse_args()

    if args.all_months:
        import sqlite3
        conn = sqlite3.connect(
            '/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db')
        months = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(date_iso, 1, 7) FROM express_payments_in WHERE is_void=0 ORDER BY 1"
        ).fetchall()]
        conn.close()
        for m in months:
            _print_table(get_commission_for_month(m, args.sp), m)
    else:
        if not args.year_month:
            ap.error('year_month required (or --all-months)')
        _print_table(get_commission_for_month(args.year_month, args.sp), args.year_month)


if __name__ == '__main__':
    main()
