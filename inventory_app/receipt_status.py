"""Receipt status (สถานะรับชำระ) of a Sendy-ledger invoice — ADR 0024.

Four statuses: paid, partial, unpaid, and written_off (ตัดหนี้แล้ว). The amounts
come from `payments_alloc.invoice_settlement()`, which already nets credit notes
and applies the legacy NULL-amount rule, so a part-received invoice reads
partial and one cleared by credit notes reads paid.

Receipt status is not AR. It answers "has this invoice been received", never
"what does this customer owe": that question is chaseable, owned by the Express
snapshot (ADR 0012). Commission's "settled" means commission paid, not this.
"""
from database import read_snapshot
import payments_alloc

PAID = 'paid'
PARTIAL = 'partial'
UNPAID = 'unpaid'
WRITTEN_OFF = 'written_off'
STATUSES = (PAID, PARTIAL, UNPAID, WRITTEN_OFF)

# Decision A (Put, 2026-10-02): a remainder under ฿1 is an Express short-close
# (IV6900675 closed ฿0.03 short). It still reads partial, but reconcile counts
# a gap under this as a match and the matcher never offers such a remainder.
SHORT_CLOSE_FLOOR = 1.00

_FROM_ENGINE = {
    'paid': PAID,
    'overpaid': PAID,
    'fully_credited': PAID,
    'partial': PARTIAL,
    'unpaid': UNPAID,
}


def rows(conn=None, customer=None, customer_code=None, as_of=None):
    """One row per receivable invoice, ordered by invoice date then doc_base.

    Receivable = the engine's population (a doc_base, not a return, not HS)
    with billed > 0. `customer` filters by bill name, `customer_code` by exact
    code. `as_of` is point-in-time: later invoices, receipts, credit notes and
    write-offs do not count.

    An invoice in `ar_writeoffs` (the whole table, collectability reading) is
    written_off unless it was received in full, in which case the write-off
    forgave nothing and it stays paid. `written_off` flags every invoice in
    the table whatever its status.

    With no `conn` it reads inside its own read transaction. A caller that
    reads more alongside passes a connection from `database.read_snapshot()`.

    `remainder` is billed minus credit notes minus collected. It is negative
    on an overpaid invoice, whose status stays paid.
    """
    if conn is None:
        with read_snapshot() as c:
            return rows(conn=c, customer=customer, customer_code=customer_code, as_of=as_of)
    written_off = {r[0] for r in conn.execute(
        "SELECT doc_no FROM ar_writeoffs WHERE ? IS NULL OR writeoff_date <= ?",
        (as_of, as_of))}
    settled = payments_alloc.invoice_settlement(
        customer=customer, customer_code=customer_code, as_of=as_of, conn=conn)

    out = []
    for s in settled:
        if s['billed'] <= 0:
            continue
        status = _FROM_ENGINE[s['status']]
        is_written_off = s['doc_base'] in written_off
        if is_written_off and status != PAID:
            status = WRITTEN_OFF
        out.append({
            'doc_base': s['doc_base'],
            'customer': s['customer'],
            'customer_code': s['customer_code'],
            'invoice_date': s['invoice_date'],
            'billed': s['billed'],
            'collected': s['collected'],
            'remainder': s['outstanding'],
            'status': status,
            'written_off': is_written_off,
            'last_payment_date': s['last_payment_date'],
        })
    return out


def summarize(status_rows):
    """Counts per status for the invoices tab, with each card's amount: billed
    for paid and written_off, the remainder still open for partial and unpaid.
    The four counts add up to total_bills."""
    s = {'total_bills': 0, 'paid_billed': 0.0, 'partial_remainder': 0.0,
         'unpaid_remainder': 0.0, 'written_off_billed': 0.0}
    s.update({f'{st}_count': 0 for st in STATUSES})
    for r in status_rows:
        s['total_bills'] += 1
        s[f"{r['status']}_count"] += 1
        if r['status'] in (PAID, WRITTEN_OFF):
            s[f"{r['status']}_billed"] += r['billed']
        else:
            s[f"{r['status']}_remainder"] += r['remainder']
    for k in ('paid_billed', 'partial_remainder', 'unpaid_remainder', 'written_off_billed'):
        s[k] = round(s[k], 2)
    return s
