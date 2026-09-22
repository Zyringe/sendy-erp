"""TDD for the pid 436 `ช3` sync (Put 2026-09-22, decisions/log.md).

Two Shopee sales lines of pid 436 (อะไหล่ลูกกลิ้งทาสีหนา 13mm, unit_type อัน) were
stored as `ช3`, a code the unit map did not know, so they never found the
`ชุด3` = 3 conversion and never deducted stock. #610 teaches the map `ช3` ->
ชุด3; scripts/2026_09_22_sync_436_ch3.py then relabels those two lines to the
word and syncs them through the app's own engine: stock −6, nothing else.

The fixture stubs #610 by inserting the `ช3` row into this COPY's unit map
(`_seed(taught=False)` leaves it out). A bystander product holds an unsynced
line that already HAS a conversion, so a table-wide sync would take it: that is
what makes "no other product touched" a test that can fail.
"""
import importlib.util
import json
import pathlib
import sqlite3

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / '2026_09_22_sync_436_ch3.py'
ACTOR = 'script:2026_09_22_sync_436_ch3'
LINES = ('IV6901461-2', 'IV6901500-2')
CODE_436 = '999อ1501'
CODE_437 = '999อ1502'


def _load():
    spec = importlib.util.spec_from_file_location('sync_436_ch3', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(conn, taught=True):
    from models import bsn_sync, wacc
    if taught:
        conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'ช3', 'ชุด3')")
    for pid, name, code, uc in (
            (436, 'อะไหล่ลูกกลิ้งทาสีหนา 13mm Microfiber Sendai 1in', CODE_436,
             {'อัน': 1.0, 'ชุด3': 3.0, 'ช5': 5.0}),
            (437, 'ผู้ยืนดู', CODE_437, {'ชุด3': 3.0})):
        conn.execute("INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
                     " opening_cost, low_stock_threshold, is_active) VALUES (?,?,'อัน',0,0,0,10,1)",
                     (pid, name))
        for u, r in uc.items():
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                         (pid, u, r))
        conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)"
                     " VALUES (?,?,?)", (code, name, pid))
    for seq, (pid, code, qty, price) in enumerate(((436, CODE_436, 100.0, 11.0),
                                                   (437, CODE_437, 50.0, 5.0)), 1):
        conn.execute(
            "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
            " qty, unit, unit_price, net, total, vat_type, synced_to_stock, line_seq, supplier_code)"
            " VALUES ('2026-01-10','RR6900010','RR6900010',?,?,?,'อัน',?,?,?,1,0,?,'S1')",
            (pid, code, qty, price, qty * price, qty * price, seq))
    for date, doc, qty, unit, price in (('2026-02-01', 'IV6900100-1', 10.0, 'อัน', 20.0),
                                        ('2026-03-01', 'IV6900200-1', 2.0, 'ช5', 90.0),
                                        ('2026-08-31', 'IV6901461-2', 1.0, 'ช3', 100.0),
                                        ('2026-09-04', 'IV6901500-2', 1.0, 'ช3', 100.0)):
        _sale(conn, date, doc, 436, CODE_436, qty, unit, price)
    for pid in (436, 437):
        bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
        bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
        wacc.recalculate_product_wacc(pid, conn)
    # after the sync: a pending line a table-wide sync WOULD take (437 has ชุด3)
    _sale(conn, '2026-09-01', 'IV6901470-1', 437, CODE_437, 2.0, 'ชุด3', 30.0)
    conn.commit()


def _sale(conn, date, doc, pid, code, qty, unit, price):
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
        " product_name_raw, customer, customer_code, qty, unit, unit_price, net, total, vat_type,"
        " synced_to_stock) VALUES (?,?,?,?,?,'x','หน้าร้านS','Zหน้าร้าน',?,?,?,?,?,1,0)",
        (date, doc, doc.split('-')[0], pid, code, qty, unit, price, qty * price, qty * price))


@pytest.fixture()
def db(empty_db, empty_db_conn):
    _seed(empty_db_conn)
    return str(empty_db)


@pytest.fixture()
def untaught_db(empty_db, empty_db_conn):
    _seed(empty_db_conn, taught=False)
    return str(empty_db)


def _q(path, sql, *args):
    c = sqlite3.connect(path)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _exec(path, sql, *args):
    import actor
    c = actor.install(sqlite3.connect(path))
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _run(path, *extra, mode='rehearse'):
    return _load().main(['--db', path, '--mode', mode, '--operator', 'pytest',
                         '--reason', 'test run of the pid 436 ช3 sync', *extra])


def _stock(path, pid=436):
    rows = _q(path, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)
    return rows[0][0] if rows else None


def _lines(path):
    return _q(path, "SELECT doc_no, unit, synced_to_stock FROM sales_transactions"
                    " WHERE doc_no IN (?, ?) ORDER BY doc_no", *LINES)


def _state(path):
    """Everything a run could touch, provenance columns included."""
    return [
        _q(path, "SELECT * FROM sales_transactions ORDER BY id"),
        _q(path, "SELECT * FROM purchase_transactions ORDER BY id"),
        _q(path, "SELECT * FROM transactions ORDER BY id"),
        _q(path, "SELECT product_id, quantity FROM stock_levels ORDER BY 1"),
        _q(path, "SELECT id, unit_type, printf('%.17g', cost_price), printf('%.17g', opening_cost)"
                 " FROM products ORDER BY id"),
        _q(path, "SELECT * FROM product_cost_ledger ORDER BY id"),
        _q(path, "SELECT * FROM unit_conversions ORDER BY id"),
        _q(path, "SELECT COUNT(*) FROM audit_log"),
    ]


def _content(path):
    """Every sales column except the mig-173 provenance four."""
    cols = [r[0] for r in _q(path, "SELECT name FROM pragma_table_info('sales_transactions')")
            if not r[0].startswith('change_')]
    assert 'unit' in cols and 'synced_to_stock' in cols
    return _q(path, "SELECT %s FROM sales_transactions ORDER BY id" % ', '.join(cols))


def _outside_436(path):
    return [
        _q(path, "SELECT product_id, quantity FROM stock_levels WHERE product_id <> 436 ORDER BY 1"),
        _q(path, "SELECT * FROM transactions WHERE product_id <> 436 ORDER BY id"),
        _q(path, "SELECT id, printf('%.17g', cost_price) FROM products ORDER BY id"),
        _q(path, "SELECT * FROM product_cost_ledger ORDER BY id"),
        _q(path, "SELECT id, unit, synced_to_stock FROM sales_transactions"
                 " WHERE doc_no NOT IN (?, ?) ORDER BY id", *LINES),
    ]


# ── the forward run ─────────────────────────────────────────────────────────

def test_the_fixture_is_the_prod_shape(db):
    assert _stock(db) == 80
    assert _lines(db) == [('IV6901461-2', 'ช3', 0), ('IV6901500-2', 'ช3', 0)]
    assert _q(db, "SELECT synced_to_stock FROM sales_transactions WHERE doc_no='IV6901470-1'") == [(0,)]


def test_dry_run_writes_nothing(db, capsys):
    before = _state(db)
    assert _run(db, mode='dry-run') == 0
    out = capsys.readouterr().out
    assert 'DRY RUN' in out and '80' in out and '74' in out, out
    assert _state(db) == before


def test_rehearse_syncs_the_two_lines_at_ratio_3(db):
    assert _run(db) == 0
    assert _stock(db) == 74
    assert _lines(db) == [('IV6901461-2', 'ชุด3', 1), ('IV6901500-2', 'ชุด3', 1)]
    legs = _q(db, "SELECT reference_no, txn_type, quantity_change, note, created_at FROM transactions"
                  " WHERE product_id=436 AND reference_no IN (?, ?) ORDER BY reference_no", *LINES)
    assert legs == [('IV6901461-2', 'OUT', -3, 'BSN ขาย', '2026-08-31 00:00:00'),
                    ('IV6901500-2', 'OUT', -3, 'BSN ขาย', '2026-09-04 00:00:00')]
    assert _q(db, "SELECT SUM(quantity_change) FROM transactions WHERE product_id=436") == [(74,)]


def test_no_other_product_and_no_cost_moves(db):
    before = _outside_436(db)
    cost = _q(db, "SELECT printf('%.17g', cost_price) FROM products WHERE id=436")
    assert cost == [('11',)], 'control: the fixture costed pid 436'
    assert _run(db) == 0
    assert _outside_436(db) == before
    assert _q(db, "SELECT synced_to_stock FROM sales_transactions WHERE doc_no='IV6901470-1'") == [(0,)]


def test_the_relabel_is_declared(db):
    assert _run(db) == 0
    rows = _q(db, "SELECT change_source, change_actor, change_reason, change_token"
                  " FROM sales_transactions WHERE doc_no IN (?, ?)", *LINES)
    assert [r[:2] for r in rows] == [('manual', ACTOR)] * 2
    assert all('pytest' in r[2] and 'ช3' in r[2] for r in rows), rows
    assert len({r[3] for r in rows}) == 2, 'one fresh token per line'
    audit = _q(db, "SELECT changed_fields FROM audit_log WHERE table_name='sales_transactions'"
                   " AND action='UPDATE' AND user=?", ACTOR)
    assert [json.loads(a[0]) for a in audit] == [{'unit': ['ช3', 'ชุด3']}] * 2


def test_a_second_run_is_refused(db, capsys):
    assert _run(db) == 0
    state = _state(db)
    capsys.readouterr()
    assert _run(db) == 2
    assert 'already' in capsys.readouterr().out
    assert _state(db) == state


def test_the_word_comes_from_the_map(db):
    """A census `through_map` claim, checked by behaviour: the stored word is
    whatever the map says, and a map saying anything but ชุด3 is refused."""
    _exec(db, "UPDATE unit_map SET word='ชุด' WHERE book='BSN5657' AND spelling='ช3'")
    before = _state(db)
    assert _run(db) == 2
    assert _state(db) == before


# ── refusals: nothing is written ────────────────────────────────────────────

def test_refuses_before_610_teaches_the_map(untaught_db, capsys):
    before = _state(untaught_db)
    assert _run(untaught_db) == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and '#610' in out, out
    assert _state(untaught_db) == before


@pytest.mark.parametrize('breakage, needle', [
    ("UPDATE sales_transactions SET synced_to_stock=1 WHERE doc_no='IV6901500-2'", 'IV6901500-2'),
    ("UPDATE sales_transactions SET doc_no='IV6901500-9', change_source='manual',"
     " change_actor='t', change_reason='moved by a test fixture', change_token='x1'"
     " WHERE doc_no='IV6901500-2'", 'IV6901500-2'),
    ("UPDATE sales_transactions SET unit='ชุด', change_source='manual', change_actor='t',"
     " change_reason='rekeyed by a test fixture', change_token='x2'"
     " WHERE doc_no='IV6901461-2'", 'IV6901461-2'),
    ("UPDATE sales_transactions SET qty=2, change_source='manual', change_actor='t',"
     " change_reason='rekeyed by a test fixture', change_token='x3'"
     " WHERE doc_no='IV6901461-2'", 'IV6901461-2'),
], ids=['already-synced', 'missing', 'unit-differs', 'qty-differs'])
def test_refuses_unless_both_lines_are_still_ch3_and_unsynced(db, capsys, breakage, needle):
    _exec(db, breakage)
    before = _state(db)
    assert _run(db) == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and needle in out, out
    assert _state(db) == before, 'all or nothing: the other line is untouched too'


def test_refuses_when_the_conversion_is_not_3(db, capsys):
    _exec(db, "UPDATE unit_conversions SET ratio=5 WHERE product_id=436 AND bsn_unit='ชุด3'")
    before = _state(db)
    assert _run(db) == 2
    assert 'ratio' in capsys.readouterr().out
    assert _state(db) == before


def test_refuses_another_pending_line_on_436(db, capsys):
    """The sync is scoped to pid 436, so any other pending 436 line would sync with these."""
    import actor
    c = actor.install(sqlite3.connect(db))
    _sale(c, '2026-09-10', 'IV6901600-1', 436, CODE_436, 2.0, 'อัน', 20.0)
    c.commit()
    c.close()
    before = _state(db)
    assert _run(db) == 2
    assert 'IV6901600-1' in capsys.readouterr().out
    assert _state(db) == before


def test_refuses_when_a_costing_in_follows_the_sales(db, capsys):
    """WACC weighs each purchase by the stock on hand; an OUT posted before a later
    purchase would move the cost the next rebuild computes."""
    _exec(db, "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
              " reference_no, note, created_at) VALUES (436, 'IN', 10, 'unit', 'RR6900500',"
              " 'BSN ซื้อ', '2026-09-10 00:00:00')")
    before = _state(db)
    assert _run(db) == 2
    assert 'WACC' in capsys.readouterr().out
    assert _state(db) == before


def test_a_failed_invariant_rolls_everything_back(db, capsys, monkeypatch):
    """Mutant sync: table-wide, as the importer calls it. It also takes the
    bystander's pending line, so the in-transaction checks must refuse."""
    from models import bsn_sync
    real = bsn_sync._sync_bsn_to_stock
    monkeypatch.setattr(bsn_sync, '_sync_bsn_to_stock',
                        lambda conn, table, file_type, product_ids=None: real(conn, table, file_type))
    before = _state(db)
    assert _run(db) == 1
    out = capsys.readouterr().out
    assert 'ROLLED BACK' in out and 'pid 437' in out, out
    assert _state(db) == before


def test_a_failed_backup_refuses_cleanly(db, capsys, monkeypatch):
    import db_backup

    def refuse(*a, **kw):
        raise db_backup.BackupRefused('no space left on device')
    monkeypatch.setattr(db_backup, 'guarded_backup', refuse)
    before = _state(db)
    assert _run(db, '--confirm-live', '436', mode='live') == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and 'no space left' in out and 'Traceback' not in out, out
    assert _state(db) == before


def test_rehearse_refuses_the_app_db_and_live_needs_confirmation(db, tmp_path, capsys):
    app_db = tmp_path / 'inventory.db'
    src, dst = sqlite3.connect(db), sqlite3.connect(str(app_db))
    src.backup(dst)
    src.close()
    dst.close()
    assert _run(str(app_db)) == 2
    assert _run(db, mode='live') == 2
    assert _run(db, '--confirm-live', '600', mode='live') == 2
    assert 'confirm' in capsys.readouterr().out
    assert _lines(db) == _lines(str(app_db)) == [('IV6901461-2', 'ช3', 0), ('IV6901500-2', 'ช3', 0)]


# ── undo ────────────────────────────────────────────────────────────────────

def test_undo_restores_exactly_and_a_forward_run_may_follow(db):
    before, content = _state(db), _content(db)
    assert _run(db) == 0
    assert _stock(db) == 74
    assert _run(db, '--undo') == 0
    after = _state(db)
    assert _content(db) == content, 'bills back, all but the provenance columns'
    assert after[1:7] == before[1:7], 'ledger, stock, cost, conversions exactly as before'
    assert _stock(db) == 80
    assert len(_q(db, "SELECT id FROM audit_log WHERE user=?", ACTOR + ':undo')) == 2
    assert _run(db) == 0, 'after an undo the sync may run again'
    assert _stock(db) == 74


def test_undo_with_nothing_to_undo_is_refused(db, capsys):
    before = _state(db)
    assert _run(db, '--undo') == 2
    assert 'nothing' in capsys.readouterr().out
    assert _state(db) == before


def test_undo_refuses_once_436s_ledger_moved(db, capsys):
    """A count after the sync may already absorb these 6 pieces; handing them back
    would then invent stock. The undo cannot tell, so it refuses."""
    assert _run(db) == 0
    _exec(db, "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
              " reference_no, note, created_at) VALUES (436, 'ADJUST', -2, 'unit', NULL,"
              " 'นับสต็อก', '2026-09-23 10:00:00')")
    state = _state(db)
    capsys.readouterr()
    assert _run(db, '--undo') == 2
    assert 'ledger' in capsys.readouterr().out
    assert _state(db) == state


def test_undo_refuses_a_line_changed_since(db, capsys):
    assert _run(db) == 0
    _exec(db, "UPDATE sales_transactions SET change_source='import', change_actor='express_dbf:x',"
              " change_reason=NULL WHERE doc_no='IV6901500-2'")
    state = _state(db)
    capsys.readouterr()
    assert _run(db, '--undo') == 2
    assert 'IV6901500-2' in capsys.readouterr().out
    assert _state(db) == state


# ── the importer reads the synced lines as unchanged ────────────────────────

def _reimport(path, monkeypatch, qty=1.0):
    import config
    import database
    import express_dbf_source as eds
    from models import imports
    monkeypatch.setattr(config, 'DATABASE_PATH', path)
    monkeypatch.setattr(database, 'DATABASE_PATH', path)
    import datetime
    heads, stcrd = [], []
    for doc, date in (('IV6901461', '2026-08-31'), ('IV6901500', '2026-09-04')):
        heads.append({'DOCNUM': doc, 'RECTYP': '3', 'DOCDAT': datetime.date.fromisoformat(date),
                      'FLGVAT': 1, 'CUSCOD': 'Zหน้าร้าน'})
        stcrd.append({'DOCNUM': doc, 'SEQNUM': '2', 'STKCOD': CODE_436, 'TQUCOD': 'ช3',
                      'TRNQTY': qty, 'TFACTOR': 3.0, 'DOCDAT': datetime.date.fromisoformat(date),
                      'UNITPR': 100.0, 'TRNVAL': 100.0 * qty, 'NETVAL': 100.0 * qty,
                      'DISC': '', 'STKDES': 'x'})
    entries = eds.build_sales_entries(heads, stcrd, [{'CUSCOD': 'Zหน้าร้าน', 'CUSNAM': 'หน้าร้านS'}])
    assert [e['doc_no'] for e in entries] == list(LINES)
    return imports.import_weekly(entries, 'sales', 'test-436-reimport')


def test_reimporting_the_express_lines_after_the_sync_is_a_no_op(db, monkeypatch):
    assert _run(db) == 0
    before = _state(db)[1:7]
    stats = _reimport(db, monkeypatch)
    assert stats['unchanged'] == 2 and stats['overwritten'] == 0 and stats['imported'] == 0, stats
    assert _state(db)[1:7] == before
    assert _lines(db) == [('IV6901461-2', 'ชุด3', 1), ('IV6901500-2', 'ชุด3', 1)]


def test_control_the_reimport_does_reach_these_lines(db, monkeypatch):
    """The no-op above is only evidence if the same path rewrites a line that differs."""
    assert _run(db) == 0
    stats = _reimport(db, monkeypatch, qty=2.0)
    assert stats['overwritten'] == 2, stats
