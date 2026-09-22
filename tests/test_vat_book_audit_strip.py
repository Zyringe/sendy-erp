"""vat_book_builder — the build empties its own audit_log, and finalize
returns the resulting dead pages to the OS (a DELETE alone leaves them in
the file). See vat_book_builder.py::_build and ::finalize for the why.

(a) is a throwaway-sqlite unit test on finalize alone. (b) reuses the real
build setup from test_build_imports_through_the_main_db_map in
tests/test_vat_book_builder.py (fake DBF source, tiny STMAS/APTRN/STCRD, a
main db unit_map, real init_db/commit_express_dbf/import_weekly)."""
import os
import sqlite3

import vat_book_builder as vb


# ── finalize: VACUUM returns dead pages, and the file actually shrinks ─────

def test_finalize_vacuums_dead_pages_and_shrinks_the_file(tmp_path):
    db = str(tmp_path / 'vat_book.db')
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x BLOB)")
    # Enough rows, spanning many pages, then delete them all — that is what
    # leaves the file holding freelist pages for VACUUM to reclaim.
    c.executemany("INSERT INTO t VALUES (?)",
                  [(os.urandom(2000),) for _ in range(2000)])
    c.commit()
    c.execute("DELETE FROM t")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()

    # CONTROL: the fixture actually produced dead pages before finalize runs.
    check = sqlite3.connect(db)
    freelist_before = check.execute("PRAGMA freelist_count").fetchone()[0]
    check.close()
    assert freelist_before > 0
    size_before = os.path.getsize(db)

    vb.finalize(db)

    after = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        freelist_after = after.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        after.close()
    assert freelist_after == 0
    assert os.path.getsize(db) < size_before


# ── build: no audit rows survive in the published artifact ─────────────────

def _build_a_tiny_book(tmp_path, monkeypatch):
    """Same real build as test_build_imports_through_the_main_db_map; returns
    the built db path."""
    import sys
    scripts = os.path.join(os.path.dirname(__file__), '..', 'scripts')
    if scripts not in sys.path:
        sys.path.append(scripts)          # commit_express_dbf imports import_express
    import datetime
    import config
    import database
    import express_dbf_source as eds
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
        INSERT INTO unit_map (book, spelling, word) VALUES ('xp5', 'ตว', 'ตัว');
    """)
    c.commit()
    c.close()

    db_path = str(tmp_path / 'build' / 'inventory.db')
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    monkeypatch.setattr(database, 'DATABASE_PATH', db_path)
    monkeypatch.setenv('VAT_BOOK_BUILD', '1')
    d = datetime.date(2026, 4, 1)
    tables = {
        'STMAS': [{'STKCOD': 'X1', 'STKDES': 'น้ำยาทดสอบ', 'QUCOD': 'ตว', 'TOTBAL': 0}],
        'STLOC': [], 'ISVAT': [], 'ISINFO': [],
        'ARTRN': [], 'ARMAS': [], 'APMAS': [], 'ARTRNRM': [],
        'ARRCPIT': [], 'APRCPIT': [],
        'APTRN': [{'DOCNUM': 'RR2600001', 'RECTYP': '3', 'SUPCOD': 'S001',
                   'FLGVAT': 0, 'DOCDAT': d}],
        'STCRD': [{'DOCNUM': 'RR2600001', 'SEQNUM': 1, 'STKCOD': 'X1',
                   'STKDES': 'น้ำยาทดสอบ', 'TRNQTY': 6.0, 'TQUCOD': 'ตว',
                   'UNITPR': 10.0, 'DISC': '', 'TRNVAL': 60.0, 'NETVAL': 60.0,
                   'RDOCNUM': ''}],
    }

    def fake_open(dataset_dir, name):
        if name not in tables:
            raise FileNotFoundError(name)
        return tables[name]
    monkeypatch.setattr(eds, 'open_table', fake_open)

    vb.build(str(tmp_path / 'dbf'), snapshot_date='2026-09-19',
             main_db_path=str(main_db))

    leaked = sorted(m for m in set(sys.modules) - before
                    if getattr(sys.modules[m], 'DATABASE_PATH', None) == db_path)
    assert leaked == [], f'first imported inside the build, bound to the tmp db: {leaked}'
    return db_path


def test_build_leaves_no_audit_rows(tmp_path, monkeypatch):
    db_path = _build_a_tiny_book(tmp_path, monkeypatch)

    built = sqlite3.connect(db_path)
    try:
        # CONTROL 1: the row the importer wrote is actually in the book —
        # the assertion below is not just "everything is empty".
        purchase_rows = built.execute(
            "SELECT doc_no FROM purchase_transactions WHERE doc_no = 'RR2600001'"
        ).fetchall()
        # CONTROL 2: the audit trigger exists and would have fired — the
        # build genuinely generated audit rows, they were then removed.
        trigger = built.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'audit_purchase_transactions_insert'"
        ).fetchall()
        audit_count = built.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        built.close()

    assert purchase_rows == [('RR2600001',)]
    assert trigger == [('audit_purchase_transactions_insert',)]
    assert audit_count == 0
