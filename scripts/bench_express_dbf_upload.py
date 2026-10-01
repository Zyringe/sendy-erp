"""Manual prod-sized benchmark for Express DBF upload; this is not a CI test.

Posts a real daily zip through Flask's test client against a fresh SQLite
``.backup`` copy, with wall-clock timers around the import phases.

Usage:
    bench_express_dbf_upload.py SNAPSHOT_DB DAILY_ZIP [--force-older]
        [--cprofile OUTPUT.prof]

The source snapshot is opened read-only and never modified. Each run leaves its
temporary DATA_DIR in place and prints the path for inspection.
"""
import argparse
import functools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time


SCRIPT_DIR = os.path.realpath(os.path.dirname(__file__))
APP_DIR = os.path.realpath(os.path.join(SCRIPT_DIR, '..', 'inventory_app'))

# Running a file from scripts/ puts that directory at sys.path[0]. Move it to
# the end so a same-named script can never shadow an inventory_app module.
sys.path[:] = [
    path for path in sys.path
    if os.path.realpath(path or os.getcwd()) not in (SCRIPT_DIR, APP_DIR)
]
sys.path.insert(0, APP_DIR)
sys.path.append(SCRIPT_DIR)


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('snapshot', help='prod snapshot DB (opened read-only)')
    parser.add_argument('zip', help='daily Express DBF zip')
    parser.add_argument('--force-older', action='store_true')
    parser.add_argument('--cprofile', help='optional cProfile output path')
    return parser.parse_args(argv)


def _backup_copy(snapshot, data_dir):
    source = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
    target = sqlite3.connect(os.path.join(data_dir, 'inventory.db'))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def main(argv=None):
    args = _arguments(argv)
    data_dir = tempfile.mkdtemp(prefix='bench676_')
    _backup_copy(args.snapshot, data_dir)
    os.environ['DATA_DIR'] = data_dir
    os.environ.setdefault('SECRET_KEY', 'bench')
    os.environ.setdefault('ADMIN_PASSWORD', 'bench')
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

    from app import app
    import db_backup
    import express_dbf_source as eds
    import express_registers
    import import_credit_notes
    import import_express
    import import_router
    import models
    import review_rules as rr
    from blueprints import bsn
    try:
        import line_unit_correction
    except ImportError:
        line_unit_correction = None

    log = []
    depth = [0]

    def wrap(module, name, label=None):
        function = getattr(module, name)
        base = label or f'{getattr(module, "__name__", module)}.{name}'

        @functools.wraps(function)
        def timed(*call_args, **kwargs):
            call_label = (f'{base}({call_args[1]})'
                          if name == 'open_table' else base)
            level = depth[0]
            depth[0] += 1
            started = time.perf_counter()
            try:
                return function(*call_args, **kwargs)
            finally:
                depth[0] -= 1
                log.append((level, call_label, time.perf_counter() - started))

        setattr(module, name, timed)

    for name in (
            'open_table', 'build_sales_entries', 'build_purchase_entries',
            'build_invoice_refs', 'build_payments_in_records',
            'build_payments_out_records', 'build_credit_notes_ar_records',
            'build_credit_notes_ap_records', 'build_ar_snapshot_records',
            'build_ap_snapshot_records', 'build_gl_records',
            'build_sales_order_records', 'build_billing_note_records',
            'build_bank_cheque_records', 'run_document_drift_scan'):
        wrap(eds, name)
    for name in (
            'import_weekly', 'import_payment_records', 'scan_reconcile',
            'clear_import_staleness_alert', 'record_express_doc_drift_alerts',
            'clear_express_doc_drift_skipped_alert',
            'record_express_doc_drift_skipped_alert'):
        wrap(models, name)
    wrap(import_express, 'run_import_records')
    wrap(import_credit_notes, 'import_credit_note_amounts_records')
    wrap(express_registers, 'replace')
    for name in ('_upsert_invoice_refs', '_commit_snapshot',
                 '_upsert_billing_notes', '_unit_words', 'commit_express_dbf'):
        wrap(import_router, name)
    wrap(db_backup, 'guarded_backup')
    wrap(rr, 'scan_after_import')
    if hasattr(rr, 'scan_docs'):
        wrap(rr, 'scan_docs')
    for name in ('active_by_line_key', 'verdict', 'retire', 'blocking',
                 'ratio_change_blockers'):
        if line_unit_correction:
            wrap(line_unit_correction, name)
    wrap(bsn, '_claim_export_date')
    wrap(bsn, '_acquire_import_lock')

    bsn._spawn_vat_rebuild = lambda *_args, **_kwargs: log.append(
        (0, 'vat spawn (stubbed)', 0.0))
    register_spawns = []

    def stub_register_spawn(dataset_dir, run_id):
        register_spawns.append({
            'run_id': run_id,
            'files': sorted(os.listdir(dataset_dir)),
        })
        log.append((0, 'register spawn (stubbed)', 0.0))
        shutil.rmtree(dataset_dir, ignore_errors=True)

    bsn._spawn_register_import = stub_register_spawn

    client = app.test_client()
    with client.session_transaction() as session:
        session['role'] = 'admin'
        session['username'] = 'admin'
        session['user_id'] = 1

    form = {}
    if args.force_older:
        form['force_older'] = '1'

    profiler = None
    if args.cprofile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()
    started = time.perf_counter()
    with open(args.zip, 'rb') as upload:
        form['file'] = (upload, os.path.basename(args.zip))
        response = client.post(
            '/import-express-dbf/upload', data=form,
            content_type='multipart/form-data')
    total = time.perf_counter() - started
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.cprofile)

    print(f'status {response.status_code}  TOTAL {total:.2f}s  data_dir {data_dir}')
    aggregate = {}
    for level, label, elapsed in log:
        key = (level, label)
        count, seconds = aggregate.get(key, (0, 0.0))
        aggregate[key] = (count + 1, seconds + elapsed)
    for (level, label), (count, seconds) in sorted(
            aggregate.items(), key=lambda item: (item[0][0], -item[1][1])):
        if seconds >= 0.05 or level == 0:
            print(f'{"  " * level}{seconds:7.2f}s  x{count:<4} {label}')

    conn = sqlite3.connect(os.path.join(data_dir, 'inventory.db'))
    try:
        row = conn.execute(
            'SELECT id, notes FROM import_log ORDER BY id DESC LIMIT 1').fetchone()
    finally:
        conn.close()
    notes = json.loads(row[1])
    print('import_log', row[0], 'bsn.ok', (notes.get('bsn') or {}).get('ok'),
          (notes.get('bsn') or {}).get('summary')
          or (notes.get('bsn') or {}).get('error'))
    print('doc_drift keys', sorted(
        ((notes.get('bsn') or {}).get('doc_drift') or {}).keys()))
    print('register spawn called', len(register_spawns), register_spawns)
    with client.session_transaction() as session:
        print('flashes', [message[1][:120]
                          for message in session.get('_flashes', [])])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
