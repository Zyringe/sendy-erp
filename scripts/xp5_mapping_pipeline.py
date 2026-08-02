"""xp5 ↔ Sendy product mapping pipeline (vat-book-view P4).

Three layers, auto-apply only where the plan's conjunctive rules hold; every
ambiguous case goes to the review sheet for Put/team. Writes ONLY the
`xp5_product_mapping` table (mig 151) in the MAIN db — never products, never
the vat book.

  Layer 1 'code+name': xp5 STKCOD == a mapped main bsn_code AND the
      normalized names agree. A code match with a conflicting name NEVER
      auto-applies (Put, grilling Q4).
  Layer 2 'dualkey': xp5 IV docs paired 1:1 to BSN5657 docs by
      (DOCDAT, amount); line items matched by (qty, unit_price); a product
      pair auto-applies only when: doc pairing 1:1 both ways, >= 2
      independent doc pairs, no competing candidate on either side, and
      zero contradictions. (Quantities compare raw+price equal, which pins
      the unit implicitly.)
  Layer 3 'name': difflib similarity of normalized names — suggestions for
      the sheet ONLY, never auto.

Usage:
  ~/.virtualenvs/erp/bin/python scripts/xp5_mapping_pipeline.py \
      --xp5 <xp5 dbf dir> --bsn <bsn5657 dbf dir> [--apply]
Without --apply: dry-run — prints what WOULD auto-apply + writes the sheet.
With --apply: `.backup` the main db first, INSERT the auto rows, re-read
verify in the same run, then write the sheet for the remainder.
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'inventory_app'))

import config                                  # noqa: E402
import express_dbf_source as eds               # noqa: E402

REPORT_DIR = Path.home() / 'Sendai-Boonsawat' / 'Operations' / '05_analysis-reports' / 'data-quality'


_INCH = re.compile(r'(\d+(?:[./]\d+)?)\s*(?:"|นิว|นิ้ว)')
_DROP = re.compile(r'["\'`#*()\[\].,\-_/\\]|\s+')


def normalize_name(s):
    """Format-insensitive product-name key: inch marks → นิ้ว, punctuation and
    spaces dropped, case-folded. Deliberately does NOT strip brand suffixes —
    losing the brand would false-match different-brand twins."""
    s = (s or '').strip().lower()
    s = _INCH.sub(lambda m: m.group(1) + 'นิ้ว', s)
    return _DROP.sub('', s)


def load_xp5_products(xp5_dir):
    out = {}
    for r in eds.open_table(xp5_dir, 'STMAS'):
        code = str(r.get('STKCOD') or '').strip()
        if code and code not in out:
            out[code] = str(r.get('STKDES') or '').strip()
    return out


def load_main(conn):
    """bsn_code → (product_id, bsn_name) for active mapped codes, plus
    product_id → product_name for the whole catalog."""
    code_map = {}
    for r in conn.execute(
            "SELECT bsn_code, bsn_name, product_id FROM product_code_mapping "
            "WHERE is_ignored = 0 AND product_id IS NOT NULL"):
        code_map.setdefault(r['bsn_code'], (r['product_id'], r['bsn_name']))
    names = {r['id']: r['product_name']
             for r in conn.execute("SELECT id, product_name FROM products")}
    return code_map, names


def layer1(xp5_products, code_map, product_names):
    auto, review = [], []
    for code, xname in xp5_products.items():
        hit = code_map.get(code)
        if not hit:
            continue
        pid, bsn_name = hit
        if normalize_name(xname) in (normalize_name(bsn_name),
                                     normalize_name(product_names.get(pid, ''))):
            auto.append({'xp5_code': code, 'product_id': pid, 'xp5_name': xname,
                         'match_layer': 'code+name', 'evidence_count': 1})
        else:
            review.append({'xp5_code': code, 'xp5_name': xname, 'layer': 'code-name-conflict',
                           'suggest_pid': pid,
                           'suggest_name': product_names.get(pid, ''),
                           'evidence': f'code ตรงแต่ชื่อไม่ตรง (BSN: {bsn_name})'})
    return auto, review


def _doc_headers(dbf_dir):
    """IV-type sale docs: DOCNUM → (date, amount). Amount = AFTDISC (falls
    back to NETAMT/TOTAL) rounded to 2dp."""
    docs = {}
    for r in eds.open_table(dbf_dir, 'ARTRN'):
        if str(r.get('RECTYP') or '').strip() != '3':
            continue
        doc = str(r.get('DOCNUM') or '').strip()
        d = r.get('DOCDAT')
        amt = r.get('AFTDISC') or r.get('NETAMT') or r.get('TOTAL') or 0
        if doc and d and amt:
            docs[doc] = (d, round(float(amt), 2))
    return docs


def _doc_lines(dbf_dir, wanted_docs):
    lines = defaultdict(list)
    for r in eds.open_table(dbf_dir, 'STCRD'):
        doc = str(r.get('DOCNUM') or '').strip()
        if doc not in wanted_docs:
            continue
        code = str(r.get('STKCOD') or '').strip()
        qty = round(float(r.get('TRNQTY') or 0), 4)
        price = round(float(r.get('UNITPR') or 0), 4)
        if code:
            lines[doc].append((qty, price, code))
    return lines


def layer2(xp5_dir, bsn_dir, code_map, product_names, already):
    """Dual-keyed invoice-line pairing. Returns (auto, review, stats)."""
    xdocs = _doc_headers(xp5_dir)
    bdocs = _doc_headers(bsn_dir)

    by_key_b = defaultdict(list)
    for doc, key in bdocs.items():
        by_key_b[key].append(doc)
    pairs = []
    xkey_count = Counter(xdocs.values())
    for xdoc, key in xdocs.items():
        cands = by_key_b.get(key, [])
        # 1:1 after all evidence: exactly one BSN doc has this (date, amount)
        # AND this xp5 doc is the only xp5 doc with that key.
        if len(cands) == 1 and xkey_count[key] == 1:
            pairs.append((xdoc, cands[0]))

    xlines = _doc_lines(xp5_dir, {p[0] for p in pairs})
    blines = _doc_lines(bsn_dir, {p[1] for p in pairs})

    votes = defaultdict(set)          # xp5_code → {(pid, doc_pair)}
    contradictions = defaultdict(set)  # xp5_code → {pid}
    for xdoc, bdoc in pairs:
        xl, bl = xlines.get(xdoc, []), blines.get(bdoc, [])
        bl_by_sig = defaultdict(list)
        for qty, price, code in bl:
            bl_by_sig[(qty, price)].append(code)
        xl_sig = Counter((qty, price) for qty, price, _ in xl)
        for qty, price, xcode in xl:
            sig = (qty, price)
            # the line signature must be unique WITHIN both documents
            if xl_sig[sig] != 1 or len(bl_by_sig.get(sig, [])) != 1:
                continue
            bcode = bl_by_sig[sig][0]
            hit = code_map.get(bcode)
            if not hit:
                continue
            pid = hit[0]
            votes[xcode].add((pid, (xdoc, bdoc)))
            contradictions[xcode].add(pid)

    pid_claims = defaultdict(set)      # pid → {xp5_code} (competing-candidate check)
    for xcode, vs in votes.items():
        for pid, _ in vs:
            pid_claims[pid].add(xcode)

    auto, review = [], []
    for xcode, vs in votes.items():
        if xcode in already:
            continue
        pids = contradictions[xcode]
        ev = len({pair for _, pair in vs})
        if len(pids) == 1:
            pid = next(iter(pids))
            if ev >= 2 and len(pid_claims[pid] - {xcode}) == 0:
                auto.append({'xp5_code': xcode, 'product_id': pid,
                             'xp5_name': '', 'match_layer': 'dualkey',
                             'evidence_count': ev})
                continue
            reason = (f'หลักฐาน {ev} คู่เอกสาร (ต้อง ≥2)' if ev < 2
                      else f'สินค้า #{pid} มีผู้ท้าชิงหลาย code')
        else:
            reason = f'ขัดแย้ง: ชี้ไป {len(pids)} สินค้า'
        best = Counter(pid for pid, _ in vs).most_common(1)[0][0]
        review.append({'xp5_code': xcode, 'xp5_name': '', 'layer': 'dualkey-ambiguous',
                       'suggest_pid': best,
                       'suggest_name': product_names.get(best, ''),
                       'evidence': f'{reason} · {ev} doc-pairs'})
    return auto, review, {'doc_pairs': len(pairs)}


def layer3(xp5_products, product_names, taken, threshold=0.78):
    """Name-similarity suggestions — sheet only, never auto."""
    review = []
    norm_main = {pid: normalize_name(nm) for pid, nm in product_names.items()}
    main_list = list(norm_main.items())
    for code, xname in xp5_products.items():
        if code in taken:
            continue
        key = normalize_name(xname)
        if not key:
            continue
        best_pid, best_score = None, 0.0
        sm = difflib.SequenceMatcher(None, '', key)
        for pid, nm in main_list:
            if not nm:
                continue
            sm.set_seq1(nm)
            # quick_ratio is a cheap upper bound — only pay for the real
            # ratio when it could beat the current best AND the threshold.
            if sm.quick_ratio() < max(best_score, threshold):
                continue
            score = sm.ratio()
            if score > best_score:
                best_pid, best_score = pid, score
        if best_pid is not None and best_score >= threshold:
            review.append({'xp5_code': code, 'xp5_name': xname, 'layer': 'name-similar',
                           'suggest_pid': best_pid,
                           'suggest_name': product_names[best_pid],
                           'evidence': f'similarity {best_score:.2f}'})
    return review


def apply_auto(conn, rows, xp5_products):
    for r in rows:
        conn.execute(
            "INSERT INTO xp5_product_mapping "
            "(xp5_code, product_id, xp5_name, match_layer, status, evidence_count) "
            "VALUES (?, ?, ?, ?, 'auto', ?) "
            "ON CONFLICT(xp5_code) DO UPDATE SET "
            "product_id=excluded.product_id, match_layer=excluded.match_layer, "
            "evidence_count=excluded.evidence_count, "
            "updated_at=datetime('now','localtime')",
            (r['xp5_code'], r['product_id'],
             r['xp5_name'] or xp5_products.get(r['xp5_code'], ''),
             r['match_layer'], r['evidence_count']))
    conn.commit()


def write_sheet(review_rows, out_base):
    import csv
    cols = ['xp5_code', 'xp5_name', 'layer', 'suggest_pid', 'suggest_name',
            'evidence', 'คำตัดสิน (pid หรือ x)']
    csv_path = out_base.with_suffix('.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in review_rows:
            w.writerow([r['xp5_code'], r['xp5_name'], r['layer'],
                        r['suggest_pid'], r['suggest_name'], r['evidence'], ''])
    rows_html = '\n'.join(
        f"<tr><td>{r['xp5_code']}</td><td>{r['xp5_name']}</td><td>{r['layer']}</td>"
        f"<td>{r['suggest_pid']}</td><td>{r['suggest_name']}</td>"
        f"<td>{r['evidence']}</td><td></td></tr>" for r in review_rows)
    html = ("<!doctype html><meta charset='utf-8'><title>xp5 mapping review</title>"
            "<style>body{font-family:sans-serif}table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px}</style>"
            f"<h2>xp5 ↔ Sendy mapping — review ({date.today().isoformat()})</h2>"
            f"<p>{len(review_rows)} รายการรอเคาะ · คอลัมน์สุดท้าย: ใส่ product id "
            "ที่ถูกต้อง หรือ x = ไม่ map</p>"
            f"<table><tr>{''.join(f'<th>{c}</th>' for c in cols)}</tr>{rows_html}</table>")
    html_path = out_base.with_suffix('.html')
    html_path.write_text(html, encoding='utf-8')
    return csv_path, html_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--xp5', required=True)
    ap.add_argument('--bsn', required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(config.DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    xp5_products = load_xp5_products(args.xp5)
    code_map, product_names = load_main(conn)

    auto1, review1 = layer1(xp5_products, code_map, product_names)
    already = {r['xp5_code'] for r in auto1}
    auto2, review2, l2stats = layer2(args.xp5, args.bsn, code_map,
                                     product_names, already)
    taken = already | {r['xp5_code'] for r in auto2} \
        | {r['xp5_code'] for r in review1} | {r['xp5_code'] for r in review2}
    review3 = layer3(xp5_products, product_names, taken)

    auto_rows = auto1 + auto2
    review_rows = review1 + review2 + review3
    print(json.dumps({
        'xp5_products': len(xp5_products),
        'layer1_auto': len(auto1), 'layer1_review': len(review1),
        'layer2_auto': len(auto2), 'layer2_review': len(review2),
        'layer2_doc_pairs': l2stats['doc_pairs'],
        'layer3_suggestions': len(review3),
        'unmapped_no_suggestion': len(xp5_products) - len(auto_rows) - len(review_rows),
    }, ensure_ascii=False, indent=1))

    if args.apply:
        backup = str(REPORT_DIR / f'_main_db_backup_pre_xp5_mapping_{date.today().isoformat()}.db')
        conn.execute("PRAGMA busy_timeout=10000")
        src = sqlite3.connect(config.DATABASE_PATH, timeout=10)
        dst = sqlite3.connect(backup)
        src.backup(dst)
        dst.close(); src.close()
        apply_auto(conn, auto_rows, xp5_products)
        n = conn.execute("SELECT COUNT(*) FROM xp5_product_mapping "
                         "WHERE status='auto'").fetchone()[0]
        print(f'applied auto rows, re-read verify: {n} auto rows in table '
              f'(backup: {backup})')
    else:
        print('dry-run only — rerun with --apply to write auto rows')

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f'xp5_mapping_review_{date.today().isoformat()}'
    csv_path, html_path = write_sheet(review_rows, out)
    print(f'review sheet: {csv_path}\n              {html_path}')


if __name__ == '__main__':
    main()
