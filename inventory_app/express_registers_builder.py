"""Build the three slow Express reference registers in a detached process."""
import argparse
import fcntl
import json
import os
import shutil
import sqlite3
from datetime import datetime

import bsn_units
import import_router


def _empty_results(reason):
    return {
        'general_ledger': {
            'accounts': 0, 'vouchers': 0, 'lines': 0, 'skipped': reason},
        'sales_orders': {'orders': 0, 'lines': 0, 'skipped': reason},
        'bank_cheques': {'stored': 0, 'skipped': reason},
    }


def _newer_run_owns_registers(db_path, run_id):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute('PRAGMA busy_timeout=10000')
        rows = conn.execute(
            "SELECT notes FROM import_log "
            "WHERE filename='express-dbf-upload' AND id>? ORDER BY id DESC",
            (run_id,)).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            notes = json.loads(row[0] or '{}')
        except (TypeError, ValueError):
            continue
        if isinstance(notes, dict) and 'registers' in notes:
            return True
    return False


def _report_result(db_path, run_id, result):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute('PRAGMA busy_timeout=10000')
        row = conn.execute(
            'SELECT notes FROM import_log WHERE id=?', (run_id,)).fetchone()
        notes = json.loads(row[0]) if row and row[0] else {}
        notes['registers'] = result
        conn.execute('UPDATE import_log SET notes=? WHERE id=?',
                     (json.dumps(notes, ensure_ascii=False), run_id))
        conn.commit()
    finally:
        conn.close()


def _finished(status, per_register, error=None):
    result = {
        'status': status,
        **per_register,
        'finished_at': datetime.now().isoformat(timespec='seconds'),
    }
    if error is not None:
        result['error'] = str(error)[:400]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True,
                        help='directory holding the copied register DBF tables')
    parser.add_argument('--db', required=True, help='main inventory.db path')
    parser.add_argument('--run-id', required=True, type=int,
                        help='import_log row id to update')
    parser.add_argument('--cleanup-dir', required=True,
                        help='copied dataset directory to remove at exit')
    args = parser.parse_args(argv)

    lock_fd = None
    try:
        lock_fd = os.open(args.db + '.registers.lock', os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        if _newer_run_owns_registers(args.db, args.run_id):
            result = _finished(
                'superseded', _empty_results('superseded by a newer upload'))
            _report_result(args.db, args.run_id, result)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        try:
            per_register = import_router.commit_express_registers(
                args.source, args.db, bsn_units.DEFAULT_BOOK)
        except BaseException as exc:
            result = _finished(
                'error', _empty_results('job failed before register import'), exc)
            _report_result(args.db, args.run_id, result)
            raise

        status = ('error' if any('error' in stats
                                 for stats in per_register.values()) else 'done')
        result = _finished(status, per_register)
        _report_result(args.db, args.run_id, result)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if status == 'error' else 0
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        shutil.rmtree(args.cleanup_dir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
