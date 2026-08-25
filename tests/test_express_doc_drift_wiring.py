"""The detector is wired into the import, and the page says what it cannot see.

Three separate things, because passing one proves nothing about the others:
  · commit_express_dbf runs the scan only when asked (vat_book_builder calls the
    same function against the OTHER book with the OTHER dataset);
  · the results page renders — a render test is what catches a `url_for` on an
    endpoint that does not exist, which pytest and curl on the model layer never
    would (the first version of the template linked `alerts.index`; the real
    endpoint is `inventory.alerts_view`, and every load of the page would have
    500'd);
  · the SCOPE note prints on a CLEAN run too. That is the whole point of it.
"""
import json
import sqlite3

import pytest


def _client(app, role='admin'):
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = role
        s['username'] = 'admin'
        s['user_id'] = 1
    return c


def _stamp_last_run(db, results):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)"
        " VALUES ('express-dbf-upload', 0, 0, ?)",
        (json.dumps(results, ensure_ascii=False),))
    conn.commit()
    conn.close()


CLEAN = {'bsn': {'ok': True, 'summary': 'ok', 'reconcile': {},
                 'doc_drift': {'findings': [],
                               'counters': {'compared': 9917,
                                            'freshness': 'authoritative',
                                            'baseline_loaded': True},
                               'scope': 'ตรวจเฉพาะเอกสารที่ Sendy ถืออยู่ ⛔ ไม่เห็น: ชื่อสินค้า'}}}

DIRTY = {'bsn': {'ok': True, 'summary': 'ok', 'reconcile': {},
                 'doc_drift': {'findings': [
                     {'doc_no': 'IV0001', 'kind': 'content', 'fields': ['line:net'],
                      'message': 'x', 'fingerprint': 'f', 'docstat': ''},
                     {'doc_no': 'IV0001', 'kind': 'source_status', 'fields': ['DOCSTAT'],
                      'message': 'y', 'fingerprint': 'f', 'docstat': 'C'},
                     {'doc_no': 'RR0002', 'kind': 'content', 'fields': ['hdr:date_iso'],
                      'message': 'z', 'fingerprint': 'g', 'docstat': ''}],
                     'counters': {'compared': 9917, 'freshness': 'indeterminate',
                                  'baseline_loaded': False},
                     'scope': 'ตรวจเฉพาะเอกสารที่ Sendy ถืออยู่ ⛔ ไม่เห็น: ชื่อสินค้า'}}}


def test_a_clean_run_still_prints_the_scope(empty_db, monkeypatch):
    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    from app import app
    _stamp_last_run(empty_db, CLEAN)
    r = _client(app).get('/import-express-dbf')
    assert r.status_code == 200, r.status_code          # catches url_for BuildError
    body = r.get_data(as_text=True)
    assert 'เอกสารที่ถูกแก้ที่ต้นทาง: พบ 0 ใบ' in body
    assert 'doc-drift-scope' in body and '⛔ ไม่เห็น' in body


def test_findings_are_counted_by_document_not_by_finding(empty_db, monkeypatch):
    """IV0001 produces TWO findings (content + source_status). The page must say
    2 documents, not 3 — a count of findings would inflate every cancelled
    document that also drifted."""
    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    from app import app
    _stamp_last_run(empty_db, DIRTY)
    body = _client(app).get('/import-express-dbf').get_data(as_text=True)
    assert 'เอกสารที่ถูกแก้ที่ต้นทาง: พบ 2 ใบ' in body
    assert 'อ่านเวลาส่งออกของ zip ไม่ได้' in body       # freshness warning
    assert 'อ่านไฟล์ baseline ไม่ได้' in body           # missing-baseline warning
    assert '/alerts' in body                            # the link resolves
    assert '⛔ ไม่เห็น' in body                          # scope still printed


def test_commit_runs_the_scan_only_when_asked(monkeypatch, tmp_path, empty_db):
    """vat_book_builder calls commit_express_dbf against vat_book.db with the
    xp5 dataset. The shipped baseline describes BSN5657's documents, so a scan
    that turned itself on by default would report the other book's whole history
    as drift."""
    import import_router
    import express_dbf_source as eds
    calls = []
    monkeypatch.setattr(eds, 'run_document_drift_scan',
                        lambda *a, **k: calls.append(k) or {'findings': []})
    monkeypatch.setattr(eds, 'open_table', lambda *a, **k: [])
    for name in ('build_sales_entries', 'build_purchase_entries', 'build_invoice_refs',
                 'build_payments_in_records', 'build_payments_out_records',
                 'build_credit_notes_ar_records', 'build_credit_notes_ap_records'):
        monkeypatch.setattr(eds, name, lambda *a, **k: [])
    # empty_db carries the schema but no seed rows, and the payments importer
    # refuses without a company. One row, not a stub, so the call really walks
    # the function to the scan at the end of it.
    seed = sqlite3.connect(str(empty_db))
    seed.execute("INSERT OR IGNORE INTO companies (code, name_th) VALUES ('BSN','x')")
    seed.commit(); seed.close()

    import models
    monkeypatch.setattr(models, 'import_weekly', lambda *a, **k: {'imported': 0})
    monkeypatch.setattr(models, 'scan_reconcile', lambda *a, **k: {})
    monkeypatch.setattr(import_router, '_upsert_invoice_refs', lambda *a, **k: 0)

    import_router.commit_express_dbf(str(tmp_path), db_path=str(empty_db))
    assert calls == [], 'the scan ran without being asked'

    import_router.commit_express_dbf(str(tmp_path), db_path=str(empty_db),
                                     detect_drift=True, export_at=None)
    assert len(calls) == 1, 'detect_drift=True did not reach the scan'
    assert 'export_at' in calls[0], 'export_at was not passed through'


def test_the_upload_route_asks_for_the_scan_and_passes_the_real_export_time():
    """⚠ A STRUCTURAL check, not an execution one — say so rather than let it
    read as an integration test. The upload route needs a zip, a lock, a backup
    and a watermark before it reaches this call, so exercising it end to end
    here would test the scaffolding. What actually rots is the call's KEYWORDS:
    drop `detect_drift=True` and the feature quietly never runs in production
    while every other test stays green, and pass `effective_export_at` instead
    of `export_at` and the scan starts claiming documents were deleted at source
    on a value that means "the clock said so".

    Read from the AST, so a comment mentioning either name cannot satisfy it.
    """
    import ast
    import inspect

    import blueprints.bsn as bsn

    tree = ast.parse(inspect.getsource(bsn))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == 'commit_express_dbf']
    # CONTROL: if the call cannot be found the assertions below are vacuous.
    assert len(calls) == 1, f'{len(calls)} call sites, expected exactly 1'
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get('detect_drift'), ast.Constant) and kw['detect_drift'].value is True
    assert isinstance(kw.get('export_at'), ast.Name), 'export_at must be a plain name'
    assert kw['export_at'].id == 'export_at', (
        f"passes {kw['export_at'].id} — effective_export_at falls back to today, "
        "and a fallback cannot decide that a document was deleted at source")
