"""scripts/preflight_193_unit_vocabulary.py: the lever run on a fresh prod
snapshot minutes before 193 is merged. It must report what would block the
migration or move stock, and must never write to the database it inspects.

Each test starts from the true pre-193 state (the live dev DB this clones may
already carry 193)."""
import importlib.util
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_spec = importlib.util.spec_from_file_location(
    'preflight_193', os.path.join(REPO, 'scripts', 'preflight_193_unit_vocabulary.py'))
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

MIG = '193_unit_vocabulary.sql'
ROLLBACK = os.path.join(REPO, 'data', 'migrations', '193_unit_vocabulary.rollback.sql')


@pytest.fixture
def pre193(tmp_db):
    conn = sqlite3.connect(tmp_db)
    try:
        if conn.execute("SELECT 1 FROM applied_migrations WHERE filename=?", (MIG,)).fetchone():
            conn.executescript(open(ROLLBACK, encoding='utf-8').read())
            conn.execute("DELETE FROM applied_migrations WHERE filename=?", (MIG,))
            conn.commit()
    finally:
        conn.close()
    return tmp_db


def _product(conn, name, unit_type='ตัว'):
    return conn.execute("INSERT INTO products (product_name, unit_type, cost_price) "
                        "VALUES (?, ?, 1)", (name, unit_type)).lastrowid


def _sale(conn, doc_no, pid, unit, synced=0):
    return conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01',?,?,?,'PF193','x','c','C1',2,?,10,0,'',20,20,?)",
        (doc_no, doc_no.rsplit('-', 1)[0], pid, unit, synced)).lastrowid


def test_reports_a_precondition_violator(pre193):
    conn = sqlite3.connect(pre193)
    pid = _product(conn, 'preflight193 conflict')
    bad = conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                       "VALUES (?, 'ช5', 5)", (pid,)).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'ชุด5', 6)",
                 (pid,))
    conn.commit()
    conn.close()

    code, report = preflight.run(pre193)

    assert code == 1
    assert [bad, pid, 'ช5', 5.0, 'ชุด5', 6.0] in report['violators']['twin_ratio']
    assert 'twin_ratio' in report['migration_error']


def test_lists_a_held_line_and_never_writes_the_source(pre193):
    """pid 436's shape: a ช3 line with only a ชุด3 conversion stays ช3 and is
    listed, and the run is still CLEAN. The migration APPLIES here, so this is
    also where "read-only" is provable."""
    conn = sqlite3.connect(pre193)
    pid = _product(conn, 'preflight193 held', unit_type='อัน')
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'ชุด3', 3)",
                 (pid,))
    held = _sale(conn, 'IVPF193-1', pid, 'ช3')
    # a กร twin 193 keeps: listed, never "left behind"
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กร', 1)", (pid,))
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กุรุส', 1)", (pid,))
    moved = _sale(conn, 'IVPF193-2', _product(conn, 'preflight193 moved'), 'คค')
    conn.commit()
    conn.close()

    code, report = preflight.run(pre193)

    assert code == 0, report
    assert [r[:5] for r in report['skipped']] == [['sales_transactions', held, pid, 'ช3', 'ชุด3']]
    assert report['changed']['sales_transactions.unit'] >= 1
    assert report['invariant_mismatches'] == [] and report['left_behind'] == {}
    assert [pid, 'กร', 1.0, 'กุรุส', 1.0] in report['kept_conversions']
    conn = sqlite3.connect(pre193)
    try:
        assert conn.execute("SELECT unit FROM sales_transactions WHERE id=?", (moved,)).fetchone()[0] == 'คค'
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'migration_193%'"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM applied_migrations WHERE filename=?",
                            (MIG,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM unit_map WHERE spelling='ช3'").fetchone()[0] == 0
    finally:
        conn.close()


def test_sees_what_the_migrations_sql_cannot(pre193):
    """The preflight re-derives every line through the app's own
    _get_base_qty, whose short circuit strips ANY Unicode space, while the
    migration's SQL trims a listed set. A unit_type ending in an em space
    (U+2003) makes a คร line on a เครื่อง product syncable as the word, which
    only the Python oracle can see: the preflight must block."""
    conn = sqlite3.connect(pre193)
    pid = _product(conn, 'preflight193 em space', unit_type='เครื่อง ')
    sid = _sale(conn, 'IVPF193-3', pid, 'คร')
    conn.commit()
    conn.close()

    code, report = preflight.run(pre193)

    assert code == 1
    assert ['sales_transactions', sid, None, 2] in report['lines_newly_syncable']
