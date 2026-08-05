"""Build vat_book.db — a Sendy-schema snapshot of an Express VAT company file
(xp5, บจก.บุญสวัสดิ์ นำชัย's ภพ.30 book) for the read-only VAT-book view.

SUBPROCESS-ONLY contract
------------------------
Every Sendy importer this reuses (init_db, models.import_weekly via
import_router.commit_express_dbf) writes through config.DATABASE_PATH, which is
fixed at import time from the DATA_DIR env var. So the caller MUST run this in
a fresh subprocess with DATA_DIR pointed at an EMPTY build directory:

    env = {**os.environ, 'DATA_DIR': build_dir, 'VAT_BOOK_BUILD': '1'}
    subprocess.run([sys.executable, 'vat_book_builder.py', '--source', dbf_dir],
                   cwd=<inventory_app dir>, env=env)

Two refuse-guards make the wrong invocation fail loud instead of writing into
the live DB: VAT_BOOK_BUILD=1 must be set, and the target DB must not exist yet
(the live DB always exists). The finished artifact is <build_dir>/inventory.db,
finalized self-contained (journal_mode=DELETE, no -wal/-shm, integrity-checked);
the caller renames/moves it into place (see blueprints/bsn.py).

Fill order: fresh schema → products+mapping seeded from STMAS (so every STKCOD
resolves during import) → commit_express_dbf(since_days=None) = full history
through the six REAL importers → stock_levels overwritten from STMAS.TOTBAL
(the book's own stock, oracle-checked vs Σ STLOC.LOCBAL) → isvat_raw dump →
book_meta → finalize.
"""
import argparse
import fcntl
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime

import bsn_units


def seed_companies(conn):
    """Fresh schema.sql builds carry no seed rows; scripts/import_express's
    payments/credit-notes importers require companies code='BSN' (originally
    seeded by mig 011, which the bootstrap runner stamps without executing).
    Mirror that seed here."""
    conn.executemany(
        "INSERT INTO companies (code, name_th, short_name) VALUES (?, ?, ?) "
        "ON CONFLICT(code) DO NOTHING",
        [('BSN', 'บุญสวัสดิ์ นำชัย', 'BSN'),
         ('SD', 'เซ็นไดเทรดดิ้ง', 'Sendai Trading')])
    conn.commit()


def _stmas_cost(r):
    """STMAS.UNITPR (Express's own maintained average unit cost, ex-VAT) —
    the VAT book's cost_price source (plan §4.2, replacing the old partial-
    coverage WACC-from-imported-purchases). Blank/non-numeric/<=0 -> 0.0;
    nothing invents a cost."""
    try:
        val = float(r.get('UNITPR') or 0)
    except (TypeError, ValueError):
        return 0.0
    return val if val > 0 else 0.0


def seed_products_from_stmas(conn, stmas_rows):
    """Create one product + one catch-all mapping row per STMAS code.
    Returns {stkcod: product_id}. Blank STKDES falls back to the code itself
    (product_name is NOT NULL); duplicate STKCOD keeps the first row.
    cost_price := STMAS.UNITPR (plan §4.2) — the VAT book is a mirror of
    Express, so Express's own valuation is the truth everywhere it shows."""
    code_to_pid = {}
    for r in stmas_rows:
        code = str(r.get('STKCOD') or '').strip()
        if not code or code in code_to_pid:
            continue
        name = str(r.get('STKDES') or '').strip() or code
        unit = bsn_units.normalize_unit(str(r.get('QUCOD') or '').strip()) or 'ตัว'
        cost = _stmas_cost(r)
        cur = conn.execute(
            "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, ?)",
            (name, unit, cost))
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
            "VALUES (?, ?, ?, '')",
            (code, name, pid))
        code_to_pid[code] = pid
    conn.commit()
    return code_to_pid


def overwrite_stock_from_stmas(conn, stmas_rows, stloc_rows, code_to_pid):
    """stock_levels := STMAS.TOTBAL (the tax book's own on-hand — NOT physical
    stock). Oracle: per code, TOTBAL must equal Σ STLOC.LOCBAL (the same
    internal invariant that holds 5,429/5,429 on BSN5657); any mismatch aborts
    the build rather than publishing an unexplained number."""
    loc_sum = {}
    for r in stloc_rows:
        code = str(r.get('STKCOD') or '').strip()
        loc_sum[code] = loc_sum.get(code, 0.0) + float(r.get('LOCBAL') or 0)

    mismatches = []
    conn.execute("DELETE FROM stock_levels")
    for r in stmas_rows:
        code = str(r.get('STKCOD') or '').strip()
        pid = code_to_pid.get(code)
        if pid is None:
            continue
        totbal = float(r.get('TOTBAL') or 0)
        if abs(totbal - loc_sum.get(code, 0.0)) > 1e-6:
            mismatches.append((code, totbal, loc_sum.get(code, 0.0)))
            continue
        conn.execute(
            "INSERT INTO stock_levels (product_id, quantity) VALUES (?, ?) "
            "ON CONFLICT(product_id) DO UPDATE SET quantity=excluded.quantity",
            (pid, totbal))
    if mismatches:
        conn.rollback()
        raise ValueError(
            f"STMAS.TOTBAL != Σ STLOC.LOCBAL for {len(mismatches)} codes "
            f"(first 5: {mismatches[:5]}) — refusing to publish")
    conn.commit()


def dump_stmas_meta(conn, stmas_rows):
    """Per-STKCOD STKGRP (Express's own category) + VATCOD (tax type at
    invoice time) — needed by the vat-substitute candidate/guess filters
    (plan §2/§5, decision 8: VATCOD != '1' is excluded from suggestions;
    STKGRP is the category bridge for identity-mapped guesses) but with no
    column on the Sendy-shape `products` table seed_products_from_stmas
    writes. Book-only artifact, same footing as isvat_raw: NOT part of the
    shared migration-managed schema.sql (no analog in the main book), so it
    is created directly here rather than via a migration. Duplicate STKCOD
    keeps the first row, matching seed_products_from_stmas' own dedup rule."""
    conn.execute("DROP TABLE IF EXISTS stmas_meta")
    conn.execute(
        "CREATE TABLE stmas_meta (stkcod TEXT PRIMARY KEY, stkgrp TEXT NOT NULL, "
        "vatcod TEXT NOT NULL)")
    seen = set()
    rows = []
    for r in stmas_rows:
        code = str(r.get('STKCOD') or '').strip()
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append((code, str(r.get('STKGRP') or '').strip(),
                     str(r.get('VATCOD') or '').strip()))
    conn.executemany("INSERT INTO stmas_meta VALUES (?, ?, ?)", rows)
    conn.commit()
    return len(rows)


_IDENT = re.compile(r'[^A-Za-z0-9_]')


def dump_isvat(conn, isvat_rows):
    """Raw ISVAT dump (ภพ.30 filing lines) so the deferred VAT-summary page
    needs no importer change later. Columns mirror the DBF fields."""
    if not isvat_rows:
        return 0
    cols = [_IDENT.sub('_', str(k)) for k in isvat_rows[0].keys()]
    conn.execute("DROP TABLE IF EXISTS isvat_raw")
    conn.execute("CREATE TABLE isvat_raw (%s)" % ", ".join(f'"{c}"' for c in cols))
    ins = "INSERT INTO isvat_raw VALUES (%s)" % ",".join("?" * len(cols))
    for r in isvat_rows:
        conn.execute(ins, [
            v.isoformat() if hasattr(v, 'isoformat') else v for v in r.values()])
    conn.commit()
    return len(isvat_rows)


def write_book_meta(conn, source_dir, isinfo_rows, counts):
    info = isinfo_rows[0] if isinfo_rows else {}
    conn.execute("DROP TABLE IF EXISTS book_meta")
    conn.execute("CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rows = {
        'built_at': datetime.now().isoformat(timespec='seconds'),
        'source_dir': os.path.basename(os.path.normpath(source_dir)),
        'company_name': str(info.get('THINAM') or '').strip(),
        'tax_id': str(info.get('TAXID') or '').strip(),
        'counts': json.dumps(counts, ensure_ascii=False),
    }
    conn.executemany("INSERT INTO book_meta (key, value) VALUES (?, ?)",
                     rows.items())
    conn.commit()


def finalize(db_path):
    """Make the artifact a single self-contained file: checkpoint + drop WAL,
    integrity-check, then fsync file and directory. Aborts on any failure."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode.lower() != 'delete':
            raise RuntimeError(f"journal_mode is {mode!r}, expected delete")
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != 'ok':
            raise RuntimeError(f"integrity_check failed: {ok}")
    finally:
        conn.close()
    # After a VERIFIED switch to journal_mode=delete (checkpointed, integrity
    # ok), leftover sidecars are stale artifacts — SQLite on macOS can leave
    # the -shm behind until process exit. Remove, then require both gone.
    for suffix in ('-wal', '-shm'):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    for suffix in ('-wal', '-shm'):
        if os.path.exists(db_path + suffix):
            raise RuntimeError(f"sidecar left behind: {db_path + suffix}")
    fd = os.open(db_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(os.path.dirname(db_path) or '.', os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _guard_subprocess_target():
    if os.environ.get('VAT_BOOK_BUILD') != '1':
        raise SystemExit(
            "refusing: VAT_BOOK_BUILD=1 not set — this tool must run in a "
            "dedicated subprocess with DATA_DIR pointed at an empty build dir")
    import config
    if os.path.exists(config.DATABASE_PATH):
        raise SystemExit(
            f"refusing: target DB already exists: {config.DATABASE_PATH} "
            "(the build dir must be empty — never the live DATA_DIR)")
    return config.DATABASE_PATH


def build(source_dir):
    """Full build at config.DATABASE_PATH (guarded). Returns a summary dict."""
    db_path = _guard_subprocess_target()

    import database
    import express_dbf_source as eds
    import import_router

    database.init_db()

    stmas = eds.open_table(source_dir, 'STMAS')
    stloc = eds.open_table(source_dir, 'STLOC')
    isvat = eds.open_table(source_dir, 'ISVAT')
    isinfo = eds.open_table(source_dir, 'ISINFO')

    conn = database.get_connection()
    try:
        seed_companies(conn)
        code_to_pid = seed_products_from_stmas(conn, stmas)
        per_type = import_router.commit_express_dbf(
            source_dir, db_path=db_path, since_days=None)
        overwrite_stock_from_stmas(conn, stmas, stloc, code_to_pid)
        isvat_n = dump_isvat(conn, isvat)
        stmas_meta_n = dump_stmas_meta(conn, stmas)
        counts = {
            'products': len(code_to_pid),
            'isvat_rows': isvat_n,
            'stmas_meta_rows': stmas_meta_n,
            'sales_imported': per_type['sales']['imported'],
            'purchase_imported': per_type['purchase']['imported'],
            'payments_in': per_type['payments_in']['imported'],
            'payments_out': per_type['payments_out']['imported'],
            'credit_notes_ar': per_type['credit_notes_ar']['upserted'],
            'credit_notes_ap': per_type['credit_notes_ap']['imported'],
        }
        write_book_meta(conn, source_dir, isinfo, counts)
    finally:
        conn.close()

    finalize(db_path)
    return {'db_path': db_path, 'counts': counts}


def publish(built_path, target_path):
    """Copy the finished artifact next to the live target and swap atomically.
    A plain copy first because the build dir (/tmp) and the data volume are
    different filesystems on Railway — os.replace cannot cross devices.
    Staging name is a fresh uuid per run (pids can repeat across container
    restarts), and the STAGED copy is integrity-checked again before the
    swap — the bytes that survived the cross-device copy are what goes
    live."""
    import shutil
    tmp_target = f'{target_path}.publish.{uuid.uuid4().hex[:12]}'
    try:
        shutil.copyfile(built_path, tmp_target)
        conn = sqlite3.connect(f'file:{tmp_target}?mode=ro', uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            meta = conn.execute(
                "SELECT value FROM book_meta WHERE key='built_at'").fetchone()
        finally:
            conn.close()
        if ok != 'ok' or not meta:
            raise RuntimeError(f'staged copy failed verification: {ok!r}')
        fd = os.open(tmp_target, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_target, target_path)
    finally:
        if os.path.exists(tmp_target):
            os.remove(tmp_target)
    dfd = os.open(os.path.dirname(target_path) or '.', os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def acquire_publish_lock(target_path):
    """Serialize the whole rebuild→publish lifecycle across workers and
    processes with a kernel flock on a PERSISTENT lockfile (same volume as
    the target). Returns the held fd; raises RuntimeError when another
    rebuild holds it.

    flock, not files-as-locks (Codex R6 blocker: every check-then-act
    variant — token release, liveness probe, age backstop — had a TOCTOU):
    acquisition IS the atomic check-and-own, the kernel releases it the
    instant the owner dies (crash included), and NOBODY ever unlinks the
    lockfile — so there is no stale-recovery logic left to race. The pid
    written inside is debugging breadcrumb only, never consulted."""
    lock_path = target_path + '.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError('another VAT rebuild is already running '
                           f'(lock: {lock_path})')
    os.ftruncate(fd, 0)
    os.write(fd, f'{os.getpid()} {datetime.now().isoformat()}'.encode())
    return fd


def release_publish_lock(fd):
    """Unlock + close. Never unlinks the lockfile — see acquire."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _report_result(result_db, result_row, vat_result):
    """Update the upload's persisted last-run record (import_log row created
    by the route with vat status 'building') in the MAIN db. Narrow contract:
    one UPDATE of one pre-existing row's notes JSON, nothing else."""
    conn = sqlite3.connect(result_db, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        row = conn.execute("SELECT notes FROM import_log WHERE id=?",
                           (result_row,)).fetchone()
        notes = json.loads(row[0]) if row and row[0] else {}
        notes['vat'] = vat_result
        conn.execute("UPDATE import_log SET notes=? WHERE id=?",
                     (json.dumps(notes, ensure_ascii=False), result_row))
        conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--source', required=True,
                   help='directory holding the xp5 DBF tables')
    p.add_argument('--publish-to',
                   help='live vat_book.db path to atomically replace on success')
    p.add_argument('--result-db',
                   help='MAIN db path holding the import_log last-run row to update')
    p.add_argument('--result-row', type=int,
                   help='import_log row id to update with the outcome')
    p.add_argument('--cleanup-dir',
                   help='scratch dir (dataset copy + build dir) to delete at exit')
    args = p.parse_args()
    lock_fd = None
    try:
        try:
            # The lock spans build AND publish (P0: two workers spawning two
            # rebuilds must serialize the entire lifecycle, not just the swap).
            if args.publish_to:
                lock_fd = acquire_publish_lock(args.publish_to)
            summary = build(args.source)
            if args.publish_to:
                publish(summary['db_path'], args.publish_to)
            outcome = {'ok': True, 'counts': summary['counts'],
                       'built_at': datetime.now().isoformat(timespec='seconds')}
            print(json.dumps(summary, ensure_ascii=False, indent=1))
        except BaseException as exc:
            outcome = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:400]}
            if args.result_db and args.result_row:
                _report_result(args.result_db, args.result_row, outcome)
            raise
        if args.result_db and args.result_row:
            _report_result(args.result_db, args.result_row, outcome)
    finally:
        if lock_fd is not None:
            release_publish_lock(lock_fd)
        if args.cleanup_dir:
            import shutil
            shutil.rmtree(args.cleanup_dir, ignore_errors=True)
