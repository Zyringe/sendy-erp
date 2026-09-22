"""Derive #600's relabel list: the stored bill lines the pre-#599 unit map
mis-read, recovered line by line from the BSN5657 stock card (spec #595 · 5/7,
ADR 0018).

Until migration 190 Sendy's map read BSN5657 `กร` as ตัว, `ถง` as ถุง and `บล` as
แผง. Express's own unit list says กุรุส / ถัง / บล็อก. 190 fixed the map; the rows
already written through the old one still carry the old word, and batch 37's
history purchases still carry the raw code `กร` (migration 186 left the three
codes for #599/#600).

A stored `ตัว` does not say whether Express wrote `ตว` or `กร`, so the answer is
read per line from Express's stock card (STCRD): a sales line by its printed
document + line number (`IV6701146-6` = STCRD DOCNUM IV6701146, SEQNUM 6), a
purchase line by its document + stock code. A line is relabelled only when:
  * exactly one stock-card line answers, carrying the same stock code;
  * that line's code is กร / ถง / บล;
  * the stored word is what the old map wrote for it (or the raw code);
  * the stored qty equals the stock card's, and doc_no + bsn_code names one row.
Anything else on a stock code Express bills in those codes is listed as
unrecoverable with its reason, and is never guessed.

The word a line reads after the relabel always comes from the unit map
(bsn_units.translate, the call the importer makes). OLD_READING below is a
historical fact about the retired map, not a spelling.

READ-ONLY on both sides: the DB is opened `mode=ro`, the DBF is only read. It
writes one JSON file for scripts/2026_09_22_relabel_history_600.py to apply.

    EXPRESS_DIR=~/Sendai-Boonsawat/projects/express-integration/data/BSN5657 \\
    ~/.virtualenvs/erp/bin/python scripts/derive_600_relabel_history.py --db COPY.db

Run it on a copy of prod taken to migration 190 or later. The stock card is a
snapshot (2026-08-25 as of this ticket): a line dated after it cannot be
recovered from it, and shows up as unrecoverable if it is on a stock code
Express bills in these codes.
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import sqlite3
import sys

BOOK = 'BSN5657'
# What the retired map wrote for each code (migration 190's `_mig190_meaning`).
OLD_READING = {'กร': 'ตัว', 'ถง': 'ถุง', 'บล': 'แผง'}
TABLES = ('sales_transactions', 'purchase_transactions')

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(_HERE, '2026_09_22_relabel_history_600.json')
DEFAULT_EXPRESS_DIR = os.path.expanduser(
    '~/Sendai-Boonsawat/projects/express-integration/data/BSN5657')


class DeriveRefused(Exception):
    """The inputs cannot support a list."""


def _txt(v):
    return ('' if v is None else str(v)).replace('\xa0', ' ').strip()


def _seq(v):
    try:
        return int(_txt(v))
    except ValueError:
        return None


def _iso(d):
    return d.isoformat() if hasattr(d, 'isoformat') else _txt(d)


def words_from_map(conn):
    """{code: (old word, the word the unit map gives it now)}. Refuses before
    #599: a map that still reads กร as ตัว would make this list a no-op."""
    import bsn_units
    words = {}
    for code, old in OLD_READING.items():
        new = bsn_units.translate(code, BOOK, conn=conn)
        if new in (None, '', old, code):
            raise DeriveRefused(
                f'the unit map reads {BOOK} {code!r} as {new!r}; migration 190 (#599) '
                'must be on this DB first')
        words[code] = (old, new)
    return words


def derive(conn, stcrd_rows):
    """The relabel list for `conn`'s bill lines, from the stock-card rows."""
    words = words_from_map(conn)
    by_seq = collections.defaultdict(list)
    by_doc = collections.defaultdict(list)
    billed = collections.defaultdict(set)     # code -> stock codes Express bills in it
    last = ''
    for r in stcrd_rows:
        line = {'doc': _txt(r.get('DOCNUM')), 'seq': _seq(r.get('SEQNUM')),
                'code': _txt(r.get('STKCOD')), 'unit': _txt(r.get('TQUCOD')),
                'qty': r.get('TRNQTY'), 'factor': r.get('TFACTOR'),
                'date': _iso(r.get('DOCDAT'))}
        by_seq[(line['doc'], line['seq'])].append(line)
        by_doc[line['doc']].append(line)
        if line['unit'] in words:
            billed[line['unit']].add(line['code'])
        last = max(last, line['date'] or '')
    if not by_doc:
        raise DeriveRefused('the stock card is empty; "nothing to relabel" would be a lie')

    relabel, unrecoverable = [], []
    already = collections.Counter()
    not_suspect = collections.Counter()
    for table in TABLES:
        rows = conn.execute(
            f"SELECT id, doc_no, bsn_code, product_id, date_iso, qty, unit FROM {table} "
            f"ORDER BY id").fetchall()
        n_ident = collections.Counter((r['doc_no'], r['bsn_code']) for r in rows)
        for r in rows:
            doc_no, stk, stored = r['doc_no'] or '', _txt(r['bsn_code']), r['unit'] or ''
            if table == 'sales_transactions':
                base, _, suffix = doc_no.rpartition('-')
                cands = [l for l in by_seq.get((base, _seq(suffix)), []) if l['code'] == stk]
            else:
                base = doc_no
                cands = [l for l in by_doc.get(base, []) if l['code'] == stk]
            entry = {'table': table, 'row_id': r['id'], 'doc_no': doc_no, 'bsn_code': stk,
                     'product_id': r['product_id'], 'date_iso': r['date_iso'],
                     'qty': r['qty'], 'stored': stored}
            # A row that COULD be a mis-read: stored as a code's old word or as the
            # code itself, on a stock code Express bills in that code.
            suspect = any(stored in (old, code) and (stk in billed[code] or stored == code)
                          for code, (old, _new) in words.items())

            if not cands:
                reason = (f'document not on the stock card (last date {last})'
                          if base not in by_doc else 'no stock-card line with this stock code'
                          + (f' at line {_seq(suffix)}' if table == 'sales_transactions' else ''))
                if suspect:
                    unrecoverable.append(dict(entry, reason=reason))
                else:
                    not_suspect[reason] += 1
                continue
            codes = {l['unit'] for l in cands}
            if len(codes) > 1:
                if codes & set(words):
                    unrecoverable.append(dict(entry, reason='%d stock-card lines answer, with '
                                              'codes %s' % (len(cands), sorted(codes))))
                continue
            code = cands[0]['unit']
            if code not in words:
                continue
            old, new = words[code]
            if stored == new:
                already[code] += 1
                continue
            if stored not in (old, code):
                unrecoverable.append(dict(entry, reason=f'stored {stored!r}, but Express wrote '
                                          f'{code!r} ({new}), which the old map read as {old!r}'))
                continue
            if n_ident[(r['doc_no'], r['bsn_code'])] > 1:
                unrecoverable.append(dict(entry, reason='%d rows share this doc_no + bsn_code'
                                          % n_ident[(r['doc_no'], r['bsn_code'])]))
                continue
            match = [l for l in cands if l['qty'] == r['qty']]
            if not match:
                unrecoverable.append(dict(entry, reason='qty %r, the stock card says %s — edited '
                                          'at source?' % (r['qty'], sorted(l['qty'] for l in cands))))
                continue
            line = match[0]
            relabel.append(dict(entry, new=new, express_doc=line['doc'],
                                express_seq=line['seq'], express_code=code,
                                express_qty=line['qty'], express_factor=line['factor']))

    per_product = collections.defaultdict(collections.Counter)
    for e in relabel:
        per_product[e['product_id']]['%s %s→%s' % (e['table'].split('_')[0], e['stored'],
                                                    e['new'])] += 1
    return {
        'words': {code: {'old': old, 'new': new} for code, (old, new) in words.items()},
        'stock_card_last_date': last,
        'relabel': relabel,
        'unrecoverable': unrecoverable,
        'counts': {
            'relabel': len(relabel),
            'by_change': dict(collections.Counter(
                '%s %s→%s' % (e['table'].split('_')[0], e['stored'], e['new']) for e in relabel)),
            'by_product': {str(k): dict(v) for k, v in sorted(per_product.items())},
            'already_new_word': dict(already),
            'unrecoverable': len(unrecoverable),
            'unmatched_not_suspect': dict(not_suspect),
        },
    }


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True, help='a COPY of prod at migration 190 or later')
    ap.add_argument('--out', default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    express_dir = os.environ.get('EXPRESS_DIR', DEFAULT_EXPRESS_DIR)
    stcrd_path = os.path.join(express_dir, 'STCRD.DBF')
    if not os.path.isfile(stcrd_path):
        print(f'REFUSED — no stock card at {stcrd_path} (set EXPRESS_DIR)')
        return 2
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'inventory_app'))
    import express_dbf_source as eds

    conn = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        plan = derive(conn, eds.open_table(express_dir, 'STCRD'))
        migration = conn.execute("SELECT MAX(filename) FROM applied_migrations").fetchone()[0]
    except DeriveRefused as exc:
        print('REFUSED —', exc)
        return 2
    finally:
        conn.close()

    plan = {'ticket': '#600', 'source': {
        'stock_card': 'BSN5657/STCRD.DBF', 'stock_card_sha256': _sha256(stcrd_path),
        'db': os.path.basename(a.db), 'db_migration': migration,
        'derived_at': datetime.datetime.now().isoformat(timespec='seconds')}, **plan}
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print(json.dumps(plan['counts'], ensure_ascii=False, indent=1))
    for e in plan['unrecoverable']:
        print('UNRECOVERABLE', e['table'], e['doc_no'], e['bsn_code'], e['stored'], '—', e['reason'])
    print('wrote', a.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
