"""Tests for scripts/diff_sku_map.py (product-naming-round2 Phase 3 fix ซ,
item 1). Emits the old->new sku_code map that rename_sku_folders.py --map
consumes. All tests use real sqlite files under pytest tmp_path — this
script's whole point is a real WAL/backup filesystem interaction, so an
in-memory DB would miss the bug it exists to fix.
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import diff_sku_map as dsm  # noqa: E402


def _mk_db(path, rows, journal_mode=None):
    """rows: list of (id, sku_code) tuples. sku_code=None -> NULL."""
    conn = sqlite3.connect(str(path))
    if journal_mode:
        conn.execute(f"PRAGMA journal_mode={journal_mode};")
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, sku_code TEXT, product_name TEXT)")
    conn.executemany("INSERT INTO products (id, sku_code, product_name) VALUES (?,?,?)",
                      [(i, sku, f"product {i}") for i, sku in rows])
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# diff_sku_map — the core diff logic
# ---------------------------------------------------------------------------

def test_diff_detects_a_normal_sku_change(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "OLD-01")])
    _mk_db(live, [(1, "NEW-01")])
    rows = dsm.diff_sku_map(backup, live)
    assert rows == [{"product_id": "1", "old_sku": "OLD-01", "new_sku": "NEW-01"}]


def test_diff_no_change_is_empty(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "SAME-01"), (2, "SAME-02")])
    _mk_db(live, [(1, "SAME-01"), (2, "SAME-02")])
    assert dsm.diff_sku_map(backup, live) == []


def test_diff_null_to_value(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, None)])
    _mk_db(live, [(1, "NEW-01")])
    rows = dsm.diff_sku_map(backup, live)
    assert rows == [{"product_id": "1", "old_sku": "", "new_sku": "NEW-01"}]


def test_diff_value_to_null(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "OLD-01")])
    _mk_db(live, [(1, None)])
    rows = dsm.diff_sku_map(backup, live)
    assert rows == [{"product_id": "1", "old_sku": "OLD-01", "new_sku": ""}]


def test_diff_null_to_null_is_no_change(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, None)])
    _mk_db(live, [(1, None)])
    assert dsm.diff_sku_map(backup, live) == []


def test_diff_ignores_products_only_present_in_one_db(tmp_path):
    """A product added or deleted between the snapshot and now is not a
    rename — must be excluded, not surfaced as a spurious NULL<->value row."""
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "OLD-01"), (2, "DELETED-BEFORE-LIVE")])  # pid 2 gone by 'now'
    _mk_db(live, [(1, "NEW-01"), (3, "ADDED-AFTER-BACKUP")])     # pid 3 didn't exist at backup time
    rows = dsm.diff_sku_map(backup, live)
    assert rows == [{"product_id": "1", "old_sku": "OLD-01", "new_sku": "NEW-01"}]


def test_diff_sorted_by_product_id(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(20, "A"), (3, "B"), (100, "C")])
    _mk_db(live, [(20, "A2"), (3, "B2"), (100, "C2")])
    rows = dsm.diff_sku_map(backup, live)
    assert [r["product_id"] for r in rows] == ["3", "20", "100"]


def test_diff_read_only_does_not_modify_either_db(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "OLD-01")])
    _mk_db(live, [(1, "NEW-01")])
    before_backup_mtime = backup.stat().st_mtime
    before_live_mtime = live.stat().st_mtime
    dsm.diff_sku_map(backup, live)
    assert backup.stat().st_mtime == before_backup_mtime
    assert live.stat().st_mtime == before_live_mtime


# ---------------------------------------------------------------------------
# normalize_backup_journal_mode — the real WAL/.backup gotcha
# ---------------------------------------------------------------------------

def test_wal_backup_fails_mode_ro_without_normalization(tmp_path):
    """Ground-truth regression pin for the gotcha itself: a FRESH backup
    (via the real sqlite3.Connection.backup() online-backup API — the same
    mechanism the `.backup` CLI dot-command uses) of a WAL-mode source, with
    no -wal/-shm companions yet, must reproduce the actual 'unable to open
    database file' failure under mode=ro. If this test ever stops failing on
    its own (e.g. a future sqlite3 version changes this behavior), that's a
    signal normalize_backup_journal_mode() may no longer be necessary —
    don't just delete this test to make it pass."""
    src_path = tmp_path / "src.db"
    backup_path = tmp_path / "fresh_backup.db"
    src = sqlite3.connect(str(src_path))
    src.execute("PRAGMA journal_mode=WAL;")
    src.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, sku_code TEXT)")
    src.execute("INSERT INTO products VALUES (1, 'OLD-01')")
    src.commit()
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()

    assert not (tmp_path / "fresh_backup.db-wal").exists()  # ground truth: no companions yet
    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        with __import__("pytest").raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM products").fetchall()
    finally:
        conn.close()


def test_normalize_backup_journal_mode_makes_wal_backup_readable(tmp_path):
    """The actual fix, exercised end to end against a real WAL-backup file
    reproducing the same gotcha as the test above."""
    src_path = tmp_path / "src.db"
    backup_path = tmp_path / "fresh_backup.db"
    src = sqlite3.connect(str(src_path))
    src.execute("PRAGMA journal_mode=WAL;")
    src.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, sku_code TEXT)")
    src.execute("INSERT INTO products VALUES (1, 'OLD-01')")
    src.commit()
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()

    dsm.normalize_backup_journal_mode(backup_path)

    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT * FROM products").fetchall() == [(1, "OLD-01")]
    finally:
        conn.close()


def test_normalize_backup_journal_mode_is_idempotent_on_a_non_wal_file(tmp_path):
    """A backup that's already off WAL mode (e.g. re-run, or the source
    itself wasn't WAL) must be a harmless no-op, not an error."""
    backup_path = tmp_path / "already_delete_mode.db"
    _mk_db(backup_path, [(1, "OLD-01")], journal_mode="DELETE")
    dsm.normalize_backup_journal_mode(backup_path)  # should not raise
    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT * FROM products").fetchall() == [(1, "OLD-01", "product 1")]
    finally:
        conn.close()


def test_normalize_backup_journal_mode_does_not_touch_table_data(tmp_path):
    """The fix is header-only — row content must be byte-identical after."""
    src_path = tmp_path / "src.db"
    backup_path = tmp_path / "fresh_backup.db"
    src = sqlite3.connect(str(src_path))
    src.execute("PRAGMA journal_mode=WAL;")
    src.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, sku_code TEXT, product_name TEXT)")
    src.execute("INSERT INTO products VALUES (1, 'OLD-01', 'ตัวอย่างสินค้า')")
    src.execute("INSERT INTO products VALUES (2, NULL, 'ไม่มี sku')")
    src.commit()
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()

    dsm.normalize_backup_journal_mode(backup_path)

    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [(1, "OLD-01", "ตัวอย่างสินค้า"), (2, None, "ไม่มี sku")]


# ---------------------------------------------------------------------------
# main() — CLI end to end
# ---------------------------------------------------------------------------

def test_main_writes_csv_with_exact_contract_columns(tmp_path):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "OLD-01"), (2, "SAME")])
    _mk_db(live, [(1, "NEW-01"), (2, "SAME")])
    out = tmp_path / "sku_map.csv"

    rc = dsm.main(["--backup", str(backup), "--live", str(live), "--out", str(out)])
    assert rc == 0

    import csv
    with open(out, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["product_id", "old_sku", "new_sku"]
        rows = list(reader)
    assert rows == [{"product_id": "1", "old_sku": "OLD-01", "new_sku": "NEW-01"}]


def test_main_no_changes_writes_header_only_csv_with_notice(tmp_path, capsys):
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _mk_db(backup, [(1, "SAME-01")])
    _mk_db(live, [(1, "SAME-01")])
    out = tmp_path / "sku_map.csv"

    rc = dsm.main(["--backup", str(backup), "--live", str(live), "--out", str(out)])
    assert rc == 0
    assert out.exists()

    import csv
    with open(out, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows == []   # header-only, still a valid CSV rename_sku_folders.py can consume

    captured = capsys.readouterr()
    assert "no" in captured.out.lower() and "chang" in captured.out.lower()


def test_main_refuses_when_backup_and_live_are_the_same_path(tmp_path, capsys):
    same = tmp_path / "same.db"
    _mk_db(same, [(1, "OLD-01")])
    out = tmp_path / "sku_map.csv"

    rc = dsm.main(["--backup", str(same), "--live", str(same), "--out", str(out)])
    assert rc != 0
    assert not out.exists()   # nothing written — fail loud BEFORE any work
    captured = capsys.readouterr()
    assert "same" in (captured.out + captured.err).lower()


def test_main_refuses_same_path_via_different_spellings(tmp_path):
    """'.' components / a relative vs absolute spelling of the identical
    file must still be caught — the guard compares RESOLVED paths."""
    same = tmp_path / "same.db"
    _mk_db(same, [(1, "OLD-01")])
    out = tmp_path / "sku_map.csv"
    weird_spelling = str(tmp_path) + "/./same.db"

    rc = dsm.main(["--backup", str(same), "--live", weird_spelling, "--out", str(out)])
    assert rc != 0
    assert not out.exists()


def test_main_same_path_refusal_never_mutates_the_shared_file(tmp_path):
    """The same-path guard must run BEFORE normalize_backup_journal_mode —
    otherwise a same-path call would silently flip journal_mode on what
    could be the LIVE production DB. Pin: a WAL-mode file passed as both
    --backup and --live must still be WAL mode afterward."""
    same = tmp_path / "same.db"
    conn = sqlite3.connect(str(same))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, sku_code TEXT)")
    conn.commit()
    conn.close()
    out = tmp_path / "sku_map.csv"

    rc = dsm.main(["--backup", str(same), "--live", str(same), "--out", str(out)])
    assert rc != 0

    check = sqlite3.connect(str(same))
    mode = check.execute("PRAGMA journal_mode;").fetchone()[0]
    check.close()
    assert mode.lower() == "wal"   # untouched
