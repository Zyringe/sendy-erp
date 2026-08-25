"""Two failures that only appear on day two of using this thing.

1. ONE DOCUMENT, ONE ALERT, ALL OF THE FACTS. A cancelled-at-source document
   normally drifts in content too (Express dropped its lines), so it produces
   two findings. create_system_alert dedupes on (kind, dedupe_key), so the
   second one is silently discarded — and the fact that survives is whichever
   happened to be appended first. For IV6900631, the motivating case of this
   whole feature, that means the alert says "line count differs" and never
   mentions that Express CANCELLED the document.

2. ACKNOWLEDGING MUST STICK UNTIL SOMETHING CHANGES. create_system_alert's
   contract is that a resolved incident may alert again, "because a recurrence
   is news". For drift that contract inverts: the disagreement persists until
   somebody edits data, so acknowledging it would raise the identical alert on
   the very next upload, and the next, for ever. The plan's own ship gate says a
   detector that cries wolf teaches people to stop reading /alerts. A drift
   recurrence is only news when the FINGERPRINT moved.
"""
import json
import sqlite3

import pytest

from models import system_alerts as sa


def _finding(doc, kind, fingerprint='fp1', **kw):
    d = {'doc_no': doc, 'kind': kind, 'fields': [kind], 'fingerprint': fingerprint,
         'docstat': '', 'message': f'{kind} บน {doc}'}
    d.update(kw)
    return d


def _alerts(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT id, dedupe_key, message, context_json, resolved_at FROM system_alerts"
        " WHERE kind=? ORDER BY id", (sa.KIND_EXPRESS_DOC_DRIFT,))]
    c.close()
    return rows


@pytest.fixture
def db(empty_db, monkeypatch):
    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    return empty_db


def test_one_alert_carries_every_finding_for_that_document(db):
    sa.record_express_doc_drift_alerts([
        _finding('IV6900631', 'content', message='ต่างที่ line:LINE_COUNT'),
        _finding('IV6900631', 'source_status', docstat='C',
                 message='DOCSTAT = "C" แต่ Sendy ยังถือบรรทัดอยู่'),
        _finding('RR0002', 'content', fingerprint='fp2', message='ต่างที่ line:net'),
    ], dataset_label='BSN5657')

    rows = _alerts(db)
    assert len(rows) == 2, f'{len(rows)} alerts, expected one per document'
    by_doc = {r['dedupe_key']: r for r in rows}
    assert set(by_doc) == {'IV6900631', 'RR0002'}
    both = by_doc['IV6900631']
    assert 'LINE_COUNT' in both['message'], both['message']
    assert 'DOCSTAT' in both['message'], (
        'the cancellation was dropped — that is the fact that matters most')
    ctx = json.loads(both['context_json'])
    assert sorted(ctx['drift_kinds']) == ['content', 'source_status']
    assert ctx['docstat'] == 'C'
    # CONTROL: the other document did NOT absorb the first one's facts.
    assert 'DOCSTAT' not in by_doc['RR0002']['message']


def test_acknowledging_holds_until_the_document_moves_again(db):
    f = _finding('IV0001', 'content', fingerprint='aaa')
    sa.record_express_doc_drift_alerts([f], dataset_label='BSN5657')
    rows = _alerts(db)
    assert len(rows) == 1

    # Put acknowledges it.
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE system_alerts SET resolved_at=datetime('now'), resolved_by='put'"
                 " WHERE id=?", (rows[0]['id'],))
    conn.commit(); conn.close()

    # Tomorrow's upload finds the identical disagreement.
    sa.record_express_doc_drift_alerts([f], dataset_label='BSN5657')
    assert len(_alerts(db)) == 1, (
        'the same unchanged disagreement re-alerted after being acknowledged — '
        'that is a daily alert for ever')

    # …but a document that moved AGAIN is news, and must come back.
    sa.record_express_doc_drift_alerts(
        [_finding('IV0001', 'content', fingerprint='bbb')], dataset_label='BSN5657')
    rows = _alerts(db)
    assert len(rows) == 2, 'a changed fingerprint must raise a fresh alert'
    assert rows[1]['resolved_at'] is None
    assert json.loads(rows[1]['context_json'])['fingerprint'] == 'bbb'
