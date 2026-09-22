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

Fill order: fresh schema → unit_map copied from the main db → products+mapping
seeded from STMAS (so every STKCOD resolves during import) →
commit_express_dbf(since_days=None) = full history
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

import actor
import bsn_units
import express_registers
import import_router
import report_types


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
        # #601: this is the VAT book — STMAS.QUCOD is an xp5 unit code, and
        # xp5's own ISTAB disagrees with BSN5657 on `หอ` (หลอด, not ห่อ).
        # Reading it against the default (BSN5657) book was the bug.
        unit = bsn_units.normalize_unit(
            str(r.get('QUCOD') or '').strip(), bsn_units.BOOK_XP5, conn=conn) or 'ตัว'
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
    VACUUM (a DELETE alone leaves the freed pages in the file), integrity-
    check, then fsync file and directory. Aborts on any failure."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode.lower() != 'delete':
            raise RuntimeError(f"journal_mode is {mode!r}, expected delete")
        conn.execute("VACUUM")
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


# The executable vocabulary owns which balance snapshots a from-scratch book
# cannot publish without (see _require_snapshots_ok).
_SNAPSHOT_LABELS = {
    register.key: register.label
    for register in express_registers.REGISTERS
    if register.snapshot_required
}


def _require_snapshots_ok(per_type):
    """Fail the build when either outstanding snapshot refused.

    import_router._commit_snapshot deliberately reports a snapshot failure instead
    of raising, because on the DAILY BSN import the ledger has already committed and
    the previous day's snapshot is still there to read. Neither is true here: this
    builds a whole book from scratch and then REPLACES the live one, so a refused
    snapshot would publish a VAT book with no balances at all — and `counts` would
    read `ar_snapshot: 0`, indistinguishable from a book that owes nothing.

    Raising here is what keeps publish() from running: main() wraps build() in
    `except BaseException`, records ok:False on the run row, and never reaches the
    publish call.
    """
    for key, label in _SNAPSHOT_LABELS.items():
        err = (per_type.get(key) or {}).get('error')
        if err:
            raise RuntimeError(
                f'{label}: สร้างไม่สำเร็จ — ไม่ publish สมุด VAT รอบนี้ ({err})')


def _use_main_unit_map(conn, main_db_path):
    """Make this build db's unit_map an exact copy of the MAIN db's (#596).

    The one unit map lives in the main app db. Everything here that
    translates a unit (seed_products_from_stmas, and import_weekly deep
    inside commit_express_dbf, which opens its own connection) reads it
    through `database.get_connection()`, and DATA_DIR points that at this
    fresh build db. init_db() filled that db's table from data/schema.sql,
    i.e. the map as of the last schema dump, which misses every code Put has
    named since. So it is replaced here, before anything reads it. Read-only
    on the main db."""
    if not main_db_path:
        raise RuntimeError(
            "vat_book_builder needs the main db (--result-db): the VAT book "
            "translates unit codes through the main db's unit_map")
    src = sqlite3.connect(f'file:{main_db_path}?mode=ro', uri=True)
    try:
        rows = src.execute("SELECT book, spelling, word FROM unit_map").fetchall()
    finally:
        src.close()
    if not rows:
        # An empty map reads as "every code unknown": the whole book would
        # import its unit codes untranslated, silently.
        raise RuntimeError(
            f"the main db's unit_map is empty ({main_db_path}); refusing to build "
            "a VAT book that would import every unit code untranslated")
    conn.execute("DELETE FROM unit_map")
    conn.executemany(
        "INSERT INTO unit_map (book, spelling, word) VALUES (?, ?, ?)", rows)
    conn.commit()


def build(source_dir, snapshot_date=None, main_db_path=None, uploader=None):
    """Full build, attributed to the person whose upload started it (#590).

    `uploader` comes from the spawning request (--uploader). The build rebuilds
    WACC through the real importers, so every cost row it writes carries
    system/vat-book-build and that person. With no uploader nothing is declared,
    and the importers refuse before their first write, as for any unsigned run.
    """
    if not uploader:
        return _build(source_dir, snapshot_date, main_db_path)
    with actor.acting_as(kind='system', who=uploader, source='import',
                         detail='vat-book-build'):
        return _build(source_dir, snapshot_date, main_db_path)


def _build(source_dir, snapshot_date=None, main_db_path=None):
    """Full build at config.DATABASE_PATH (guarded). Returns a summary dict.

    snapshot_date: the as-of date for this book's outstanding snapshots, decided
    ONCE by the upload request and passed down, so both books carry the same date.
    Letting it default here would stamp the VAT book from this subprocess's own
    clock — it starts minutes after the request and can cross midnight.

    main_db_path: the MAIN app db (the CLI's `--result-db`, captured by the
    route before this subprocess's DATA_DIR override took effect). Its
    unit_map is the one map; see _use_main_unit_map.
    """
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
        _use_main_unit_map(conn, main_db_path)
        seed_companies(conn)
        code_to_pid = seed_products_from_stmas(conn, stmas)
        per_type = import_router.commit_express_dbf(
            source_dir, db_path=db_path, since_days=None,
            snapshot_date=snapshot_date, book=bsn_units.BOOK_XP5)
        _require_snapshots_ok(per_type)
        overwrite_stock_from_stmas(conn, stmas, stloc, code_to_pid)
        isvat_n = dump_isvat(conn, isvat)
        stmas_meta_n = dump_stmas_meta(conn, stmas)
        counts = {
            'products': len(code_to_pid),
            'isvat_rows': isvat_n,
            'stmas_meta_rows': stmas_meta_n,
            # One row count per report type, each read through its own registry
            # record. That is what fixes the inconsistency this block carried:
            # credit_notes_ar reports 'upserted' where every sibling reports
            # 'imported', and it was hand-typed here with nothing pinning it.
            # Includes this book's OWN outstanding balances (xp5's RR26/IV
            # series) — kept here, never merged into the main book's figures.
            **report_types.book_meta_counts(per_type),
            'snapshot_date': per_type['snapshot_date'],
        }
        write_book_meta(conn, source_dir, isinfo, counts)
        # Every audit_log row here is this build's OWN insert — the book is
        # rebuilt from scratch on every upload, so the trail carries no real
        # history, and it was 44% of the published file (53MB of 120MB,
        # ENOSPC on prod's 74MB-free volume since 09-04). Keep the triggers
        # (the six importers still need them to run); drop what they wrote.
        # table-existence check mirrors the DROP-TABLE-IF-EXISTS idiom this
        # file already uses elsewhere (write_book_meta, dump_isvat) — a caller
        # that stubs out init_db() (unit tests) never created the table.
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ).fetchone():
            conn.execute("DELETE FROM audit_log")
            conn.commit()
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
                   help='MAIN db path: holds the import_log last-run row to update, '
                        'and the unit_map the build translates units through (required to build)')
    p.add_argument('--result-row', type=int,
                   help='import_log row id to update with the outcome')
    p.add_argument('--cleanup-dir',
                   help='scratch dir (dataset copy + build dir) to delete at exit')
    p.add_argument('--snapshot-date',
                   help='as-of date (ISO) for this book\'s outstanding snapshots, '
                        'decided by the upload request so both books agree')
    p.add_argument('--uploader',
                   help='who uploaded the dataset; every cost row the build '
                        'writes is attributed to them (#590)')
    args = p.parse_args()
    lock_fd = None
    try:
        try:
            # The lock spans build AND publish (P0: two workers spawning two
            # rebuilds must serialize the entire lifecycle, not just the swap).
            if args.publish_to:
                lock_fd = acquire_publish_lock(args.publish_to)
            summary = build(args.source, snapshot_date=args.snapshot_date,
                            main_db_path=args.result_db, uploader=args.uploader)
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
