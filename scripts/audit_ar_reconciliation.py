#!/usr/bin/env python3
"""Compare Sendy's derived AR against the authoritative Express snapshot.

READ-ONLY: opens the DB with `mode=ro` and writes nothing but its own report.
This changes no page and no source of truth — Express `ARTRN.REMAMT` stays
authoritative for AR (Put, 2026-08-22). The point is to see WHERE the ledger
disagrees and whether anything explains it, not to replace a balance.

One DB is one book. `inventory.db` is BSN, `vat_book.db` is the VAT company;
they share no join key, so never run one against the other and never add the
two reports together.

    scripts/audit_ar_reconciliation.py --db inventory_app/instance/inventory.db --out /tmp/ar
"""
import argparse
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'inventory_app'))

# payments_alloc imports config for its DEFAULT db path, which this script never
# uses — every query runs on the connection built from --db. Same placeholder the
# other standalone scripts here use (cleanup_orphan_bsn_ledger.py and friends) so
# a read-only audit does not need the app's secrets to run.
os.environ.setdefault('SECRET_KEY', 'audit-read-only')
os.environ.setdefault('ADMIN_PASSWORD', 'audit-read-only')

import payments_alloc as pa            # noqa: E402
from ar_diagnostic import build_ar_reconciliation   # noqa: E402

ENTITY = 'BSN'
ERA_START = '2024-01-01'               # cashflow.BSN_AR_PREDICATE's own boundary


def _bucket_docs(rep, reason):
    for b in rep['buckets']:
        if b['reason'] == reason:
            return b['docs']
    return 0


def _fmt(n):
    return f'{n:,.2f}'


def _ap_section(conn, L):
    """AP is snapshot-backed and STAYS snapshot-backed. This prints the size of
    the ledger's blind spot so nobody re-derives it by accident, and refuses to
    present the derived number as a competing balance."""
    snap_date, snap_total, snap_docs = conn.execute(
        "SELECT snapshot_date_iso, ROUND(SUM(outstanding_amount),2), COUNT(*) "
        "FROM express_ap_outstanding WHERE entity=? AND snapshot_date_iso="
        "(SELECT MAX(snapshot_date_iso) FROM express_ap_outstanding WHERE entity=?) "
        "GROUP BY snapshot_date_iso", (ENTITY, ENTITY)).fetchone() or (None, 0, 0)
    billed = conn.execute(
        "SELECT ROUND(COALESCE(SUM(net),0),2) FROM purchase_transactions "
        "WHERE date_iso >= ?", (ERA_START,)).fetchone()[0]
    paid = conn.execute(
        "SELECT ROUND(COALESCE(SUM(cash_amount + cheque_amount),0),2) "
        "FROM express_payments_out WHERE is_void = 0 AND date_iso >= ?",
        (ERA_START,)).fetchone()[0]

    L.append('\n## AP — เทียบไว้ดูขนาดของจุดบอด ไม่ใช่ตัวเลขที่ใช้ได้\n')
    L.append(f'- snapshot (ของจริง, {snap_date}): **{_fmt(snap_total)}** จาก {snap_docs:,} เอกสาร')
    ledger = round(billed - paid, 2)
    # Computed, never typed: the ratio moves with every import, and a stale
    # hardcoded multiple in a generated report is the one number that will be
    # wrong. (Codex measured ~85x on the 2026-05-29 text-report snapshot; this
    # line prints whatever today's data actually says.)
    ratio = (f'{ledger / snap_total:,.0f} เท่า' if snap_total else 'ไม่มี snapshot ให้เทียบ')
    L.append(f'- ledger ตั้งแต่ {ERA_START}: ซื้อ {_fmt(billed)} − จ่าย {_fmt(paid)} '
             f'= **{_fmt(ledger)}** → **{ratio}ของ snapshot**')
    L.append('\n> ⛔ ตัวเลข ledger ข้างบน **ใช้แทน AP ไม่ได้ และห้ามเรียกว่าส่วนต่างที่ต้องตาม**. '
             'Sendy ไม่มียอดยกมา ไม่มีภาระระหว่างบริษัท และมีซัพพลายเออร์ที่ไม่เคยมีร่องรอย '
             'การจ่ายเงินใน ledger เลย → ยอดซื้อสะสมจึงไม่มีวันถูกหักให้ครบ. '
             'ส่วนต่างนี้ **อธิบายจาก ledger ในเครื่องไม่ได้ตามนิยาม** ไม่ใช่ finding — '
             'AP อยู่กับ snapshot ต่อไป.')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True, help='the Sendy DB for ONE book (read-only)')
    ap.add_argument('--out', help='directory for report.md + unexplained.csv')
    ap.add_argument('--era-start', default=ERA_START)
    args = ap.parse_args()

    conn = sqlite3.connect(f'file:{os.path.abspath(args.db)}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        snap_date = conn.execute(
            'SELECT MAX(snapshot_date_iso) d FROM express_ar_outstanding '
            'WHERE entity=?', (ENTITY,)).fetchone()['d']
        if not snap_date:
            raise SystemExit(f'no {ENTITY} AR snapshot in {args.db} — nothing to compare against')
        snapshot = [dict(r) for r in conn.execute(
            'SELECT doc_no, doc_date_iso, is_anomalous, outstanding_amount, customer_name '
            'FROM express_ar_outstanding WHERE entity=? AND snapshot_date_iso=?',
            (ENTITY, snap_date))]
        writeoffs = {r[0] for r in conn.execute('SELECT doc_no FROM ar_writeoffs')}
        # AS-OF the snapshot, never today: a receipt banked after the export
        # date would otherwise settle an invoice the snapshot still shows open,
        # and every one of those reads as an unexplained finding. Measured on
        # the 2026-06-05 local snapshot: 76 phantom rows worth 173,091.77
        # before this argument existed.
        derived = pa.invoice_settlement(conn=conn, as_of=snap_date)
        # {sr_doc: (ref_invoice, amount)} — lets an invoice that a still-open
        # ใบลดหนี้ nets to zero be recognised instead of read as a disagreement.
        cn_refs = {r['sr_doc_base']: (r['ref_invoice'], r['credited_amount'])
                   for r in conn.execute(
                       'SELECT sr_doc_base, ref_invoice, credited_amount '
                       'FROM credit_note_amounts '
                       'WHERE ref_invoice IS NOT NULL AND ref_invoice <> ""')}
        # How far the ledger actually reaches. A snapshot exported today against
        # a ledger that stops yesterday is not a book full of missing invoices.
        horizon = conn.execute(
            'SELECT MAX(date_iso) d FROM sales_transactions').fetchone()['d']
        rep = build_ar_reconciliation(snapshot, derived, writeoff_doc_nos=writeoffs,
                                      era_start=args.era_start,
                                      credit_note_refs=cn_refs,
                                      ledger_horizon=horizon)
        L = []
        L.append('# AR reconciliation — Express snapshot เทียบกับยอดที่ Sendy คำนวณเอง\n')
        L.append(f'- db (read-only): `{args.db}` · book **{ENTITY}**')
        L.append(f'- snapshot ล่าสุด: **{snap_date}** · {len(snapshot):,} เอกสารเปิดค้าง')
        L.append(f'- ยอด snapshot รวม (gross): **{_fmt(rep["snapshot_total"])}**')
        L.append(f'- ฝั่ง derived: {len(derived):,} ใบแจ้งหนี้จาก '
                 f'`payments_alloc.invoice_settlement(as_of={snap_date!r})` — '
                 f'ตัดที่วันเดียวกับ snapshot\n')
        L.append(f'- ledger มีข้อมูลถึง: **{horizon}**'
                 + ('' if horizon == snap_date else
                    f' — **ช้ากว่า snapshot อยู่** เอกสารหลังวันนี้จะเข้ากลุ่ม '
                    f'`not_yet_imported` ไม่ใช่ของหาย'))
        L.append('\n> อ่านอย่างเดียว ไม่เปลี่ยนแหล่งข้อมูลของหน้าไหนทั้งนั้น — '
                 'Express REMAMT ยังเป็นตัวจริงของ AR\n')

        L.append('## ทุกเอกสารอยู่กลุ่มเดียว และรวมกลับเป็นยอด snapshot ได้พอดี\n')
        L.append('| กลุ่ม | เอกสาร | ยอด snapshot | ยอด derived | ทำไม |')
        L.append('|---|--:|--:|--:|---|')
        for b in rep['buckets']:
            if not b['docs']:
                continue
            L.append(f'| `{b["reason"]}` | {b["docs"]:,} | {_fmt(b["snapshot_amount"])} '
                     f'| {_fmt(b["derived_amount"])} | {b["why"]} |')
        stale = _bucket_docs(rep, 'not_yet_imported')
        ghosts = _bucket_docs(rep, 'no_ledger_lines')
        if stale and ghosts:
            L.append(f'\n> ⚠ ledger ช้ากว่า snapshot **และ** ยังมี {ghosts:,} เอกสารที่ลงวันที่ '
                     f'**ไม่เกิน {horizon}** แต่ Sendy ไม่มีบรรทัดขาย — `MAX(date_iso)` เป็นแค่ '
                     f'วันล่าสุดที่มี ไม่ได้แปลว่าวันนั้นเข้าครบ ดังนั้นอย่าเหมาว่าทั้งหมด '
                     f'"แค่ยังไม่ได้ import" ต้องดูรายตัว')

        checksum = round(sum(b['snapshot_amount'] for b in rep['buckets']), 2)
        ok = 'ตรง ✅' if checksum == rep['snapshot_total'] else 'ไม่ตรง 🔴'
        L.append(f'\n**ตรวจยอด:** ผลรวมทุกกลุ่ม {_fmt(checksum)} '
                 f'เทียบยอด snapshot {_fmt(rep["snapshot_total"])} → {ok}')

        un = rep['unexplained']
        total_un = round(sum(r['difference'] for r in un), 2)
        L.append(f'\n## 🔴 ต่างกันโดยไม่มีอะไรอธิบายได้ — {len(un):,} เอกสาร '
                 f'(ผลต่างสุทธิ {_fmt(total_un)})\n')
        L.append('> ไม่มี tolerance และไม่มีถังรวม "legacy" — ต่าง 1 สตางค์ก็ขึ้นตรงนี้\n')
        if un:
            L.append('| วันที่ | เอกสาร | ลูกค้า | snapshot | derived | ต่าง |')
            L.append('|---|---|---|--:|--:|--:|')
            for r in un[:60]:
                L.append(f'| {r["doc_date_iso"]} | {r["doc_no"]} | {r["customer"] or "-"} '
                         f'| {_fmt(r["snapshot"])} | {_fmt(r["derived"])} | {_fmt(r["difference"])} |')
            if len(un) > 60:
                L.append(f'\n*(แสดง 60 จาก {len(un):,} — ที่เหลืออยู่ใน unexplained.csv ครบ)*')
        else:
            L.append('ไม่มี')

        _ap_section(conn, L)
        report = '\n'.join(L) + '\n'
    finally:
        conn.close()

    print(report)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, 'report.md'), 'w') as f:
            f.write(report)
        path = os.path.join(args.out, 'unexplained.csv')
        cols = ['doc_no', 'doc_date_iso', 'customer', 'snapshot', 'derived', 'difference']
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rep['unexplained'])
        with open(path) as f:
            n = sum(1 for _ in csv.DictReader(f))
        if n != len(rep['unexplained']):
            raise SystemExit(f'FAILED: wrote {n} CSV rows, expected {len(rep["unexplained"])}')
        print(f'wrote {args.out}/report.md and unexplained.csv ({n:,} rows, re-read and verified)')


if __name__ == '__main__':
    main()
