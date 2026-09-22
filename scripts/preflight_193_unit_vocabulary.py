"""Preflight for migration 193 (GH #610): what would it do to THIS database?

    ~/.virtualenvs/erp/bin/python scripts/preflight_193_unit_vocabulary.py <db> [--json out.json]

Read-only against <db>. It copies <db> with SQLite's backup API into a temp
directory, works only on the copy and deletes it on exit. Meant to run on a
fresh prod snapshot minutes before 193 is merged (merge = deploy).

It reports:
  1. precondition violators, row by row (the migration's own seed, learn, map
     and precondition blocks, run on the copy and rolled back);
  2. whether the whole migration applies, the runner's way (executescript,
     rollback on error);
  3. rows changed per table.column, conversions merged away, map rows added,
     and the bill lines 193 leaves untranslated (migration_193_skipped) with
     what each resolves to now and would resolve to as the word;
  4. base quantities through the app's own bsn_sync._get_base_qty for every
     mapped bill line, before vs after: the per-table totals, and every line
     whose quantity changed, that became syncable or that stopped resolving.
     All three lists must be empty;
  5. the invariant, on this DB's own rows: every value 193 wrote equals what
     bsn_units produces for the value it replaced (normalize_unit in the
     column's book, normalize_tier_label for tier labels), and no covered
     column still holds a spelling the map translates outside the listed skips;
  6. per covered column, the values left that the map does not know as a word
     (supplier spellings such as กป. or ปอนด์ that are not in the approved list).

Exit: 0 clean, 1 blocked (any finding in 1, 2, 4 or 5), 2 could not run.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_APP = os.path.join(_REPO, 'inventory_app')
MIG = os.path.join(_REPO, 'data', 'migrations', '193_unit_vocabulary.sql')
MIG_NAME = os.path.basename(MIG)

# (table, column, book) for every column 193 covers.
COVERED = (
    ('sales_transactions', 'unit', 'BSN5657'), ('purchase_transactions', 'unit', 'BSN5657'),
    ('products', 'unit_type', 'BSN5657'), ('promotions', 'bundle_unit', 'BSN5657'),
    ('product_code_mapping', 'bsn_unit', 'BSN5657'),
    ('pending_product_suggestions', 'bsn_unit', 'BSN5657'),
    ('pending_product_suggestions', 'suggested_unit_type', 'BSN5657'),
    ('credit_note_imports', 'unit', 'BSN5657'), ('express_sales', 'unit', 'BSN5657'),
    ('product_price_tiers', 'qty_label', 'BSN5657'), ('unit_conversions', 'bsn_unit', 'BSN5657'),
    ('express_sales_order_lines', 'unit', 'BSN5657'),
    ('express_credit_note_lines', 'unit', 'BSN5657'),
    ('supplier_catalogue_items', 'unit', '*'), ('supplier_catalogue_price_history', 'unit', '*'),
)
PRECONDITIONS = {
    'seed_conflict': (
        "SELECT u.id, u.book, u.spelling, u.word, s.word FROM _mig193_pre_seed_conflict v "
        "JOIN unit_map u ON u.id = v.map_id "
        "JOIN _mig193_seed s ON s.book = u.book AND s.spelling = u.spelling"),
    'twin_ratio': (
        "SELECT c.id, c.product_id, c.bsn_unit, c.ratio, w.bsn_unit, w.ratio "
        "FROM _mig193_pre_twin_ratio v JOIN unit_conversions c ON c.id = v.uc_id "
        "JOIN _mig193_map m ON m.source = 'BSN5657' AND m.spelling = c.bsn_unit "
        "JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = m.word"),
    'codes_ratio': (
        "SELECT c.id, c.product_id, c.bsn_unit, c.ratio FROM _mig193_pre_codes_ratio v "
        "JOIN unit_conversions c ON c.id = v.uc_id"),
    'pcm_collision': (
        "SELECT p.id, p.bsn_code, p.bsn_unit, p.product_id FROM _mig193_pre_pcm_collision v "
        "JOIN product_code_mapping p ON p.id = v.pcm_id"),
    'tier_collision': (
        "SELECT t.id, t.product_id, t.qty_label, t.price FROM _mig193_pre_tier_collision v "
        "JOIN product_price_tiers t ON t.id = v.tier_id"),
    'unit_type_ratio': (
        "SELECT c.id, c.product_id, p.unit_type, c.bsn_unit, c.ratio "
        "FROM _mig193_pre_unit_type_ratio v JOIN unit_conversions c ON c.id = v.uc_id "
        "JOIN products p ON p.id = c.product_id"),
}


def _block(sql, name):
    return sql[sql.index(f'-- >>> {name}\n'):sql.index(f'-- <<< {name}\n')]


def _copy(src, dst):
    """Backup API from a read-only connection. A snapshot pulled from prod has
    no -shm, which mode=ro cannot open in WAL mode; immutable=1 can."""
    try:
        s = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
        s.execute('SELECT 1 FROM sqlite_master LIMIT 1')
    except sqlite3.OperationalError:
        s = sqlite3.connect(f'file:{src}?immutable=1', uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def _base_qtys(conn):
    """{(table, id): (synced, base_qty or None)} for every mapped, stock-moving
    bill line, the way _sync_bsn_to_stock would compute it right now."""
    from models.bsn_sync import _get_base_qty
    from models.stock_filters import is_non_stock_code
    out = {}
    for table in ('sales_transactions', 'purchase_transactions'):
        for r in conn.execute(
                f"SELECT t.id, t.bsn_code, t.unit, t.qty, t.synced_to_stock, t.product_id, "
                f"p.unit_type FROM {table} t JOIN products p ON p.id = t.product_id"):
            if is_non_stock_code(r['bsn_code']):
                continue
            q = _get_base_qty(conn, r['product_id'], r['unit_type'] or '', r['unit'], r['qty'] or 0)
            out[(table, r['id'])] = (r['synced_to_stock'], q)
    return out


def _totals(qtys):
    out = {}
    for (table, _id), (_s, q) in qtys.items():
        t = out.setdefault(table, {'lines': 0, 'resolved': 0, 'base_qty': 0.0})
        t['lines'] += 1
        if q is not None:
            t['resolved'] += 1
            t['base_qty'] += q
    for t in out.values():
        t['base_qty'] = round(t['base_qty'], 4)
    return out


def run(db_path):
    """Returns (exit_code, report dict). Never writes to db_path."""
    report = {'db': os.path.abspath(db_path)}
    sql = open(MIG, encoding='utf-8').read()
    tmp = tempfile.mkdtemp(prefix='preflight193-')
    try:
        copy = os.path.join(tmp, 'inventory.db')
        _copy(db_path, copy)
        conn = sqlite3.connect(copy)
        return _run_on_copy(conn, sql, report)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_on_copy(conn, sql, report):
    import bsn_units
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        report['migration_level'] = conn.execute(
            "SELECT MAX(filename) FROM applied_migrations").fetchone()[0]
        if conn.execute("SELECT 1 FROM applied_migrations WHERE filename = ?", (MIG_NAME,)).fetchone():
            report['error'] = '193 is already applied to this database'
            return 2, report

        # 1. preconditions, rolled back
        conn.executescript(
            'BEGIN;' + _block(sql, 'mig193 seeds') + _block(sql, 'mig193 seed precondition')
            + 'CREATE TEMP TABLE migration_193_unit_map_added (book TEXT, spelling TEXT, word TEXT);'
            + _block(sql, 'mig193 learn') + _block(sql, 'mig193 map')
            + _block(sql, 'mig193 preconditions'))
        report['violators'] = {name: [list(r) for r in conn.execute(q)]
                               for name, q in PRECONDITIONS.items()}
        conn.rollback()
        conn.execute('DROP TABLE IF EXISTS temp.migration_193_unit_map_added')

        # 2. the migration, the runner's way
        before = _base_qtys(conn)
        try:
            conn.executescript(sql)
        except Exception as e:                    # noqa: BLE001 - reported, not hidden
            conn.rollback()
            report['migration_error'] = str(e)
            return 1, report

        # 3. what changed
        report['changed'] = {f'{t}.{c}': n for t, c, n in conn.execute(
            "SELECT table_name, column_name, COUNT(*) FROM migration_193_snapshot "
            "GROUP BY 1, 2 ORDER BY 1, 2")}
        report['changed_pairs'] = {f'{t}.{c}': {f'{o} -> {n}': k for o, n, k in conn.execute(
            "SELECT old_value, new_value, COUNT(*) FROM migration_193_snapshot "
            "WHERE table_name = ? AND column_name = ? GROUP BY 1, 2 ORDER BY 3 DESC", (t, c))}
            for t, c in {(r[0], r[1]) for r in conn.execute(
                "SELECT DISTINCT table_name, column_name FROM migration_193_snapshot")}}
        report['unit_conversions_deleted'] = [list(r) for r in conn.execute(
            "SELECT id, product_id, bsn_unit, ratio FROM migration_193_uc_deleted ORDER BY id")]
        report['unit_map_added'] = [list(r) for r in conn.execute(
            "SELECT book, spelling, word FROM migration_193_unit_map_added ORDER BY 1, 2")]
        report['skipped'] = [list(r) for r in conn.execute(
            "SELECT k.table_name, k.row_id, k.product_id, k.unit, k.word, k.detail, "
            "COALESCE(s.doc_no, p.doc_no) FROM migration_193_skipped k "
            "LEFT JOIN sales_transactions s ON k.table_name = 'sales_transactions' AND s.id = k.row_id "
            "LEFT JOIN purchase_transactions p ON k.table_name = 'purchase_transactions' AND p.id = k.row_id "
            "ORDER BY 1, 2")]

        # 4. what a ledger rebuild would post
        after = _base_qtys(conn)
        moved, newly_syncable, newly_unresolved = [], [], []
        for key, (synced, q0) in before.items():
            q1 = after[key][1]
            if q0 is None and q1 is not None:
                (moved if synced else newly_syncable).append([*key, q0, q1])
            elif q0 is not None and q1 is None:
                newly_unresolved.append([*key, q0, q1])
            elif q0 is not None and abs(q0 - q1) > 1e-9:
                moved.append([*key, q0, q1])
        report['lines_compared'] = len(before)
        report['base_qty_before'] = _totals(before)
        report['base_qty_after'] = _totals(after)
        report['lines_base_qty_changed'] = moved
        report['lines_newly_syncable'] = newly_syncable
        report['lines_newly_unresolved'] = newly_unresolved

        # 5. the invariant, on this DB's own rows
        books = {(t, c): b for t, c, b in COVERED}
        mismatch = []
        for t, c, old, new in conn.execute(
                "SELECT table_name, column_name, old_value, new_value FROM migration_193_snapshot"):
            if c == 'qty_label':
                want = bsn_units.normalize_tier_label(old, conn=conn)
            else:
                want = bsn_units.normalize_unit(old, books[(t, c)], conn=conn)
            if want != new:
                mismatch.append([t, c, old, new, want])
        report['invariant_mismatches'] = mismatch
        skipped = {(r[0], r[1]) for r in report['skipped']}
        left = {}
        unknown = {}
        words = bsn_units.full_units(conn=conn)
        for t, c, book in COVERED:
            behind, odd = Counter(), Counter()
            for rid, v in conn.execute(f"SELECT id, {c} FROM {t} WHERE COALESCE({c}, '') <> ''"):
                if c == 'qty_label':
                    unit = bsn_units.split_tier_label(v)[1]
                    translated = bsn_units.normalize_tier_label(v, conn=conn) != v
                else:
                    unit = v
                    translated = bsn_units.normalize_unit(v, book, conn=conn) != v
                if translated and (t, rid) not in skipped:
                    behind[v] += 1
                elif not translated and unit not in words:
                    odd[unit] += 1
            left[f'{t}.{c}'] = dict(behind.most_common())
            unknown[f'{t}.{c}'] = dict(odd.most_common())
        report['left_behind'] = {k: v for k, v in left.items() if v}
        report['not_a_known_word'] = {k: v for k, v in unknown.items() if v}
    finally:
        conn.close()

    blocked = (any(report['violators'].values()) or report['lines_base_qty_changed']
               or report['lines_newly_syncable'] or report['lines_newly_unresolved']
               or report['invariant_mismatches'] or report['left_behind'])
    return (1 if blocked else 0), report


def _print(code, r):
    print(f"preflight 193 on {r['db']} (migrations up to {r.get('migration_level')})")
    if 'error' in r:
        print('  CANNOT RUN:', r['error'])
        return
    for name, rows in r['violators'].items():
        print(f'  precondition {name}: {len(rows)}')
        for row in rows[:20]:
            print('     ', row)
    if 'migration_error' in r:
        print('  MIGRATION FAILED:', r['migration_error'])
    else:
        print('  rows changed:')
        for k, n in r['changed'].items():
            print(f'    {k}: {n}   {r["changed_pairs"][k]}')
        print(f"    unit_conversions deleted (merged): {len(r['unit_conversions_deleted'])}")
        for row in r['unit_conversions_deleted']:
            print('       ', row)
        print(f"    unit_map rows added: {len(r['unit_map_added'])}")
        print(f"  bill lines left untranslated (their word would resolve differently): "
              f"{len(r['skipped'])}")
        for row in r['skipped']:
            print('     ', row)
        print(f"  bill lines compared through _get_base_qty: {r['lines_compared']}")
        for table, t in r['base_qty_before'].items():
            a = r['base_qty_after'][table]
            print(f"    {table}: base qty {t['base_qty']} -> {a['base_qty']}, "
                  f"resolved {t['resolved']} -> {a['resolved']} of {t['lines']}")
        for k in ('lines_base_qty_changed', 'lines_newly_syncable', 'lines_newly_unresolved'):
            print(f'    {k[6:]}: {len(r[k])}')
            for row in r[k][:20]:
                print('       ', row)
        print(f"  invariant (value written == bsn_units of the value replaced): "
              f"{len(r['invariant_mismatches'])} mismatches")
        for row in r['invariant_mismatches'][:20]:
            print('     ', row)
        print('  left behind (the map translates it, 193 did not):', r['left_behind'] or 'none')
        print('  values the map does not know as a word (left as written):')
        for k, v in r['not_a_known_word'].items():
            print(f'    {k}: {v}')
    print('RESULT:', {0: 'CLEAN', 1: 'BLOCKED', 2: 'COULD NOT RUN'}[code])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('db')
    ap.add_argument('--json')
    a = ap.parse_args(argv)
    if not os.path.isfile(a.db):
        print(f'no such file: {a.db}', file=sys.stderr)
        return 2
    # Point the app's config at a throwaway dir BEFORE importing any app module,
    # so nothing can open the real dev DB by default.
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='preflight193-cfg-')
    os.environ['SKIP_DB_INIT'] = '1'
    if _APP not in sys.path:
        sys.path.insert(0, _APP)
    code, report = run(a.db)
    _print(code, report)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    return code


if __name__ == '__main__':
    sys.exit(main())
