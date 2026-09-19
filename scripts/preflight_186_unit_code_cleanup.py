"""Preflight for migration 186 (GH #597): what would it do to THIS database?

    ~/.virtualenvs/erp/bin/python scripts/preflight_186_unit_code_cleanup.py <db> [--json out.json]

Read-only against <db>. It copies <db> with SQLite's backup API into a temp
directory, works only on the copy and deletes it on exit. Meant to run on a
fresh prod snapshot minutes before 186 is merged (merge = deploy).

It reports:
  1. precondition violators, row by row (the migration's own precondition
     block, run on the copy and rolled back);
  2. whether the whole migration applies, through the runner's own call
     (executescript, rollback on error);
  3. rows changed per table.column (from the migration's own snapshot tables);
  4. what a ledger rebuild would post: every mapped bill line's base quantity
     through the app's own bsn_sync._get_base_qty, before vs after. A synced
     line whose quantity changes, an unsynced line that becomes syncable, or a
     line that stops resolving means stock moves the next time an import
     touches that product. All three must be 0;
  5. unit values left per covered column that the unit map does not know as a
     word (กร/ถง/บล for #600, the approved-but-unknown codes for #610), and the
     importer codes still in the tables 186 does not cover (#610).

Exit: 0 clean, 1 blocked (any finding in 1, 2 or 4), 2 could not run.
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
MIG = os.path.join(_REPO, 'data', 'migrations', '186_unit_code_cleanup.sql')

# (table, column) pairs 186 translates, in the migration's own order.
COVERED = (
    ('sales_transactions', 'unit'), ('purchase_transactions', 'unit'),
    ('products', 'unit_type'), ('promotions', 'bundle_unit'),
    ('product_code_mapping', 'bsn_unit'),
    ('pending_product_suggestions', 'bsn_unit'),
    ('pending_product_suggestions', 'suggested_unit_type'),
    ('credit_note_imports', 'unit'), ('express_sales', 'unit'),
    ('product_price_tiers', 'qty_label'), ('unit_conversions', 'bsn_unit'),
)
# Their writers rewrite raw codes on the next upload, so 186 leaves them (#610).
NOT_COVERED = (
    ('express_sales_order_lines', 'unit'), ('express_credit_note_lines', 'unit'),
    ('supplier_catalogue_items', 'unit'), ('supplier_catalogue_price_history', 'unit'),
    ('supplier_product_mapping', 'supplier_unit'), ('supplier_product_mapping', 'erp_unit'),
)
PRECONDITIONS = {
    '_mig186_pre_twin_ratio': (
        "SELECT c.id, c.product_id, c.bsn_unit, c.ratio, w.bsn_unit, w.ratio "
        "FROM _mig186_pre_twin_ratio v JOIN unit_conversions c ON c.id = v.uc_id "
        "JOIN _mig186_map m ON m.code = c.bsn_unit "
        "JOIN unit_conversions w ON w.product_id = c.product_id AND w.bsn_unit = m.word"),
    '_mig186_pre_codes_ratio': (
        "SELECT c.id, c.product_id, c.bsn_unit, c.ratio FROM _mig186_pre_codes_ratio v "
        "JOIN unit_conversions c ON c.id = v.uc_id"),
    '_mig186_pre_pcm_collision': (
        "SELECT p.id, p.bsn_code, p.bsn_unit, p.product_id FROM _mig186_pre_pcm_collision v "
        "JOIN product_code_mapping p ON p.id = v.pcm_id"),
    '_mig186_pre_tier_collision': (
        "SELECT t.id, t.product_id, t.qty_label, t.price FROM _mig186_pre_tier_collision v "
        "JOIN product_price_tiers t ON t.id = v.tier_id"),
    '_mig186_pre_unit_type_ratio': (
        "SELECT c.id, c.product_id, p.unit_type, c.bsn_unit, c.ratio "
        "FROM _mig186_pre_unit_type_ratio v JOIN unit_conversions c ON c.id = v.uc_id "
        "JOIN products p ON p.id = c.product_id"),
}


def _block(sql, name):
    return sql[sql.index(f'-- >>> {name}'):sql.index(f'-- <<< {name}')]


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
            q = _get_base_qty(conn, r['product_id'], r['unit_type'], r['unit'], r['qty'] or 0)
            out[(table, r['id'])] = (r['synced_to_stock'], q)
    return out


def _unknown_values(conn, words, pairs):
    """Per column: values the unit map does not know as a word, with counts."""
    out = {}
    for table, col in pairs:
        c = Counter()
        for (v,) in conn.execute(f"SELECT {col} FROM {table} WHERE COALESCE({col}, '') <> ''"):
            unit = v.lstrip('0123456789 ') if col == 'qty_label' else v
            if unit not in words:
                c[unit] += 1
        out[f'{table}.{col}'] = dict(c.most_common())
    return out


def run(db_path):
    """Returns (exit_code, report dict). Never writes to db_path."""
    report = {'db': os.path.abspath(db_path)}
    sql = open(MIG, encoding='utf-8').read()
    tmp = tempfile.mkdtemp(prefix='preflight186-')
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
        if conn.execute("SELECT 1 FROM applied_migrations WHERE filename = "
                        "'186_unit_code_cleanup.sql'").fetchone():
            report['error'] = '186 is already applied to this database'
            return 2, report

        # 1. preconditions, rolled back
        conn.executescript('BEGIN;' + _block(sql, 'mig186 map') + _block(sql, 'mig186 preconditions'))
        report['violators'] = {name: [list(r) for r in conn.execute(q)]
                               for name, q in PRECONDITIONS.items()}
        conn.rollback()

        # 2. the migration, the runner's way
        before = _base_qtys(conn)
        try:
            conn.executescript(sql)
        except Exception as e:                    # noqa: BLE001 - reported, not hidden
            conn.rollback()
            report['migration_error'] = str(e)
            return 1, report

        # 3. rows changed
        report['changed'] = {f'{t}.{c}': n for t, c, n in conn.execute(
            "SELECT table_name, column_name, COUNT(*) FROM migration_186_snapshot "
            "GROUP BY 1, 2 ORDER BY 1, 2")}
        report['unit_conversions_deleted'] = conn.execute(
            "SELECT COUNT(*) FROM migration_186_uc_deleted").fetchone()[0]

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
        report['lines_base_qty_changed'] = moved
        report['lines_newly_syncable'] = newly_syncable
        report['lines_newly_unresolved'] = newly_unresolved

        # 5. what is left, for #600 / #610
        words = bsn_units.full_units()
        report['left_not_a_known_word'] = _unknown_values(conn, words, COVERED)
        importer = {k for k in bsn_units.load_unit_map() if bsn_units.normalize_unit(k) != k}
        report['not_covered_importer_codes'] = {
            f'{t}.{c}': conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {c} IN ({','.join('?' * len(importer))})",
                sorted(importer)).fetchone()[0]
            for t, c in NOT_COVERED}
    finally:
        conn.close()

    blocked = (any(report['violators'].values()) or report['lines_base_qty_changed']
               or report['lines_newly_syncable'] or report['lines_newly_unresolved'])
    return (1 if blocked else 0), report


def _print(code, r):
    print(f"preflight 186 on {r['db']} (migrations up to {r.get('migration_level')})")
    if 'error' in r:
        print('  CANNOT RUN:', r['error'])
        return
    for name, rows in r['violators'].items():
        print(f"  precondition {name[12:]}: {len(rows)}")
        for row in rows[:20]:
            print('     ', row)
    if 'migration_error' in r:
        print('  MIGRATION FAILED:', r['migration_error'])
    else:
        print('  rows changed:')
        for k, n in r['changed'].items():
            print(f'    {k}: {n}')
        print(f"    unit_conversions deleted (merged): {r['unit_conversions_deleted']}")
        print(f"  bill lines compared through _get_base_qty: {r['lines_compared']}")
        for k in ('lines_base_qty_changed', 'lines_newly_syncable', 'lines_newly_unresolved'):
            print(f'    {k[6:]}: {len(r[k])}')
            for row in r[k][:20]:
                print('       ', row)
        print('  left in covered columns, not a known word (#600 / #610):')
        for k, v in r['left_not_a_known_word'].items():
            if v:
                print(f'    {k}: {v}')
        print('  importer codes in tables 186 does not cover (#610):')
        for k, n in r['not_covered_importer_codes'].items():
            print(f'    {k}: {n}')
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
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='preflight186-cfg-')
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
