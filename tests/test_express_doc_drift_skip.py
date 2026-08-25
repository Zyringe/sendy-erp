"""A skipped scan must never look like a clean one — three ways.

The whole feature exists because a wrong bill can sit there with nobody knowing.
A watchman that switches ITSELF off quietly is the same failure wearing our own
badge, so the skip has to reach three different places:

  · the data still lands            (the scan is the last thing, and read-only)
  · the person who uploaded sees it (the results page and a flash)
  · Put sees it                     (an alert — he is not the person who uploaded)
"""
import json
import sqlite3
import time

import pytest


def _client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'], s['username'], s['user_id'] = 'admin', 'admin', 1
    return c


def _stamp(db, results):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)"
                 " VALUES ('express-dbf-upload', 0, 0, ?)",
                 (json.dumps(results, ensure_ascii=False),))
    conn.commit(); conn.close()


# ── 1. the data lands, and the scan is refused rather than half-run ─────────
def test_no_room_left_skips_the_scan_but_still_imports(monkeypatch, tmp_path, empty_db):
    import import_router, express_dbf_source as eds, models
    seed = sqlite3.connect(str(empty_db))
    seed.execute("INSERT OR IGNORE INTO companies (code, name_th) VALUES ('BSN','x')")
    seed.commit(); seed.close()

    ran, imported = [], []
    monkeypatch.setattr(eds, 'run_document_drift_scan',
                        lambda *a, **k: ran.append(1) or {'findings': [], 'counters': {}})
    monkeypatch.setattr(eds, 'open_table', lambda *a, **k: [])
    for name in ('build_sales_entries', 'build_purchase_entries', 'build_invoice_refs',
                 'build_payments_in_records', 'build_payments_out_records',
                 'build_credit_notes_ar_records', 'build_credit_notes_ap_records'):
        monkeypatch.setattr(eds, name, lambda *a, **k: [])
    monkeypatch.setattr(models, 'import_weekly',
                        lambda *a, **k: imported.append(1) or {'imported': 0})
    monkeypatch.setattr(models, 'scan_reconcile', lambda *a, **k: {})
    monkeypatch.setattr(import_router, '_upsert_invoice_refs', lambda *a, **k: 0)

    # CONTROL: with room to spare the scan DOES run, so "it skipped" below
    # cannot be explained by the scan never being reachable.
    ok = import_router.commit_express_dbf(str(tmp_path), db_path=str(empty_db),
                                          detect_drift=True, started_at=time.monotonic())
    assert ran == [1] and ok['doc_drift'].get('skipped') is None

    # A request that started 59 seconds ago has no room for a 12s reserve.
    out = import_router.commit_express_dbf(
        str(tmp_path), db_path=str(empty_db), detect_drift=True,
        started_at=time.monotonic() - 59)
    assert ran == [1], 'the scan ran with no time left'
    assert out['doc_drift']['skipped'], 'the skip was silent'
    assert out['doc_drift']['scope'], 'even a skipped run states what it cannot see'
    # …and the import itself still happened, both times.
    assert len(imported) == 4, f'import_weekly called {len(imported)} times, expected 4'


# ── 2. the person who uploaded sees it ──────────────────────────────────────
def test_the_results_page_shows_a_skip_differently_from_a_clean_run(empty_db, monkeypatch):
    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    from app import app
    _stamp(empty_db, {'bsn': {'ok': True, 'summary': 'ok', 'reconcile': {},
                              'doc_drift': {'skipped': 'เหลือเวลาไม่พอ (3 วินาที)',
                                            'scope': 'ตรวจเฉพาะ ⛔ ไม่เห็น: ชื่อสินค้า'}}})
    body = _client(app).get('/import-express-dbf').get_data(as_text=True)
    assert 'doc-drift-skipped' in body
    assert 'ไม่ต้องอัปโหลดใหม่' in body, 'the team is not told the data is safe'
    assert 'พบ 0 ใบ' not in body, 'a skip must not render as a clean result'
    assert '⛔ ไม่เห็น' in body, 'the scope note is dropped on a skip'


# ── 3. Put sees it, once, with a count — and it clears itself ───────────────
def test_the_alert_counts_repeats_and_clears_when_the_scan_runs(empty_db, monkeypatch):
    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    from models import system_alerts as sa

    def rows():
        c = sqlite3.connect(str(empty_db)); c.row_factory = sqlite3.Row
        out = [dict(r) for r in c.execute(
            "SELECT message, context_json, resolved_at FROM system_alerts WHERE kind=?",
            (sa.KIND_EXPRESS_DRIFT_SKIPPED,))]
        c.close(); return out

    for _ in range(3):
        sa.record_express_doc_drift_skipped_alert('เหลือเวลาไม่พอ', elapsed=2.0)
    r = rows()
    assert len(r) == 1, f'{len(r)} rows — three skips must not stack three alerts'
    assert json.loads(r[0]['context_json'])['times'] == 3
    assert '3 ครั้ง' in r[0]['message'], r[0]['message']
    assert r[0]['resolved_at'] is None

    assert sa.clear_express_doc_drift_skipped_alert() == 1
    r = rows()
    assert len(r) == 1 and r[0]['resolved_at'] is not None, 'the alert did not clear'

    # …and a skip AFTER the clear starts a fresh count, not a resurrection.
    sa.record_express_doc_drift_skipped_alert('เหลือเวลาไม่พอ')
    r = [x for x in rows() if x['resolved_at'] is None]
    assert len(r) == 1 and json.loads(r[0]['context_json'])['times'] == 1
