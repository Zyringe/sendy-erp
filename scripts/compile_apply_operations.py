"""Compile Phase-2 decision CSVs into a strict operations file for the apply engine.

Decisions live as human-reviewed free text (decision / proposed_fix columns).
This compiler is the ONLY place that text is interpreted; anything it cannot
classify is a loud error, never a silent skip. Output schema (one op per row):

    op ∈ name | field | brand_name_th
    product_id, field, value, before, after, source

Multiple name-affecting decisions for one product are COMPOSED here into a
single final `name` op (mechanical transforms first, then insertions), so the
apply engine never has to reconcile per-row proposed names.

Usage:
    python compile_apply_operations.py --reports-dir DIR --db PATH --out OPS.csv
"""
import argparse
import csv
import os
import re
import sqlite3
import sys

DATE = "2026-07-07"

# ── mechanical name transforms (tier A classes) ──────────────────────────────

_INCH = [(re.compile(r'(\d)\s*นิ้ว'), r'\1in'), (re.compile(r'(\d)"'), r'\1in')]
_MMCM = re.compile(r'(\d)\s*(mm|cm|MM|CM)\.?')
TYPOS = [('ปุ๊ก', 'พุก'), ('บอร์น', 'บรอนซ์')]  # curated; บอร์น per Put 2026-07-07 (pid 716)


def mech_transform(cls, name):
    if cls == 'INCH_TO_IN':
        for rx, rep in _INCH:
            name = rx.sub(rep, name)
        return name
    if cls == 'MM_CM_FORMAT':
        return _MMCM.sub(lambda m: m.group(1) + m.group(2).lower(), name)
    if cls == 'TYPO_CURATED':
        for a, b in TYPOS:
            name = name.replace(a, b)
        return name
    if cls == 'PACKAGING_LEGACY':
        return name.replace('(P)', '(แผง)')
    if cls == 'PACK_VARIANT_SUFFIX':
        return name[:-2] if name.endswith(' 1') else name
    raise ValueError(f'unknown mechanical class {cls}')


# ── insertion helpers (template-ordered) ─────────────────────────────────────

_BRACKETS = ('(แผง)', '(ตัว)', '(ถุง)', '(แพ็คหัว)', '(แพ็คถุง)', '(แพ็ค)', '(ซอง)',
             '(โหล)', '(เก่า)', '(ไม่สวย)', '(ตำหนิ)', '(หมดอายุ)', '(ไม่สกรีน)',
             '(ไม่มีน็อต)', '(แผงอ่อน)', '(กล่องไม่สวย)', '(รีแพ็ค)', '(แบบเก่า)')

# earliest of: #model · numeric size token · สี token · any known bracket
_BRAND_ANCHOR = re.compile(
    r'#\S+|(?<![\wก-๙])\d[\d./x×-]*(?:in|mm|cm|kg|m|g)\b|สี\S+|\((?:แผง|ตัว|ถุง|เก่า)')


def insert_color(name, phrase):
    """Insert `สีxxx (CODE)` before the first packaging/condition bracket."""
    idxs = [name.find(b) for b in _BRACKETS if b in name]
    if not idxs:
        return f'{name} {phrase}'
    i = min(idxs)
    return f'{name[:i].rstrip()} {phrase} {name[i:]}'


def insert_brand(name, brand):
    """Insert brand per template order: after [ประเภท][ซีรีส์?], i.e. before the
    first #model / size / สี / bracket token."""
    m = _BRAND_ANCHOR.search(name)
    if not m:
        return f'{name} {brand}'
    i = m.start()
    return f'{name[:i].rstrip()} {brand} {name[i:]}'


# ── judgment-row classification ──────────────────────────────────────────────

_ADD_PHRASE = re.compile(r"add '([^']+)' to product_name", re.I)
_ADD_TARGET = re.compile(r"add '[^']+' to name -> '([^']+)'", re.I)
_SET_BRAND = re.compile(r'set brand_id\s*=\s*(\d+)', re.I)


def _action_text(row):
    d = row.get('decision') or ''
    if ':' in d and d.startswith('approved'):
        return d.split(':', 1)[1].strip()
    return (row.get('proposed_fix') or '').strip()


def classify(row):
    """Return (kind, payload) or ('error', reason). Kind ∈ noop, add_color,
    add_brand, clear_color, set_field, brand_name_th, pid716."""
    t = _action_text(row)
    tl = t.lower()
    if 'no change' in tl or tl.startswith('keep color_code'):
        return 'noop', None
    if 'เกือกม้า' in t:
        return 'brand_name_th', ('22', 'เกือกม้า')
    if 'บอร์น' in t:
        return 'pid716', None
    if "update products.model to '#o'" in tl:
        return 'set_field', ('model', '#O')
    m = _SET_BRAND.search(t)
    if m:
        return 'set_field', ('brand_id', m.group(1))
    if 'clear color_code' in tl or ('color_code' in tl and ('null' in tl or 'duplicates' in tl)):
        return 'clear_color', None
    m = _ADD_TARGET.search(t)
    if m:
        return 'name_target', m.group(1)   # explicit final name in the decision
    m = _ADD_PHRASE.search(t)
    if m:
        phrase = m.group(1)
        if phrase.startswith('สี'):
            return 'add_color', phrase
        return 'add_brand', phrase.strip("'\" ")
    if 'add sendai to name per rule 4' in tl or "add brand 'sendai'" in tl:
        return 'add_brand', 'Sendai'
    return 'error', f"pid {row['product_id']}: unclassifiable approved text: {t[:80]}"


def compile_judgment(rows):
    """rows = triaged CSV dicts. Returns (ops, errors). Name insertions are
    returned as intent tuples for later composition (kind, pid, payload)."""
    ops, errors = [], []
    for r in rows:
        d = r.get('decision') or ''
        if not d.startswith('approved'):
            continue  # closed / decided / pending / empty → no DB change
        kind, payload = classify(r)
        if kind == 'noop':
            continue
        if kind == 'error':
            errors.append(payload)
        elif kind == 'brand_name_th':
            ops.append({'op': 'brand_name_th', 'product_id': '', 'field': payload[0],
                        'value': payload[1], 'before': '', 'after': payload[1],
                        'source': f"judgment pid {r['product_id']}"})
        elif kind == 'clear_color':
            ops.append({'op': 'field', 'product_id': r['product_id'], 'field': 'color_code',
                        'value': '', 'before': '', 'after': 'NULL',
                        'source': f"judgment {r['issue']}"})
        elif kind == 'set_field':
            ops.append({'op': 'field', 'product_id': r['product_id'], 'field': payload[0],
                        'value': payload[1], 'before': '', 'after': payload[1],
                        'source': f"judgment {r['issue']}"})
        elif kind == 'add_color':
            ops.append({'op': 'insert_color', 'product_id': r['product_id'], 'field': '',
                        'value': payload, 'before': '', 'after': '',
                        'source': f"judgment {r['issue']}"})
        elif kind == 'add_brand':
            ops.append({'op': 'insert_brand', 'product_id': r['product_id'], 'field': '',
                        'value': payload, 'before': '', 'after': '',
                        'source': f"judgment {r['issue']}"})
        elif kind == 'name_target':
            ops.append({'op': 'name_target', 'product_id': r['product_id'], 'field': '',
                        'value': payload, 'before': '', 'after': '',
                        'source': f"judgment {r['issue']} explicit target"})
        elif kind == 'pid716':
            ops.append({'op': 'name_swap', 'product_id': r['product_id'], 'field': '',
                        'value': 'เคลือบบอร์น=>เคลือบบรอนซ์', 'before': '', 'after': '',
                        'source': 'judgment pid716'})
            ops.append({'op': 'field', 'product_id': r['product_id'], 'field': 'color_code',
                        'value': 'BZ', 'before': '', 'after': 'BZ', 'source': 'judgment pid716'})
    # dedupe brand_name_th (16 rows → 1 op)
    seen, out = set(), []
    for o in ops:
        key = (o['op'], o['product_id'], o['field'], o['value'])
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out, errors


# ── full compile: mechanical + judgment, composed per product ────────────────

def compile_all(reports_dir, db_path):
    mech = list(csv.DictReader(open(
        os.path.join(reports_dir, f'audit_mechanical_approved_{DATE}.csv'), encoding='utf-8-sig')))
    tri = list(csv.DictReader(open(
        os.path.join(reports_dir, f'audit_judgment_triaged_{DATE}.csv'), encoding='utf-8-sig')))

    jops, errors = compile_judgment(tri)

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    names = {str(r[0]): r[1] for r in conn.execute('SELECT id, product_name FROM products')}
    conn.close()

    # group mechanical approved rows per product, keep class order stable
    mech_by_pid = {}
    for r in mech:
        if r['decision'] != 'approved':
            continue
        mech_by_pid.setdefault(r['product_id'], []).append(r)

    name_final, ops = {}, []
    for pid, rows_ in mech_by_pid.items():
        cur = names.get(pid)
        if cur is None:
            errors.append(f'pid {pid}: in mechanical file but not in DB')
            continue
        composed = cur
        for r in sorted(rows_, key=lambda x: x['class']):
            composed = mech_transform(r['class'], composed)
        if len(rows_) == 1 and composed != rows_[0]['proposed_name']:
            errors.append(f"pid {pid}: mech compose mismatch: {composed!r} != {rows_[0]['proposed_name']!r}")
        name_final[pid] = composed

    # judgment name intents compose on top of any mechanical result
    for o in jops:
        if o['op'] in ('insert_color', 'insert_brand', 'name_swap', 'name_target'):
            pid = o['product_id']
            cur = name_final.get(pid, names.get(pid))
            if cur is None:
                errors.append(f'pid {pid}: judgment name op but product not in DB')
                continue
            if o['op'] == 'insert_color':
                name_final[pid] = insert_color(cur, o['value'])
            elif o['op'] == 'insert_brand':
                name_final[pid] = insert_brand(cur, o['value'])
            elif o['op'] == 'name_target':
                if pid in name_final and name_final[pid] != names.get(pid):
                    errors.append(f'pid {pid}: explicit target clashes with another name op')
                    continue
                name_final[pid] = o['value']
            else:
                a, b = o['value'].split('=>')
                if a not in cur:
                    errors.append(f'pid {pid}: name_swap token {a!r} absent from {cur!r}')
                    continue
                name_final[pid] = cur.replace(a, b)
        else:
            ops.append(o)

    for pid, after in sorted(name_final.items(), key=lambda x: int(x[0])):
        before = names[pid]
        if after != before:
            ops.append({'op': 'name', 'product_id': pid, 'field': '', 'value': '',
                        'before': before, 'after': after, 'source': 'composed'})
    return ops, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reports-dir', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    ops, errors = compile_all(args.reports_dir, args.db)
    if errors:
        for e in errors:
            print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
    with open(args.out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['op', 'product_id', 'field', 'value', 'before', 'after', 'source'])
        w.writeheader()
        w.writerows(ops)
    import collections
    print('ops written:', len(ops), dict(collections.Counter(o['op'] for o in ops)))


if __name__ == '__main__':
    main()
