"""vat_book_builder — unit tests for the risky transforms (seed, stock
overwrite + oracle, isvat dump, finalize, subprocess guard).

Pure dict-fixture + throwaway-sqlite tests: no DBF files, no live DB. The
minimal schema below mirrors data/schema.sql's columns these
functions touch; commit_express_dbf itself is covered by its own suite."""
import os
import sqlite3

import pytest

import vat_book_builder as vb


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'build.db')
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'ตัว',
            cost_price REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE product_code_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT NOT NULL,
            bsn_name TEXT NOT NULL,
            product_id INTEGER,
            is_ignored INTEGER NOT NULL DEFAULT 0,
            bsn_unit TEXT NOT NULL DEFAULT '',
            UNIQUE(bsn_code, bsn_unit));
        CREATE TABLE stock_levels (
            product_id INTEGER PRIMARY KEY,
            quantity INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE unit_map (
            id INTEGER PRIMARY KEY, book TEXT NOT NULL,
            spelling TEXT NOT NULL, word TEXT NOT NULL);
        -- #601: seed_products_from_stmas reads STMAS.QUCOD (an xp5 unit
        -- code) against book='xp5' — this is the VAT-book build's OWN
        -- unit_map (a copy of the main db's, see _use_main_unit_map), which
        -- holds both books' rows in production.
        INSERT INTO unit_map (book, spelling, word) VALUES ('xp5', 'ตว', 'ตัว');
    """)
    yield c
    c.close()


def _stmas(code, des='ของทดสอบ', qucod='ตว', totbal=0, unitpr=None):
    row = {'STKCOD': code, 'STKDES': des, 'QUCOD': qucod, 'TOTBAL': totbal}
    if unitpr is not None:
        row['UNITPR'] = unitpr
    return row


# ── seed_products_from_stmas ────────────────────────────────────────────────

def test_seed_creates_product_and_catchall_mapping(conn):
    pids = vb.seed_products_from_stmas(conn, [_stmas('001ก1', 'ค้อน 2 ปอนด์')])
    row = conn.execute(
        "SELECT p.product_name, p.unit_type, m.bsn_code, m.bsn_unit "
        "FROM products p JOIN product_code_mapping m ON m.product_id = p.id"
    ).fetchone()
    assert row['product_name'] == 'ค้อน 2 ปอนด์'
    assert row['unit_type'] == 'ตัว'          # 'ตว' acronym normalized
    assert (row['bsn_code'], row['bsn_unit']) == ('001ก1', '')
    assert pids['001ก1'] == 1


# ── #596: the build translates through the MAIN db's unit map ──────────────

def test_use_main_unit_map_refuses_without_a_main_db(conn):
    with pytest.raises(RuntimeError, match='--result-db'):
        vb._use_main_unit_map(conn, None)
    # control: nothing was touched before the refusal
    assert conn.execute("SELECT COUNT(*) FROM unit_map").fetchone()[0] == 1


def test_use_main_unit_map_refuses_an_empty_main_map(conn, tmp_path):
    """An empty main map would leave the build reading every code as
    unknown, so the whole book would import untranslated. Refuse instead,
    before the build db's own map is touched."""
    main_db = tmp_path / 'main.db'
    c = sqlite3.connect(str(main_db))
    c.execute("CREATE TABLE unit_map (book TEXT, spelling TEXT, word TEXT)")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match='unit_map is empty'):
        vb._use_main_unit_map(conn, str(main_db))
    assert conn.execute("SELECT COUNT(*) FROM unit_map").fetchone()[0] == 1


def test_build_imports_through_the_main_db_map(tmp_path, monkeypatch):
    """The VAT book is a fresh db: init_db() fills its unit_map from
    data/schema.sql, the map as of the last schema dump. The main db's map is
    the only live one (it holds every code Put has named since). A real build
    must translate through it on BOTH paths: the STMAS product seed and the
    importers commit_express_dbf runs, which open their own connection.

    Real init_db, seed, commit_express_dbf and import_weekly; only reading the
    DBF files is faked. `ขว` is in no dumped map; the main db names it.

    #601: the build now reads through book='xp5' (STMAS/STCRD carry xp5's own
    unit codes), so the main db's map must carry an xp5 row for `ขว`, not a
    BSN5657 one — the two books can disagree (`หอ` does), so a caller that
    reads the wrong book's row for a code is exactly the bug #601 fixes."""
    import sys
    scripts = os.path.join(os.path.dirname(__file__), '..', 'scripts')
    if scripts not in sys.path:
        sys.path.append(scripts)          # commit_express_dbf imports import_express
    import datetime
    import config
    import database
    import express_dbf_source as eds
    # The build imports these lazily. First imported under the patched
    # DATABASE_PATH, they would keep the deleted tmp path for every later
    # test (`from config import DATABASE_PATH` binds once); import them now.
    import cashflow  # noqa: F401
    import import_credit_notes  # noqa: F401
    import payments_alloc  # noqa: F401
    before = set(sys.modules)

    main_db = tmp_path / 'main.db'
    c = sqlite3.connect(str(main_db))
    c.executescript("""
        CREATE TABLE unit_map (
            id INTEGER PRIMARY KEY, book TEXT NOT NULL,
            spelling TEXT NOT NULL, word TEXT NOT NULL);
        INSERT INTO unit_map (book, spelling, word) VALUES
            ('BSN5657', 'ขว', 'ขวด'), ('xp5', 'ขว', 'ขวด');
    """)
    c.commit()
    c.close()

    db_path = str(tmp_path / 'build' / 'inventory.db')
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    monkeypatch.setattr(database, 'DATABASE_PATH', db_path)
    monkeypatch.setenv('VAT_BOOK_BUILD', '1')
    d = datetime.date(2026, 4, 1)
    tables = {
        'STMAS': [_stmas('X1', 'น้ำยาทดสอบ', qucod='ขว', unitpr=10.0)],
        'STLOC': [], 'ISVAT': [], 'ISINFO': [],
        'ARTRN': [], 'ARMAS': [], 'APMAS': [], 'ARTRNRM': [],
        'ARRCPIT': [], 'APRCPIT': [],
        'APTRN': [{'DOCNUM': 'RR2600001', 'RECTYP': '3', 'SUPCOD': 'S001',
                   'FLGVAT': 0, 'DOCDAT': d}],
        'STCRD': [{'DOCNUM': 'RR2600001', 'SEQNUM': 1, 'STKCOD': 'X1',
                   'STKDES': 'น้ำยาทดสอบ', 'TRNQTY': 6.0, 'TQUCOD': 'ขว',
                   'UNITPR': 10.0, 'DISC': '', 'TRNVAL': 60.0, 'NETVAL': 60.0,
                   'RDOCNUM': ''}],
    }

    def fake_open(dataset_dir, name):
        if name not in tables:
            raise FileNotFoundError(name)    # an optional table this zip lacks
        return tables[name]
    monkeypatch.setattr(eds, 'open_table', fake_open)

    vb.build(str(tmp_path / 'dbf'), snapshot_date='2026-09-19',
             main_db_path=str(main_db))

    built = sqlite3.connect(db_path)
    try:
        product_unit = built.execute(
            "SELECT unit_type FROM products WHERE product_name = 'น้ำยาทดสอบ'").fetchall()
        line_unit = built.execute(
            "SELECT unit FROM purchase_transactions WHERE doc_no = 'RR2600001'").fetchall()
        rows = built.execute("SELECT book, spelling, word FROM unit_map").fetchall()
    finally:
        built.close()
    assert line_unit == [('ขวด',)]           # the importer path
    assert product_unit == [('ขวด',)]        # the STMAS seed path
    # exactly the main map: schema.sql's dumped rows were replaced, not topped up
    assert sorted(rows) == [('BSN5657', 'ขว', 'ขวด'), ('xp5', 'ขว', 'ขวด')]
    leaked = sorted(m for m in set(sys.modules) - before
                    if getattr(sys.modules[m], 'DATABASE_PATH', None) == db_path)
    assert leaked == [], f'first imported inside the build, bound to the tmp db: {leaked}'


def test_build_reads_hoo_as_xp5s_own_meaning_not_bsn5657s(tmp_path, monkeypatch):
    """#601's actual acceptance criterion: `หอ` is the ONE code the two
    books disagree on (ห่อ under BSN5657, หลอด under xp5 — a sealant/glue
    tube). The main db's map carries BOTH rows (real production shape); a
    build that read the wrong one would store every VAT-book tube as ห่อ."""
    import sys
    scripts = os.path.join(os.path.dirname(__file__), '..', 'scripts')
    if scripts not in sys.path:
        sys.path.append(scripts)
    import datetime
    import config
    import database
    import express_dbf_source as eds
    import cashflow  # noqa: F401
    import import_credit_notes  # noqa: F401
    import payments_alloc  # noqa: F401

    main_db = tmp_path / 'main.db'
    c = sqlite3.connect(str(main_db))
    c.executescript("""
        CREATE TABLE unit_map (
            id INTEGER PRIMARY KEY, book TEXT NOT NULL,
            spelling TEXT NOT NULL, word TEXT NOT NULL);
        INSERT INTO unit_map (book, spelling, word) VALUES
            ('BSN5657', 'หอ', 'ห่อ'), ('xp5', 'หอ', 'หลอด');
    """)
    c.commit()
    c.close()

    db_path = str(tmp_path / 'build' / 'inventory.db')
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    monkeypatch.setattr(database, 'DATABASE_PATH', db_path)
    monkeypatch.setenv('VAT_BOOK_BUILD', '1')
    d = datetime.date(2026, 4, 1)
    tables = {
        'STMAS': [_stmas('T1', 'กาวซิลิโคน', qucod='หอ', unitpr=25.0)],
        'STLOC': [], 'ISVAT': [], 'ISINFO': [],
        'ARTRN': [], 'ARMAS': [], 'APMAS': [], 'ARTRNRM': [],
        'ARRCPIT': [], 'APRCPIT': [],
        'APTRN': [{'DOCNUM': 'RR2600002', 'RECTYP': '3', 'SUPCOD': 'S001',
                   'FLGVAT': 0, 'DOCDAT': d}],
        'STCRD': [{'DOCNUM': 'RR2600002', 'SEQNUM': 1, 'STKCOD': 'T1',
                   'STKDES': 'กาวซิลิโคน', 'TRNQTY': 12.0, 'TQUCOD': 'หอ',
                   'UNITPR': 25.0, 'DISC': '', 'TRNVAL': 300.0, 'NETVAL': 300.0,
                   'RDOCNUM': ''}],
    }

    def fake_open(dataset_dir, name):
        if name not in tables:
            raise FileNotFoundError(name)
        return tables[name]
    monkeypatch.setattr(eds, 'open_table', fake_open)

    vb.build(str(tmp_path / 'dbf'), snapshot_date='2026-09-19',
             main_db_path=str(main_db))

    built = sqlite3.connect(db_path)
    try:
        product_unit = built.execute(
            "SELECT unit_type FROM products WHERE product_name = 'กาวซิลิโคน'").fetchall()
        line_unit = built.execute(
            "SELECT unit FROM purchase_transactions WHERE doc_no = 'RR2600002'").fetchall()
    finally:
        built.close()
    assert product_unit == [('หลอด',)], 'STMAS seed read the wrong book'
    assert line_unit == [('หลอด',)], 'the importer read the wrong book'


def test_seed_blank_name_falls_back_to_code_and_dups_keep_first(conn):
    vb.seed_products_from_stmas(conn, [
        _stmas('X1', des='  '), _stmas('X1', des='ตัวซ้ำ'), _stmas('', des='ไร้รหัส')])
    rows = conn.execute("SELECT product_name FROM products").fetchall()
    assert [r['product_name'] for r in rows] == ['X1']   # blank→code, dup+blank skipped


# ── seed_products_from_stmas: cost_price := STMAS.UNITPR (plan §4.2) ───────

def test_seed_cost_price_from_unitpr(conn):
    """The 001ก1040 example from the plan (§2): builder's old WACC-from-
    purchases gave cost 0 while STMAS carries UNITPR ฿7.59 — the fix seeds
    cost_price straight from Express's own maintained average cost."""
    vb.seed_products_from_stmas(conn, [_stmas('001ก1040', 'กลอนเหล็ก#511-4นิ้ว AC', unitpr=7.59)])
    cost = conn.execute("SELECT cost_price FROM products").fetchone()[0]
    assert cost == 7.59


def test_seed_cost_price_nonpositive_or_blank_unitpr_stays_zero(conn):
    vb.seed_products_from_stmas(conn, [
        _stmas('Z1', des='สินค้า Z1', unitpr=0),
        _stmas('Z2', des='สินค้า Z2', unitpr=-3.5),
        _stmas('Z3', des='สินค้า Z3'),                    # no UNITPR key at all
        _stmas('Z4', des='สินค้า Z4', unitpr=''),         # blank string (as DBF can yield)
    ])
    costs = {r['product_name']: r['cost_price'] for r in
              conn.execute("SELECT product_name, cost_price FROM products")}
    assert costs == {'สินค้า Z1': 0.0, 'สินค้า Z2': 0.0, 'สินค้า Z3': 0.0, 'สินค้า Z4': 0.0}


# ── overwrite_stock_from_stmas ──────────────────────────────────────────────

def test_stock_overwrite_uses_totbal_and_keeps_fractions(conn):
    pids = vb.seed_products_from_stmas(conn, [_stmas('A', totbal=-4.5)])
    conn.execute("INSERT INTO stock_levels VALUES (?, 999)", (pids['A'],))
    conn.commit()
    vb.overwrite_stock_from_stmas(
        conn, [_stmas('A', totbal=-4.5)],
        [{'STKCOD': 'A', 'LOCBAL': -4.5}], pids)
    q = conn.execute("SELECT quantity FROM stock_levels").fetchone()[0]
    assert q == -4.5                     # tax-book negative + fraction preserved


def test_stock_oracle_mismatch_aborts(conn):
    pids = vb.seed_products_from_stmas(conn, [_stmas('A', totbal=10)])
    with pytest.raises(ValueError, match='STLOC'):
        vb.overwrite_stock_from_stmas(
            conn, [_stmas('A', totbal=10)],
            [{'STKCOD': 'A', 'LOCBAL': 7}], pids)
    # rollback: the pre-existing rows were not replaced by a partial write
    assert conn.execute("SELECT COUNT(*) FROM stock_levels").fetchone()[0] == 0


def test_stock_oracle_sums_multi_location(conn):
    pids = vb.seed_products_from_stmas(conn, [_stmas('A', totbal=12)])
    vb.overwrite_stock_from_stmas(
        conn, [_stmas('A', totbal=12)],
        [{'STKCOD': 'A', 'LOCBAL': 5}, {'STKCOD': 'A', 'LOCBAL': 7}], pids)
    assert conn.execute("SELECT quantity FROM stock_levels").fetchone()[0] == 12


# ── dump_stmas_meta (vat-substitute: STKGRP + VATCOD per code) ─────────────
# Book-only artifact (same footing as isvat_raw — not in data/schema.sql):
# the candidate/guess filters (plan §2/§5, decision 8) need Express's own
# category (STKGRP) and tax-type (VATCOD) per code, which seed_products_from_
# stmas' Sendy-shape products table has no column for.

def test_dump_stmas_meta_creates_lookup_table(conn):
    n = vb.dump_stmas_meta(conn, [
        {'STKCOD': 'A1', 'STKGRP': '57', 'VATCOD': '1'},
        {'STKCOD': 'A2', 'STKGRP': '93', 'VATCOD': '0'},
    ])
    assert n == 2
    rows = {r['stkcod']: (r['stkgrp'], r['vatcod']) for r in
            conn.execute("SELECT stkcod, stkgrp, vatcod FROM stmas_meta")}
    assert rows == {'A1': ('57', '1'), 'A2': ('93', '0')}


def test_dump_stmas_meta_blank_fields_and_dup_codes(conn):
    n = vb.dump_stmas_meta(conn, [
        {'STKCOD': 'B1'},                                   # no STKGRP/VATCOD at all
        {'STKCOD': 'B1', 'STKGRP': 'ignored-dup'},          # dup code keeps first
        {'STKCOD': '', 'STKGRP': '99'},                     # blank code skipped
    ])
    assert n == 1
    row = conn.execute("SELECT stkgrp, vatcod FROM stmas_meta WHERE stkcod='B1'").fetchone()
    assert (row['stkgrp'], row['vatcod']) == ('', '')


# ── dump_isvat / book_meta ──────────────────────────────────────────────────

def test_isvat_dump_sanitizes_columns_and_iso_dates(conn):
    import datetime
    n = vb.dump_isvat(conn, [
        {'DOCNUM': 'IV26001', 'VAT-AMT': 7.0, 'DOCDAT': datetime.date(2026, 3, 1)}])
    assert n == 1
    row = conn.execute('SELECT "DOCNUM", "VAT_AMT", "DOCDAT" FROM isvat_raw').fetchone()
    assert tuple(row) == ('IV26001', 7.0, '2026-03-01')


def test_book_meta_holds_identity_and_counts(conn):
    vb.write_book_meta(conn, '/x/xp5', [{'THINAM': 'บจก.ทดสอบ', 'TAXID': '0105'}],
                       {'products': 3})
    meta = dict(conn.execute("SELECT key, value FROM book_meta"))
    assert meta['company_name'] == 'บจก.ทดสอบ'
    assert meta['source_dir'] == 'xp5'
    assert '"products": 3' in meta['counts']


# ── finalize ────────────────────────────────────────────────────────────────

def test_finalize_produces_single_self_contained_file(tmp_path):
    db = str(tmp_path / 'vat_book.db')
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    vb.finalize(db)
    assert not os.path.exists(db + '-wal') and not os.path.exists(db + '-shm')
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == 'delete'
    assert c.execute("SELECT x FROM t").fetchone()[0] == 1
    c.close()


def test_finalize_rejects_corrupt_file(tmp_path):
    db = tmp_path / 'bad.db'
    db.write_bytes(b'SQLite format 3\x00' + b'\x00' * 100)  # truncated header
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        vb.finalize(str(db))


# ── publish + lock (Codex R4 P0) ────────────────────────────────────────────

def _valid_book(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO book_meta VALUES ('built_at', 'T')")
    c.commit()
    c.close()


def test_publish_swaps_and_leaves_no_staging(tmp_path):
    built = str(tmp_path / 'built.db')
    target = str(tmp_path / 'vat_book.db')
    _valid_book(built)
    vb.publish(built, target)
    assert os.path.exists(target)
    leftovers = [f for f in os.listdir(tmp_path) if '.publish' in f]
    assert leftovers == []


def test_publish_rejects_corrupt_copy_and_keeps_target(tmp_path):
    built = tmp_path / 'built.db'
    built.write_bytes(b'SQLite format 3\x00' + b'\x00' * 64)   # truncated junk
    target = str(tmp_path / 'vat_book.db')
    _valid_book(target)                     # existing live book must survive
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        vb.publish(str(built), target)
    c = sqlite3.connect(f'file:{target}?mode=ro', uri=True)
    assert c.execute("SELECT value FROM book_meta").fetchone()[0] == 'T'
    c.close()


def test_publish_lock_excludes_second_holder_and_reacquires(tmp_path):
    target = str(tmp_path / 'vat_book.db')
    fd = vb.acquire_publish_lock(target)
    with pytest.raises(RuntimeError, match='already running'):
        vb.acquire_publish_lock(target)        # separate fd, same-process: flock conflicts
    vb.release_publish_lock(fd)
    fd2 = vb.acquire_publish_lock(target)      # released → reacquirable
    vb.release_publish_lock(fd2)
    assert os.path.exists(target + '.lock')    # lockfile persists, NEVER unlinked


def test_lock_released_by_kernel_when_owner_dies(tmp_path):
    """The R6-blocker property flock buys us: a crashed builder frees the
    lock with no stale-recovery logic (and thus nothing left to race)."""
    import subprocess
    import sys as _sys
    target = str(tmp_path / 'vat_book.db')
    lock_path = target + '.lock'
    child_code = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n")
    p = subprocess.Popen([_sys.executable, '-c', child_code, lock_path],
                         stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == b'locked'
        with pytest.raises(RuntimeError, match='already running'):
            vb.acquire_publish_lock(target)    # child holds it
    finally:
        p.kill()
        p.wait()
    fd = vb.acquire_publish_lock(target)       # kernel auto-released on death
    vb.release_publish_lock(fd)


# ── subprocess guard ────────────────────────────────────────────────────────

def test_guard_refuses_without_env_flag(monkeypatch):
    monkeypatch.delenv('VAT_BOOK_BUILD', raising=False)
    with pytest.raises(SystemExit, match='VAT_BOOK_BUILD'):
        vb._guard_subprocess_target()


def test_guard_refuses_existing_target(monkeypatch):
    import config
    monkeypatch.setenv('VAT_BOOK_BUILD', '1')
    # config.DATABASE_PATH exists on a dev machine (the live/dev DB) — that is
    # exactly the accident the guard exists for. If it doesn't exist in this
    # env (CI), create a stand-in via monkeypatch.
    if not os.path.exists(config.DATABASE_PATH):
        pytest.skip('no DB at config.DATABASE_PATH in this env')
    with pytest.raises(SystemExit, match='already exists'):
        vb._guard_subprocess_target()


# ── snapshot outcomes gate the publish (Codex P1, 2026-08-17) ────────────────

def test_require_snapshots_ok_raises_when_a_snapshot_failed():
    """A fresh VAT build has no previous snapshot to fall back on, so a snapshot
    that refused must fail the BUILD — otherwise main() goes straight on to
    publish() and a book with no ลูกหนี้/เจ้าหนี้คงค้าง at all replaces the live
    one while reporting ok:True.

    Deliberately NOT the same policy as the daily BSN import, where the ledger has
    already committed and yesterday's snapshot is still readable — there,
    _commit_snapshot's report-and-continue is correct."""
    per_type = {'ar_snapshot': {'imported': 0, 'error': 'boom'},
                'ap_snapshot': {'imported': 3}}

    with pytest.raises(RuntimeError, match='ลูกหนี้คงค้าง'):
        vb._require_snapshots_ok(per_type)


def test_require_snapshots_ok_raises_for_the_ap_side_too():
    per_type = {'ar_snapshot': {'imported': 5},
                'ap_snapshot': {'imported': 0, 'error': 'boom'}}

    with pytest.raises(RuntimeError, match='เจ้าหนี้คงค้าง'):
        vb._require_snapshots_ok(per_type)


def test_require_snapshots_ok_accepts_a_book_with_no_open_documents():
    """CONTROL, and the reason the gate keys on `error` rather than on a zero count:
    a book that genuinely owes and is owed nothing is valid, not a failure."""
    per_type = {'ar_snapshot': {'imported': 0}, 'ap_snapshot': {'imported': 0}}

    vb._require_snapshots_ok(per_type)   # must not raise


def test_builder_cli_accepts_a_snapshot_date():
    """The date is decided once at upload time and handed to the detached builder;
    without this the subprocess would stamp the VAT book from its own clock, minutes
    later and possibly on the next day (Codex P2)."""
    import subprocess
    import sys
    out = subprocess.run([sys.executable, vb.__file__, '--help'],
                         capture_output=True, text=True)
    assert '--snapshot-date' in out.stdout


def test_build_refuses_to_return_when_a_snapshot_failed(tmp_path, monkeypatch):
    """The gate above is only worth anything if build() actually calls it — a test
    that invokes the helper directly stays green with the call deleted, which is
    testing the neighbour and not the subject.

    Drives build() with its dependencies stubbed out so the only thing that can
    stop it is the snapshot check. main() wraps build() in `except BaseException`
    and never reaches publish(), so "build raises" IS "the book is not published".
    """
    import database
    import express_dbf_source as eds
    import import_router

    db_path = str(tmp_path / 'built.db')
    monkeypatch.setattr(vb, '_guard_subprocess_target', lambda: db_path)
    monkeypatch.setattr(database, 'init_db', lambda *a, **k: None)
    monkeypatch.setattr(database, 'get_connection', lambda *a, **k: sqlite3.connect(db_path))
    monkeypatch.setattr(eds, 'open_table', lambda *a, **k: [])
    monkeypatch.setattr(vb, 'seed_companies', lambda conn: None)
    monkeypatch.setattr(vb, '_use_main_unit_map', lambda *a, **k: None)
    monkeypatch.setattr(vb, 'seed_products_from_stmas', lambda conn, rows: {})
    reached = []
    monkeypatch.setattr(vb, 'overwrite_stock_from_stmas',
                        lambda *a, **k: reached.append('stock'))
    monkeypatch.setattr(import_router, 'commit_express_dbf',
                        lambda *a, **k: {'ar_snapshot': {'imported': 0, 'error': 'boom'},
                                         'ap_snapshot': {'imported': 0}})

    with pytest.raises(RuntimeError, match='ลูกหนี้คงค้าง'):
        vb.build('/nonexistent')

    assert reached == [], 'build must stop AT the gate, before finishing the book'


def test_build_passes_the_snapshot_date_through_to_the_importer(tmp_path, monkeypatch):
    """Closes the last link in the chain. The route decides one date, the CLI
    accepts it and build() takes it — but none of that matters unless build()
    hands it to commit_express_dbf, which is what actually stamps the rows.
    Without this the argument could be accepted and silently dropped."""
    import database
    import express_dbf_source as eds
    import import_router

    db_path = str(tmp_path / 'built2.db')
    monkeypatch.setattr(vb, '_guard_subprocess_target', lambda: db_path)
    monkeypatch.setattr(database, 'init_db', lambda *a, **k: None)
    monkeypatch.setattr(database, 'get_connection', lambda *a, **k: sqlite3.connect(db_path))
    monkeypatch.setattr(eds, 'open_table', lambda *a, **k: [])
    monkeypatch.setattr(vb, 'seed_companies', lambda conn: None)
    monkeypatch.setattr(vb, '_use_main_unit_map', lambda *a, **k: None)
    monkeypatch.setattr(vb, 'seed_products_from_stmas', lambda conn, rows: {})
    monkeypatch.setattr(vb, 'overwrite_stock_from_stmas', lambda *a, **k: None)
    monkeypatch.setattr(vb, 'dump_isvat', lambda *a, **k: 0)
    monkeypatch.setattr(vb, 'dump_stmas_meta', lambda *a, **k: 0)
    monkeypatch.setattr(vb, 'write_book_meta', lambda *a, **k: None)
    monkeypatch.setattr(vb, 'clear_build_audit', lambda conn: None)
    monkeypatch.setattr(vb, 'finalize', lambda *a, **k: None)
    seen = {}

    def _cap(*a, **k):
        seen['date'] = k.get('snapshot_date')
        return {'sales': {'imported': 0}, 'purchase': {'imported': 0},
                'payments_in': {'imported': 0}, 'payments_out': {'imported': 0},
                'credit_notes_ar': {'upserted': 0}, 'credit_notes_ap': {'imported': 0},
                'ar_snapshot': {'imported': 0}, 'ap_snapshot': {'imported': 0},
                'snapshot_date': k.get('snapshot_date')}
    monkeypatch.setattr(import_router, 'commit_express_dbf', _cap)

    vb.build('/nonexistent', snapshot_date='2026-08-15')

    assert seen['date'] == '2026-08-15'
