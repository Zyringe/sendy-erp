"""Detached Express register import: result reporting, ordering, and cleanup."""
import datetime
import importlib
import json
import os
import sqlite3


MOVED_KEYS = {'general_ledger', 'sales_orders', 'bank_cheques'}


def _insert_run(db_path, notes):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
        "VALUES ('express-dbf-upload', 0, 0, ?)",
        (json.dumps(notes, ensure_ascii=False),))
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


def _notes(db_path, run_id):
    conn = sqlite3.connect(db_path)
    raw = conn.execute("SELECT notes FROM import_log WHERE id=?", (run_id,)).fetchone()[0]
    conn.close()
    return json.loads(raw)


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ('express_gl_accounts', 'express_gl_vouchers',
                          'express_gl_lines', 'express_sales_orders',
                          'express_sales_order_lines', 'express_bank_cheques')
        }
    finally:
        conn.close()


def _fixture_tables():
    today = datetime.date.today()
    return {
        'GLACC': [{
            'ACCNUM': '11-01', 'ACCNAM': 'เงินสด', 'LEVEL': 1, 'PARENT': '',
            'ACCTYP': 'A', 'NATURE': 'D', 'STATUS': 'N',
        }],
        'GLJNL': [{
            'VOUCHER': 'JV-676', 'VOUDAT': today, 'JNLTYP': 'JV',
            'REFNUM': '', 'DESCRP': 'fixture', 'SRCJNL': 'JV', 'DOCSTAT': 'N',
        }],
        'GLJNLIT': [{
            'VOUCHER': 'JV-676', 'SEQIT': 1, 'VOUDAT': today,
            'ACCNUM': '11-01', 'DESCRP': 'fixture', 'TRNTYP': '0',
            'AMOUNT': 125.0,
        }],
        'OESO': [{
            'SONUM': 'SO-676', 'SODAT': today, 'CUSCOD': 'C676',
            'SLMCOD': '06', 'YOUREF': '', 'PAYTRM': 30, 'DLVDAT': today,
            'CMPLDAT': None, 'TOTAL': 250.0, 'DISCAMT': 0.0,
            'VATAMT': 0.0, 'NETAMT': 250.0, 'DOCSTAT': 'N',
        }],
        'OESOIT': [{
            'SONUM': 'SO-676', 'SEQNUM': 1, 'STKCOD': 'SKU-676',
            'STKDES': 'fixture item', 'ORDQTY': 2.0, 'CANCELQTY': 0.0,
            'REMQTY': 2.0, 'TQUCOD': 'ตัว', 'UNITPR': 125.0,
            'TRNVAL': 250.0,
        }],
        'ARMAS': [{'CUSCOD': 'C676', 'CUSNAM': 'fixture customer'}],
        'BKTRN': [{
            'BKTRNTYP': 'QR', 'CHQNUM': 'CHQ-676', 'TRNDAT': today,
            'CHQDAT': today, 'GETDAT': today, 'PAYINDAT': None,
            'BNKCOD': 'KBANK', 'BRANCH': 'HQ', 'BNKACC': '123',
            'CUSCOD': 'C676', 'NAME': 'fixture customer', 'AMOUNT': 500.0,
            'CHARGE': 0.0, 'VATAMT': 0.0, 'NETAMT': 500.0,
            'REMAMT': 500.0, 'CHQSTAT': '10', 'REMARK': '', 'REFDOC': '',
            'REFNUM': '', 'VOUCHER': 'RV-676',
        }],
    }


def _dataset(root, names):
    source = root / 'dataset'
    source.mkdir(parents=True)
    for name in names:
        (source / f'{name}.DBF').write_bytes(b'fixture')
    return source


def _patch_open_table(monkeypatch, selected):
    import express_dbf_source as eds

    def fake_open(_dataset_dir, name):
        tables = selected['tables']
        if name not in tables:
            raise FileNotFoundError(name)
        return list(tables[name])

    monkeypatch.setattr(eds, 'open_table', fake_open)


def test_builder_lands_all_registers_reports_done_and_cleans_up(
        empty_db, tmp_path, monkeypatch):
    builder = importlib.import_module('express_registers_builder')
    tables = _fixture_tables()
    selected = {'tables': tables}
    _patch_open_table(monkeypatch, selected)
    cleanup = tmp_path / 'register-job'
    source = _dataset(cleanup, tables)
    run_id = _insert_run(empty_db, {'registers': {'status': 'building'}})

    builder.main(['--source', str(source), '--db', str(empty_db),
                  '--run-id', str(run_id), '--cleanup-dir', str(cleanup)])

    assert _counts(empty_db) == {
        'express_gl_accounts': 1,
        'express_gl_vouchers': 1,
        'express_gl_lines': 1,
        'express_sales_orders': 1,
        'express_sales_order_lines': 1,
        'express_bank_cheques': 1,
    }
    result = _notes(empty_db, run_id)['registers']
    assert result['status'] == 'done'
    assert result['general_ledger'] == {'accounts': 1, 'vouchers': 1, 'lines': 1}
    assert result['sales_orders'] == {'orders': 1, 'lines': 1}
    assert result['bank_cheques'] == {'stored': 1}
    assert result['finished_at']
    assert not cleanup.exists()
    assert os.path.exists(f'{empty_db}.registers.lock'), 'the persistent lock was unlinked'


def test_builder_skips_a_superseded_run_without_writing_registers(
        empty_db, tmp_path, monkeypatch):
    builder = importlib.import_module('express_registers_builder')
    import import_router

    cleanup = tmp_path / 'register-job'
    source = _dataset(cleanup, ['GLJNL'])
    old_run = _insert_run(empty_db, {'registers': {'status': 'building'}})
    newer_run = _insert_run(empty_db, {'registers': {'status': 'building'}})
    calls = []
    monkeypatch.setattr(import_router, 'commit_express_registers',
                        lambda *a, **k: calls.append((a, k)))

    builder.main(['--source', str(source), '--db', str(empty_db),
                  '--run-id', str(old_run), '--cleanup-dir', str(cleanup)])

    assert calls == []
    result = _notes(empty_db, old_run)['registers']
    assert result['status'] == 'superseded'
    assert MOVED_KEYS <= set(result)
    assert _notes(empty_db, newer_run)['registers']['status'] == 'building'
    assert all(count == 0 for count in _counts(empty_db).values())
    assert not cleanup.exists()


def test_partial_gl_reports_error_keeps_stored_register_and_cleans_up(
        empty_db, tmp_path, monkeypatch):
    builder = importlib.import_module('express_registers_builder')
    import bsn_units
    import import_router

    tables = _fixture_tables()
    selected = {'tables': {
        name: tables[name] for name in ('GLACC', 'GLJNL', 'GLJNLIT')
    }}
    _patch_open_table(monkeypatch, selected)
    seeded = import_router.commit_express_registers(
        '/seed', str(empty_db), bsn_units.DEFAULT_BOOK)
    assert seeded['general_ledger'] == {'accounts': 1, 'vouchers': 1, 'lines': 1}
    before = _counts(empty_db)

    selected['tables'] = {'GLJNL': tables['GLJNL']}
    cleanup = tmp_path / 'register-job'
    source = _dataset(cleanup, ['GLJNL'])
    run_id = _insert_run(empty_db, {'registers': {'status': 'building'}})

    builder.main(['--source', str(source), '--db', str(empty_db),
                  '--run-id', str(run_id), '--cleanup-dir', str(cleanup)])

    result = _notes(empty_db, run_id)['registers']
    assert result['status'] == 'error'
    assert 'GLJNLIT' in result['general_ledger']['error']
    assert _counts(empty_db) == before
    assert not cleanup.exists()


def test_builder_reports_an_unexpected_job_error_and_cleans_up(
        empty_db, tmp_path, monkeypatch):
    builder = importlib.import_module('express_registers_builder')
    import import_router

    cleanup = tmp_path / 'register-job'
    source = _dataset(cleanup, [])
    run_id = _insert_run(empty_db, {'registers': {'status': 'building'}})
    monkeypatch.setattr(
        import_router, 'commit_express_registers',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('unexpected register failure')))

    try:
        builder.main(['--source', str(source), '--db', str(empty_db),
                      '--run-id', str(run_id), '--cleanup-dir', str(cleanup)])
    except RuntimeError:
        pass

    result = _notes(empty_db, run_id)['registers']
    assert result['status'] == 'error'
    assert 'unexpected register failure' in result['error']
    assert MOVED_KEYS <= set(result)
    assert not cleanup.exists()
