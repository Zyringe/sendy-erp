"""DEPRECATED: one-off from 2026-05-02. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

One-shot: mark every commission for INVOICES issued BEFORE 2026-02 as paid.

Per Put 2026-05-02: "commission ที่จ่ายแล้วที่เป็นเลขที่เอกสารตั้งแต่ก่อน
เดือน 2 ปี 69 ให้ถือว่าจ่ายแล้วทั้งหมด".

The cutoff is by INVOICE issue date (เลขที่เอกสาร = ใบกำกับ), not by
receipt date. An invoice issued Jan 2026 but paid by the customer in
March 2026 still qualifies (its commission cycle is March, but it
counts as auto-paid because the invoice itself is pre-Feb).

Algorithm:
  For each receipt month with activity (any month):
    For each salesperson:
      For each invoice attributed to that (sp, month):
        If invoice_date < 2026-02-01 AND remaining > 0:
          insert payout row in that month's cycle with remaining amount.

Idempotent: rerun skips already-paid rows (remaining ≈ 0).

Marker: paid_method='auto', note='pre-Feb 2026 auto-paid'.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'inventory_app'))

import sqlite3
import commission
from config import DATABASE_PATH as DB  # noqa: E402 — commission already imports config

INVOICE_CUTOFF = '2026-02-01'   # invoices STRICTLY older than this auto-paid

PAID_DATE = '2026-02-01'
PAID_METHOD = 'auto'
NOTE = 'pre-Feb 2026 auto-paid'
PAID_BY = 'system'


def _list_months():
    """All receipt-month + sp pairs where any pre-cutoff invoice was paid."""
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT DISTINCT substr(pin.date_iso, 1, 7) AS ym, pin.salesperson_code
          FROM express_payments_in pin
          JOIN express_payment_in_invoice_refs ref ON ref.payment_in_id = pin.id
          JOIN express_sales es ON es.doc_no = ref.invoice_no
         WHERE pin.is_void = 0
           AND pin.salesperson_code <> ''
           AND es.date_iso < ?
         ORDER BY ym, pin.salesperson_code
    """, (INVOICE_CUTOFF,)).fetchall()
    conn.close()
    return rows


def main():
    pairs = _list_months()
    print(f'Cutoff: invoice_date < {INVOICE_CUTOFF}')
    print(f'  ({len(pairs)} (receipt-month, sp) pairs to scan)')

    inserted = 0
    skipped = 0
    total_amount = 0.0

    for ym, sp in pairs:
        invs = commission.get_invoice_commission_for_sp(ym, sp)
        for inv in invs:
            inv_date = inv.get('invoice_date') or ''
            if not inv_date or inv_date >= INVOICE_CUTOFF:
                continue           # invoice issued ≥ cutoff — leave alone
            if inv['remaining'] <= 0.05:
                skipped += 1
                continue
            commission.record_payout(
                year_month=ym,
                salesperson_code=sp,
                amount_paid=inv['remaining'],
                paid_date=PAID_DATE,
                paid_method=PAID_METHOD,
                note=NOTE,
                paid_by=PAID_BY,
                invoice_no=inv['invoice_no'],
            )
            inserted += 1
            total_amount += inv['remaining']
        if inserted and inserted % 200 == 0:
            print(f'  ... {ym}/{sp} → inserted {inserted} so far (฿{total_amount:,.2f})')

    print(f'\nDone:')
    print(f'  inserted     : {inserted}')
    print(f'  skipped      : {skipped} (already paid / tiny remainder)')
    print(f'  total amount : ฿{total_amount:,.2f}')


if __name__ == '__main__':
    main()
