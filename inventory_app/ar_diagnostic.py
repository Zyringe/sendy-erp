"""AR reconciliation diagnostic — Express snapshot vs Sendy's derived balance.

READ-ONLY, and deliberately NOT authoritative. Express `ARTRN.REMAMT` (landed
in `express_ar_outstanding`) remains the source of truth for AR — Put chose
that on 2026-08-22, and the measurement behind it is that ledger-DERIVED AP came
out ~85x the trusted figure because opening balances and intercompany
obligations never entered Sendy's book at all. AR derivation is closer, but
"closer" is not a licence to swap the source under `/ar`, dunning or cashflow.

So this module answers a narrower, honest question: **where do the two
disagree, and does anything explain it?** It feeds a report, never a page.

Two rules make the output trustworthy rather than reassuring:

  1. Every compared document lands in EXACTLY ONE bucket, and the buckets'
     snapshot amounts sum to the snapshot total. Same disjointness
     `cashflow.bsn_ar_excluded` already guarantees for the AR pages: a doc that
     is both an RE anomaly and pre-era is counted once, in the first reason
     that fits, so the classification adds up instead of merely looking full.

  2. Exact satang equality decides agreement — no tolerance band, and no
     catch-all "legacy" bucket. A difference nothing on the list explains is
     reported per-document as `unexplained`. Hiding a satang inside a
     tolerance is how a reconciliation stops being one.
"""

# Ordered: the FIRST reason that fits claims the document. Order matters — it
# is what keeps the buckets disjoint, and it runs most-specific-first so a
# written-off RE is reported as the receipt anomaly it is rather than as a
# write-off decision nobody made.
_REASONS = [
    ('re_anomaly',
     'RE/ใบเสร็จที่ Express ตีธง — "ลูกหนี้จ่ายแล้ว" ไม่ใช่ใบแจ้งหนี้ '
     'ฝั่ง derived ไม่มีทางมี'),
    ('pre_era',
     'หนี้ก่อนยุค Sendy — ledger ไม่เคยมียอดตั้งต้น จึง derive ไม่ได้ '
     '(กติกาเดียวกับ cashflow.BSN_AR_PREDICATE)'),
    ('written_off',
     'นักบัญชีตัดหนี้สูญแล้ว (ar_writeoffs) — snapshot ยังโชว์ แต่เลิกตาม'),
    ('credit_note',
     'ใบลดหนี้ SR — payments_alloc._settlement_rows กรอง SR% ออกโดยตั้งใจ'),
    ('cash_sale',
     'ขายสด HS — _settlement_rows กรอง HS% ออกโดยตั้งใจ'),
    ('no_ledger_lines',
     '⚠ snapshot มีเอกสาร แต่ Sendy ไม่มีบรรทัดขายของใบนี้เลย — บิลหาย'),
    ('unallocated',
     '⚠ Sendy ยังค้าง แต่ Express ปิดไปแล้ว — ขาดการผูกใบเสร็จ'),
    ('agrees', 'ตรงกันถึงสตางค์'),
    ('unexplained', '🔴 ต่างกันโดยไม่มีเหตุผลใดอธิบายได้'),
]
_WHY = dict(_REASONS)


def _r2(x):
    return round(x or 0.0, 2)


def build_ar_reconciliation(snapshot_rows, derived_rows,
                            writeoff_doc_nos=(), era_start='2024-01-01'):
    """Classify every disagreement between the two AR views.

    snapshot_rows: `express_ar_outstanding` rows for ONE entity at ONE
        snapshot date — doc_no, doc_date_iso, is_anomalous, outstanding_amount,
        customer_name. Never mix entities: the two books share no join key.
    derived_rows: `payments_alloc.invoice_settlement()` output — doc_base,
        outstanding, invoice_date, customer.
    writeoff_doc_nos: `ar_writeoffs.doc_no`.
    era_start: the pre-Sendy boundary, matching cashflow.BSN_AR_PREDICATE.

    Raises on an EMPTY snapshot: with nothing to compare against, every derived
    row would read as a finding and every bucket as clean, which is the
    can't-fail shape this diagnostic exists to avoid.
    """
    if not snapshot_rows:
        raise ValueError(
            'AR reconciliation needs a snapshot to compare against — an empty '
            'one would report a clean bill of health for a comparison that '
            'never ran')

    snap = {}
    for r in snapshot_rows:
        doc = r['doc_no']
        if doc in snap:
            raise ValueError(
                f'{doc}: appears more than once among open snapshot documents '
                f'— refusing rather than counting the balance twice')
        snap[doc] = r

    # Keep zero-outstanding derived rows: a snapshot doc that Sendy believes is
    # settled is the dangerous disagreement, and dropping the zero would hide
    # it as "no ledger lines".
    derived = {r['doc_base']: r for r in derived_rows}
    writeoffs = set(writeoff_doc_nos)

    population = set(snap) | {d for d, r in derived.items() if _r2(r['outstanding'])}

    tallies = {code: {'docs': 0, 'snapshot_amount': 0.0, 'derived_amount': 0.0}
               for code, _ in _REASONS}
    unexplained = []

    for doc in sorted(population):
        s = snap.get(doc)
        d = derived.get(doc)
        s_amt = _r2(s['outstanding_amount']) if s else 0.0
        d_amt = _r2(d['outstanding']) if d else 0.0

        if s is not None and s['is_anomalous']:
            reason = 're_anomaly'
        elif ((s['doc_date_iso'] if s else d.get('invoice_date')) or '') < era_start:
            reason = 'pre_era'
        elif doc in writeoffs:
            reason = 'written_off'
        elif doc.startswith('SR'):
            reason = 'credit_note'
        elif doc.startswith('HS'):
            reason = 'cash_sale'
        elif s is not None and d is None:
            reason = 'no_ledger_lines'
        elif s is None:
            reason = 'unallocated'
        elif s_amt == d_amt:
            reason = 'agrees'
        else:
            reason = 'unexplained'
            unexplained.append({
                'doc_no': doc,
                'customer': (s or d).get('customer_name') or (d or {}).get('customer'),
                'doc_date_iso': s['doc_date_iso'] if s else d.get('invoice_date'),
                'snapshot': s_amt, 'derived': d_amt,
                'difference': round(s_amt - d_amt, 2),
            })

        t = tallies[reason]
        t['docs'] += 1
        t['snapshot_amount'] = round(t['snapshot_amount'] + s_amt, 2)
        t['derived_amount'] = round(t['derived_amount'] + d_amt, 2)

    unexplained.sort(key=lambda r: (-abs(r['difference']), r['doc_no']))
    return {
        'snapshot_total': round(sum(_r2(r['outstanding_amount'])
                                    for r in snapshot_rows), 2),
        'derived_total': round(sum(t['derived_amount'] for t in tallies.values()), 2),
        'buckets': [{'reason': code, 'why': _WHY[code], **tallies[code]}
                    for code, _ in _REASONS],
        'unexplained': unexplained,
    }
