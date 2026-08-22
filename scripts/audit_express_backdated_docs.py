#!/usr/bin/env python3
"""F9 — report the Express documents the daily import's recency window drops.

READ-ONLY. Opens the DB with `mode=ro` so it cannot write even by accident,
and touches no DBF file except to read it.

`commit_express_dbf(since_days=60)` scopes every TRANSACTIONAL builder to
headers with DOCDAT >= today-60. Each day's cutoff moves forward, so a document
dated before it is not skipped once — no later run can reach it either. For the
ordinary back-book that is the intended trade (the file starts in 2003 and both
outstanding snapshots are windowless, so no BALANCE is lost). For a document
Express gains *now* under an old DOCDAT it means the document is invisible to
Sendy permanently, and nothing currently says so.

This reports. It changes no window and writes nothing.

One dataset is one book: BSN5657 pairs with the main DB, xp5 with vat_book.db.
Crossing them compares a book against the other book's ledger and every doc
reads as missing.

    scripts/audit_express_backdated_docs.py \
        --dataset ~/Desktop/express/novat_bsn/BSN5657 \
        --db inventory_app/instance/inventory.db \
        --out /tmp/f9

`--today` fixes the day the cutoff is measured from (default: actual today),
so a report can be reproduced later against the same dataset.
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'inventory_app'))

import express_dbf_source as eds  # noqa: E402


# Where each windowed builder lands its rows. A DOCNUM present in ANY of its
# side's tables is one Sendy already holds by some road — an older text-report
# import, a manual full-history backfill, or a daily run back when the doc was
# still inside the window.
#
# ⚠ RE receipts land in `received_payments` (+ paid_invoices) via
# models.import_payment_records — NOT in `express_payments_in`, which only the
# older TEXT-REPORT path (scripts/import_express.py::run_import) ever writes.
# Reading the wrong one is not a small error: express_payments_in stops at
# 2026-04-30 on both prod and local, so it reports every receipt since as
# "lost", and on vat_book.db (which the text path never fed) it reported all
# 9,945 of them while received_payments actually held 9,949. Every other type
# does land where its name suggests; this is the one that does not.
_KNOWN_AR_SQL = """
    SELECT doc_base    FROM sales_transactions  WHERE doc_base IS NOT NULL
    UNION SELECT doc_base    FROM express_invoice_refs
    UNION SELECT re_no       FROM received_payments
    UNION SELECT doc_no      FROM express_payments_in
    UNION SELECT sr_doc_base FROM credit_note_amounts
"""
_KNOWN_AP_SQL = """
    SELECT doc_base FROM purchase_transactions WHERE doc_base IS NOT NULL
    UNION SELECT doc_no   FROM express_payments_out
    UNION SELECT doc_no   FROM express_credit_notes
"""

# DOCNUM prefix → what the document is, for a report a human reads. Labels
# only; every filter above keys on RECTYP, never on the prefix.
_KIND = {'IV': 'ขาย IV', 'HS': 'ขายสด HS', 'SR': 'ลดหนี้ SR', 'RE': 'รับชำระ RE',
         'RR': 'ซื้อ RR', 'HP': 'ซื้อสด HP', 'GR': 'ลดหนี้ซื้อ GR', 'PS': 'จ่ายชำระ PS'}


# Sendy's own pre-history boundary, not a new one: cashflow.BSN_AR_PREDICATE
# already excludes doc_date_iso < 2024-01-01 as "before the Sendy era" (Put,
# 2026-06-04). Everything older being absent is expected and is not a finding;
# everything newer being absent is.
_SENDY_ERA_START = '2024-01-01'


def _known(conn, sql):
    return {r[0] for r in conn.execute(sql) if r[0]}


def _fmt(n):
    return f"{n:,.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dataset', required=True, help='Express dataset dir (holds ARTRN.DBF)')
    ap.add_argument('--db', required=True, help='the Sendy DB for THAT book (read-only)')
    ap.add_argument('--since-days', type=int, default=60,
                    help="the import's window; must match import_router's default (60)")
    ap.add_argument('--today', help='ISO date the cutoff is measured from (default: today)')
    ap.add_argument('--out', help='directory to write report.md + missing.csv into')
    args = ap.parse_args()

    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.date.today())
    cutoff = today - datetime.timedelta(days=args.since_days)

    artrn = eds.open_table(args.dataset, 'ARTRN')
    aptrn = eds.open_table(args.dataset, 'APTRN')

    # mode=ro: the connection physically cannot write. This is the proof that
    # an audit script left the business DB untouched, not a promise.
    conn = sqlite3.connect(f'file:{os.path.abspath(args.db)}?mode=ro', uri=True)
    try:
        rows = eds.build_out_of_window_docs(
            artrn, aptrn, cutoff,
            known_ar_docs=_known(conn, _KNOWN_AR_SQL),
            known_ap_docs=_known(conn, _KNOWN_AP_SQL))
    finally:
        conn.close()

    # Receipts are valued off their LINES, everything else off its header —
    # where NETAMT is pinned by _billed()'s NETAMT == RCVAMT + REMAMT invariant.
    # The lookup is keyed by (source, doc_no): a DOCNUM shared across the two
    # books would otherwise take whichever side was read second.
    receipts = eds.build_receipt_values(
        artrn, eds.open_table(args.dataset, 'ARRCPIT'),
        eds.open_table(args.dataset, 'ARMAS'),
        aptrn, eds.open_table(args.dataset, 'APRCPIT'),
        eds.open_table(args.dataset, 'APMAS'))
    for r in rows:
        r['value'] = (receipts.get((r['source'], r['doc_no']), 0.0)
                      if r['rectyp'] == '9' else r['netamt'])

    missing = [r for r in rows if not r['in_sendy']]
    held = len(rows) - len(missing)

    by_kind = defaultdict(lambda: {'held': 0, 'missing': 0, 'value': 0.0})
    for r in rows:
        k = by_kind[(r['source'], r['rectyp'], (r['doc_no'] or '')[:2])]
        k['missing' if not r['in_sendy'] else 'held'] += 1
        if not r['in_sendy']:
            k['value'] += r['value']
    by_year = Counter((r['doc_date_iso'] or 'no-date')[:4] for r in missing)

    L = []
    L.append(f"# F9 — เอกสาร Express ที่ window {args.since_days} วันมองไม่เห็น\n")
    L.append(f"- dataset: `{args.dataset}`")
    L.append(f"- db (read-only): `{args.db}`")
    L.append(f"- วันที่ใช้วัด: **{today.isoformat()}** → cutoff = **{cutoff.isoformat()}** "
             f"(เอกสาร DOCDAT < วันนี้ = อยู่นอก window)")
    L.append(f"- ARTRN headers {len(artrn):,} · APTRN headers {len(aptrn):,}\n")
    L.append(f"**นอก window ทั้งหมด {len(rows):,} ฉบับ — Sendy มีอยู่แล้ว {held:,} "
             f"· ไม่มีร่องรอยเลย {len(missing):,}**\n")

    L.append("## แยกตามชนิดเอกสาร\n")
    L.append("| ฝั่ง | RECTYP | ชนิด | Sendy มีแล้ว | ไม่มีเลย | ยอดของที่ไม่มี |")
    L.append("|---|---|---|--:|--:|--:|")
    for (src, rectyp, pre), v in sorted(by_kind.items()):
        L.append(f"| {src} | {rectyp} | {_KIND.get(pre, pre)} | {v['held']:,} | "
                 f"{v['missing']:,} | {_fmt(v['value'])} |")
    L.append("\n> ยอด: IV/HS/SR/RR/HP/GR ใช้ header NETAMT (ผูกด้วย invariant "
             "`NETAMT == RCVAMT + REMAMT` ใน `_billed`). RE/PS ใช้ยอดที่ build จาก "
             "บรรทัด ARRCPIT/APRCPIT ด้วย builder ตัวเดียวกับที่ import ใช้ — header "
             "ของใบเสร็จเชื่อไม่ได้ (NETAMT บน RE tie กับยอดจริงแค่ 95.01%, "
             "ต่างสูงสุด ฿13,300/ใบ).\n")

    L.append("## เอกสารที่ไม่มีร่องรอยใน Sendy — แยกตามปีของเอกสาร\n")
    L.append("| ปีเอกสาร | จำนวน |")
    L.append("|---|--:|")
    for y, n in sorted(by_year.items()):
        L.append(f"| {y} | {n:,} |")

    era = sorted((r for r in missing
                  if (r['doc_date_iso'] or '') >= _SENDY_ERA_START),
                 key=lambda r: (r['doc_date_iso'], r['doc_no']))
    era_valued = [r for r in era if r['value'] or r['remamt']]
    era_empty = [r for r in era if not (r['value'] or r['remamt'])]
    L.append(f"\n## ⭐ ยุค Sendy (เอกสารตั้งแต่ {_SENDY_ERA_START}) ที่หายไป — "
             f"{len(era):,} ฉบับ\n")
    L.append(f"- **มียอดจริง {len(era_valued):,} ฉบับ รวม "
             f"{_fmt(sum(r['value'] for r in era_valued))}** ← ส่วนที่เป็นของหายจริง")
    L.append(f"- ยอด 0 ทุกช่อง {len(era_empty):,} ฉบับ "
             f"(DOCSTAT: {', '.join(sorted(set(str(r['docstat']) for r in era_empty))) or '-'}) "
             f"— ไม่มีอะไรให้เก็บ ไม่ใช่ของหาย\n")
    L.append("| วันที่ | ฝั่ง | เลขที่ | ชนิด | DOCSTAT | ยอด |")
    L.append("|---|---|---|---|---|--:|")
    for r in era_valued:
        L.append(f"| {r['doc_date_iso']} | {r['source']} | {r['doc_no']} | "
                 f"{_KIND.get((r['doc_no'] or '')[:2], r['rectyp'])} | "
                 f"{r['docstat']} | {_fmt(r['value'])} |")

    recent = sorted((r for r in missing if r['doc_date_iso']),
                    key=lambda r: r['doc_date_iso'], reverse=True)[:40]
    L.append("\n## 40 ฉบับล่าสุดที่หายไป (ใหม่สุดก่อน)\n")
    L.append("| วันที่เอกสาร | ฝั่ง | เลขที่ | ชนิด | ยอด | REMAMT |")
    L.append("|---|---|---|---|--:|--:|")
    for r in recent:
        L.append(f"| {r['doc_date_iso']} | {r['source']} | {r['doc_no']} | "
                 f"{_KIND.get((r['doc_no'] or '')[:2], r['rectyp'])} | "
                 f"{_fmt(r['value'])} | {_fmt(r['remamt'])} |")

    report = "\n".join(L) + "\n"
    print(report)

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        md = os.path.join(args.out, 'report.md')
        with open(md, 'w') as f:
            f.write(report)
        csv_path = os.path.join(args.out, 'missing.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(missing[0].keys()) if missing
                               else ['source', 'doc_no', 'rectyp', 'doc_date_iso',
                                     'netamt', 'rcvamt', 'remamt', 'header_count',
                                     'in_sendy', 'value'])
            w.writeheader()
            w.writerows(missing)
        # Re-read what was actually written rather than trusting the writes.
        with open(csv_path) as f:
            written = sum(1 for _ in csv.DictReader(f))
        if written != len(missing):
            raise SystemExit(f'FAILED: wrote {written} CSV rows, expected {len(missing)}')
        print(f"wrote {md} and {csv_path} ({written:,} rows, re-read and verified)")


if __name__ == '__main__':
    main()
