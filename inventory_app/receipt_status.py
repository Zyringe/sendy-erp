"""Receipt status (สถานะรับชำระ) of a Sendy-ledger invoice — ADR 0024.

Four statuses: paid, partial, unpaid, and written_off (ตัดหนี้แล้ว). The amounts
come from `payments_alloc.invoice_settlement()`, which already nets credit notes
and applies the legacy NULL-amount rule, so a part-received invoice reads
partial and one cleared by credit notes reads paid.

Receipt status is not AR. It answers "has this invoice been received", never
"what does this customer owe": that question is chaseable, owned by the Express
snapshot (ADR 0012). Commission's "settled" means commission paid, not this.
"""
from database import get_connection
import payments_alloc

PAID = 'paid'
PARTIAL = 'partial'
UNPAID = 'unpaid'
WRITTEN_OFF = 'written_off'
STATUSES = (PAID, PARTIAL, UNPAID, WRITTEN_OFF)

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

    `remainder` is billed minus credit notes minus collected. It is negative
    on an overpaid invoice, whose status stays paid.
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        written_off = {r[0] for r in conn.execute(
            "SELECT doc_no FROM ar_writeoffs WHERE ? IS NULL OR writeoff_date <= ?",
            (as_of, as_of))}
        settled = payments_alloc.invoice_settlement(
            customer=customer, customer_code=customer_code, as_of=as_of, conn=conn)
    finally:
        if own:
            conn.close()

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
