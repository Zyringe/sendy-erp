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
    ('credit_note_offset',
     'บิลถูกใบลดหนี้ล้างพอดี และใบลดหนี้ยังเปิดค้างอยู่ใน snapshot เอง — '
     'Express แยกเป็น 2 ใบ, Sendy หักกลบให้ ยอดรวมเท่ากัน ทั้งคู่ถูก'),
    ('credit_note',
     'ใบลดหนี้ SR — payments_alloc._settlement_rows กรอง SR% ออกโดยตั้งใจ'),
    ('cash_sale',
     'ขายสด HS — _settlement_rows กรอง HS% ออกโดยตั้งใจ'),
    ('not_yet_imported',
     'เอกสารใหม่กว่าที่ ledger มี — export ล่ำหน้า import ยังไม่ใช่ของหาย'),
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


def _is_pre_era(snap_row, derived_row, era_start):
    """True only when a document carries a REAL date earlier than the boundary.

    A missing date is not evidence of anything. Coercing it to '' makes the
    comparison true against every ISO boundary, which files the document under
    expected-legacy debt and lets a genuine missing-ledger or unexplained gap
    disappear into a bucket nobody re-reads (Codex review, 2026-08-22)."""
    date = (snap_row['doc_date_iso'] if snap_row
            else derived_row.get('invoice_date'))
    return bool(date) and date < era_start


def build_ar_reconciliation(snapshot_rows, derived_rows,
                            writeoff_doc_nos=(), era_start='2024-01-01',
                            credit_note_refs=None, ledger_horizon=None):
    """Classify every disagreement between the two AR views.

    snapshot_rows: `express_ar_outstanding` rows for ONE entity at ONE
        snapshot date — doc_no, doc_date_iso, is_anomalous, outstanding_amount,
        customer_name. Never mix entities: the two books share no join key.
    derived_rows: `payments_alloc.invoice_settlement()` output — doc_base,
        outstanding, invoice_date, customer.
    writeoff_doc_nos: `ar_writeoffs.doc_no`.
    era_start: the pre-Sendy boundary, matching cashflow.BSN_AR_PREDICATE.
    credit_note_refs: {sr_doc_no: (ref_invoice, credited_amount)} from
        `credit_note_amounts`. Lets an invoice whose derived balance is 0 ONLY
        because a ใบลดหนี้ netted it be recognised when that same credit note is
        itself still open in the snapshot — Express keeps the two documents
        apart, Sendy nets them, the totals agree and neither is wrong.
        ⚠ Reads the AUTHORITATIVE table only. `_settlement_rows` also nets an SR
        FALLBACK over sales_transactions for SRs absent from that table; those
        cannot be proven here and deliberately stay `unexplained` rather than
        being classified on a guess (prod 2026-08-22: 3 fallback SRs exist, none
        pointing at an open invoice, so the gap is latent — and loud when it
        stops being).
    ledger_horizon: the newest date the ledger actually contains
        (MAX(sales_transactions.date_iso)). Snapshot documents dated AFTER it are
        classified `not_yet_imported` instead of being reported as missing bills:
        the export simply runs ahead of the import. Empty string is refused —
        that is a ledger with no rows at all, where every document would be
        excused and the comparison would mean nothing.
        ⚠ It is a high-water mark, not proof of completeness: if one document
        for the newest day imported and its siblings did not, they fall back
        into `no_ledger_lines`. The report says so at the header rather than
        letting the reader infer it.

    Raises on an EMPTY snapshot: with nothing to compare against, every derived
    row would read as a finding and every bucket as clean, which is the
    can't-fail shape this diagnostic exists to avoid.
    """
    if ledger_horizon is not None and not ledger_horizon:
        raise ValueError(
            'ledger_horizon is empty — a ledger with no rows would excuse every '
            'document in the snapshot and the comparison would prove nothing')
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
    # Invoice -> SUM of the credited amounts of the credit notes that are ALSO
    # open in this same snapshot. Built here rather than taken from the caller so
    # the "still open" half is checked against the snapshot actually being
    # compared.
    # ⚠ SUMMED, not assigned: `_settlement_rows`' `cn` CTE does
    # `SUM(credit_notes) GROUP BY iv_no`, so an invoice offset by TWO open credit
    # notes must add them here too. Assigning would let the second overwrite the
    # first and a fully-offset invoice would be reported as a disagreement — 9
    # invoices on prod already carry more than one credit note (2026-08-22).
    offsets = {}
    for sr_doc, (ref_iv, amount) in (credit_note_refs or {}).items():
        sr = snap.get(sr_doc)
        if sr is not None and _r2(sr['outstanding_amount']) == -_r2(amount):
            offsets[ref_iv] = round(offsets.get(ref_iv, 0.0) + _r2(amount), 2)

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
        elif _is_pre_era(s, d, era_start):
            reason = 'pre_era'
        elif doc in writeoffs:
            reason = 'written_off'
        elif (d is not None and d_amt == 0.0
              and _r2(d.get('credit_notes')) == s_amt
              and offsets.get(doc) == s_amt and s_amt):
            reason = 'credit_note_offset'
        elif doc.startswith('SR'):
            reason = 'credit_note'
        elif doc.startswith('HS'):
            reason = 'cash_sale'
        elif (ledger_horizon and s is not None and d is None
              and (s['doc_date_iso'] or '') > ledger_horizon):
            # AFTER every verdict that is already true regardless of the ledger:
            # an RE never enters sales_transactions and SR/HS are filtered out of
            # the derived population, so calling either "not yet imported" would
            # promise an import that will never happen.
            # ⚠ `d is None` is load-bearing: a derived row EXISTING proves the
            # document was imported, so excusing it on freshness would hide a
            # real balance disagreement behind a story about the ledger lagging.
            reason = 'not_yet_imported'
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
