"""vat_book_builder — unit tests for the risky transforms (seed, stock
overwrite + oracle, isvat dump, finalize, subprocess guard).

Pure dict-fixture + throwaway-sqlite tests: no DBF files, no live DB. The
minimal 3-table schema below mirrors data/schema.sql's columns these
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
