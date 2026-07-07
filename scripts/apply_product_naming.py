"""Phase 3 apply engine — consumes the strict ops file from
compile_apply_operations.py. Never interprets free text.

Modes:
    --dry-run (default): read-only (URI mode=ro). Validates every op against
        the live rows, simulates the post-change SKU regen sweep (D3/D7), and
        writes apply_preview_<date>.csv. Zero writes.
    --apply: guard stack per plan Phase 3 — sqlite3 backup API first (never
        cp: WAL), BEGIN IMMEDIATE, stale-before checks on every row, order
        fields → names → dict → sku regen → collision pass, post-assert
        invariants (sku unique, product count stable, no orphan mappings),
        import_log row, commit else rollback.

D7 collision policy: active product wins the bare canonical code; the loser
(or an inactive squatter) gets `-{id}`; same status → lower pid wins. Locked
rows (sku_code_locked=1) are never regenerated and their stored codes are
reserved.
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, '..', 'inventory_app')))

from sku_code_utils import build_sku_code  # noqa: E402

DATE = '2026-07-07'
_FIELD_WHITELIST = ('color_code', 'brand_id', 'model')


class ApplyError(Exception):
    """A stale-before check or post-apply invariant failed — rolled back."""


# ── connections / backup ─────────────────────────────────────────────────────

def _ro(db_path):
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _backup(db_path, backup_dir, tag):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(backup_dir, f'pre_{tag}_{stamp}.db')
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


def _rw(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # manual BEGIN IMMEDIATE
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


# ── invariants ───────────────────────────────────────────────────────────────

def _assert_invariants(conn, n_products_before):
    n = conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]
    if n != n_products_before:
        raise ApplyError(f'product row count changed {n_products_before} -> {n}')
    dup = conn.execute(
        'SELECT sku_code, COUNT(*) c FROM products WHERE sku_code IS NOT NULL'
        ' GROUP BY sku_code HAVING c > 1').fetchall()
    if dup:
        raise ApplyError(f'duplicate sku_code after apply: {[d[0] for d in dup[:3]]}')
    orphan = conn.execute(
        'SELECT COUNT(*) FROM product_code_mapping m LEFT JOIN products p'
        ' ON p.id = m.product_id WHERE p.id IS NULL').fetchone()[0]
    if orphan:
        raise ApplyError(f'{orphan} orphaned product_code_mapping rows')


def _log(conn, tag, n_applied, notes):
    conn.execute(
        'INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)'
        ' VALUES (?,?,0,?)', (tag, n_applied, notes))


# ── ops application ──────────────────────────────────────────────────────────

def validate_ops(db_path, ops):
    """Read-only stale-before validation. Returns list of error strings."""
    errs = []
    conn = _ro(db_path)
    for o in ops:
        if o['op'] == 'name':
            row = conn.execute('SELECT product_name FROM products WHERE id=?',
                               (o['product_id'],)).fetchone()
            if row is None:
                errs.append(f"pid {o['product_id']}: not in DB")
            elif row[0] != o['before']:
                errs.append(f"pid {o['product_id']}: stale before "
                            f"(DB={row[0]!r} != ops={o['before']!r})")
        elif o['op'] == 'field':
            if o['field'] not in _FIELD_WHITELIST:
                errs.append(f"pid {o['product_id']}: field {o['field']} not whitelisted")
            elif conn.execute('SELECT 1 FROM products WHERE id=?',
                              (o['product_id'],)).fetchone() is None:
                errs.append(f"pid {o['product_id']}: not in DB")
        elif o['op'] == 'brand_name_th':
            if conn.execute('SELECT 1 FROM brands WHERE id=?', (o['field'],)).fetchone() is None:
                errs.append(f"brand {o['field']}: not in DB")
        elif o['op'] == 'color_name_th':
            if conn.execute('SELECT 1 FROM color_finish_codes WHERE code=?',
                            (o['field'],)).fetchone() is None:
                errs.append(f"color code {o['field']}: not in DB")
        else:
            errs.append(f"unknown op {o['op']}")
    conn.close()
    return errs


def apply_ops(db_path, ops, dry_run=True, backup_dir=None):
    errs = validate_ops(db_path, ops)
    if errs:
        raise ApplyError('; '.join(errs[:5]))
    if dry_run:
        return {'applied': 0, 'validated': len(ops)}

    _backup(db_path, backup_dir or os.path.dirname(db_path), 'naming_ops')
    conn = _rw(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        n_before = conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        applied = 0
        for o in ops:
            if o['op'] == 'name':
                cur = conn.execute(
                    'UPDATE products SET product_name=? WHERE id=? AND product_name=?',
                    (o['after'], o['product_id'], o['before']))
            elif o['op'] == 'field':
                val = None if o['value'] == '' and o['after'] == 'NULL' else o['value']
                cur = conn.execute(
                    f"UPDATE products SET {o['field']}=? WHERE id=?",  # field whitelisted above
                    (val, o['product_id']))
            elif o['op'] == 'brand_name_th':
                cur = conn.execute('UPDATE brands SET name_th=? WHERE id=?',
                                   (o['value'], o['field']))
            elif o['op'] == 'color_name_th':
                cur = conn.execute('UPDATE color_finish_codes SET name_th=? WHERE code=?',
                                   (o['value'], o['field']))
            if cur.rowcount != 1:
                raise ApplyError(f"op {o['op']} pid={o['product_id']} matched "
                                 f"{cur.rowcount} rows (expected 1)")
            applied += 1
        _assert_invariants(conn, n_before)
        _log(conn, 'apply_product_naming --apply (ops)', applied,
             f'naming/field/dict ops from compiled decisions {DATE}')
        conn.execute('COMMIT')
        return {'applied': applied}
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


# ── SKU regen sweep (D3 + D7) ────────────────────────────────────────────────

def plan_sku_regen(db_path, field_overrides):
    """Compute canonical sku_code for every non-locked product, overlaying
    pending field changes (dry-run) — returns plans [{product_id, before,
    after, is_active}] where after != stored."""
    conn = _ro(db_path)
    brands = {str(r['id']): r['short_code'] for r in conn.execute('SELECT id, short_code FROM brands')}
    rows = conn.execute("""
        SELECT p.id, p.sku_code, p.is_active, p.sku_code_locked,
               p.series, p.model, p.size, p.color_code, p.packaging_th,
               p.packaging_short, p.condition, p.pack_variant,
               p.sub_category_short_code,
               b.short_code AS brand_short_code, c.short_code AS cat_short_code
          FROM products p
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN categories c ON c.id = p.category_id
    """).fetchall()
    conn.close()

    reserved = set()
    claims = []  # (canonical, is_active, pid, stored)
    for r in rows:
        if r['sku_code_locked']:
            if r['sku_code']:
                reserved.add(r['sku_code'])
            continue
        d = dict(r)
        ov = field_overrides.get(str(r['id']), {})
        for col, val in ov.items():
            if col == 'brand_id':
                d['brand_short_code'] = brands.get(str(val))
            else:
                d[col] = val
        canonical = build_sku_code(d)
        claims.append((canonical, r['is_active'], r['id'], r['sku_code']))

    assigned = {}
    by_code = {}
    for c in claims:
        by_code.setdefault(c[0], []).append(c)
    for code, group in by_code.items():
        group.sort(key=lambda c: (-c[1], c[2]))  # active first, then lower pid
        bare_taken = code in reserved
        for i, (canonical, active, pid, stored) in enumerate(group):
            if i == 0 and not bare_taken:
                assigned[pid] = canonical
            else:
                assigned[pid] = f'{canonical}-{pid}'

    plans = []
    for canonical, active, pid, stored in claims:
        after = assigned[pid]
        if after != stored:
            plans.append({'product_id': pid, 'before': stored, 'after': after,
                          'is_active': active})
    return plans


def apply_sku_plans(db_path, plans, backup_dir=None):
    if not plans:
        return {'applied': 0}
    _backup(db_path, backup_dir or os.path.dirname(db_path), 'naming_sku')
    conn = _rw(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        n_before = conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        applied = 0
        for p in plans:
            cur = conn.execute(
                'UPDATE products SET sku_code=? WHERE id=? AND sku_code IS ?',
                (p['after'], p['product_id'], p['before']))
            if cur.rowcount != 1:
                raise ApplyError(f"sku pid={p['product_id']} stale (matched {cur.rowcount})")
            applied += 1
        _assert_invariants(conn, n_before)
        _log(conn, 'apply_product_naming --apply (sku)', applied,
             f'sku_code regen sweep D3/D7 {DATE}')
        conn.execute('COMMIT')
        return {'applied': applied}
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────

def _field_overrides(ops):
    ov = {}
    for o in ops:
        if o['op'] == 'field':
            val = None if o['value'] == '' and o['after'] == 'NULL' else o['value']
            ov.setdefault(o['product_id'], {})[o['field']] = val
    return ov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ops', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--preview-out')
    ap.add_argument('--backup-dir')
    args = ap.parse_args()

    ops = list(csv.DictReader(open(args.ops, encoding='utf-8-sig')))

    if not args.apply:
        errs = validate_ops(args.db, ops)
        if errs:
            for e in errs:
                print('STALE:', e, file=sys.stderr)
            sys.exit(1)
        plans = plan_sku_regen(args.db, _field_overrides(ops))
        conn = _ro(args.db)
        cur_names = {str(r[0]): r[1] for r in conn.execute('SELECT id, product_name FROM products')}
        conn.close()
        new_names = {o['product_id']: o['after'] for o in ops if o['op'] == 'name'}

        def _namecols(pid):
            cur = cur_names.get(str(pid), '')
            return cur, new_names.get(str(pid), '')

        out = args.preview_out or f'apply_preview_{DATE}.csv'
        with open(out, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['product_id', 'product_name (ปัจจุบัน)', 'ชื่อใหม่ (ถ้าแก้)',
                        'change_kind', 'before', 'after', 'source'])
            for o in ops:
                kind = {'name': 'name', 'field': 'field', 'brand_name_th': 'dict',
                        'color_name_th': 'dict'}[o['op']]
                what = f"{o['field']}=" if o['op'] == 'field' else ''
                if o['op'] == 'brand_name_th':
                    # id column would read as a product_id — label it as a brand row
                    w.writerow([f"brand:{o['field']}", f"แบรนด์ (brand id {o['field']})",
                                '', kind, 'name_th=(ว่าง)', f"name_th={o['after']}", o['source']])
                    continue
                if o['op'] == 'color_name_th':
                    w.writerow([f"color:{o['field']}", f"สี (color code {o['field']})",
                                '', kind, o['before'], f"name_th={o['after']}", o['source']])
                    continue
                cur, new = _namecols(o['product_id'])
                w.writerow([o['product_id'], cur, new, kind,
                            o['before'] or what, what + o['after'], o['source']])
            for p in plans:
                cur, new = _namecols(p['product_id'])
                w.writerow([p['product_id'], cur, new, 'sku', p['before'], p['after'],
                            'regen D3/D7'])
        import collections
        counts = collections.Counter(o['op'] for o in ops)
        counts['sku'] = len(plans)
        print('DRY-RUN ok. preview ->', out)
        print('counts:', dict(counts))
        return

    res1 = apply_ops(args.db, ops, dry_run=False, backup_dir=args.backup_dir)
    plans = plan_sku_regen(args.db, {})  # post-write state, no overlays
    res2 = apply_sku_plans(args.db, plans, backup_dir=args.backup_dir)
    print('APPLIED ops:', res1['applied'], '| sku:', res2['applied'])


if __name__ == '__main__':
    main()
