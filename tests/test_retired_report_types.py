"""A ลูกหนี้/เจ้าหนี้คงค้าง text report must be REFUSED, never re-routed.

F8 removed the text-report AR/AP importers (Put, 2026-08-22). `detect_express_report`
still recognises those files — deliberately, so the operator can be told what
happened — and that is exactly what creates the trap this file guards:

  the type <select> is built from `_REPORT_LABELS`, and each option carries
  `{% if key == row.detected %}selected{% endif %}`. Drop the type from the
  labels while the detector still emits it and NOTHING matches, so the browser
  submits the FIRST option — `sales`. An unchanged confirm would then hand a
  ลูกหนี้คงค้าง report to the sales importer, which writes sales_transactions
  and moves stock. (Codex review, 2026-08-22.)

So the type stays selectable, the preview marks the row `blocked`, and the block
is enforced SERVER-side off the session — the dropdown is operator-supplied and
cannot be trusted to still say `ar_snapshot` by the time confirm runs.
"""
import io

import pytest

import import_router


AR_HEADER = "บริษัท บุญสวัสดิ์ นำชัย จำกัด\nรายงานลูกหนี้คงค้าง\nณ วันที่ 31/07/2569\n\n"
AP_HEADER = "บริษัท บุญสวัสดิ์ นำชัย จำกัด\nรายงานเจ้าหนี้คงค้าง\nณ วันที่ 31/07/2569\n\n"
RCV_HEADER = "บริษัท บุญสวัสดิ์ นำชัย จำกัด\nรายงานการรับชำระหนี้\nณ วันที่ 31/07/2569\n\n"


def _client(tmp_path):
    from app import app as flask_app
    flask_app.config['UPLOAD_FOLDER'] = str(tmp_path)
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess.update(role='admin', username='admin', user_id=1)
    return c


def _stage(client, text, filename='report.txt'):
    r = client.post('/import-data',
                    data={'files': (io.BytesIO(text.encode('cp874')), filename)},
                    content_type='multipart/form-data')
    assert r.status_code == 200, r.status_code
    with client.session_transaction() as s:
        stage = s['import_stage']
    return stage['token'], stage['rows']


# ── the detector must keep recognising them ─────────────────────────────────

@pytest.mark.parametrize('text,expected', [(AR_HEADER, 'ar_snapshot'),
                                           (AP_HEADER, 'ap_snapshot')])
def test_the_detector_still_recognises_them(tmp_path, text, expected):
    """CONTROL for everything below. Had detection been switched to 'unknown'
    instead, every other test here would still pass while the operator silently
    lost the explanation — recognition is what makes the refusal explainable."""
    p = tmp_path / 'x.txt'
    p.write_bytes(text.encode('cp874'))
    assert import_router.detect_express_report(str(p)) == expected


def test_the_retired_set_and_its_reason_live_in_one_place():
    assert import_router.RETIRED_REPORT_TYPES == {'ar_snapshot', 'ap_snapshot'}
    assert 'zip' in import_router.RETIRED_REPORT_REASON


def test_the_types_are_still_offered_in_the_dropdown():
    """Not cosmetic: an <option> that does not exist cannot be `selected`, and
    the browser then submits the first one instead."""
    from blueprints import bsn
    assert {'ar_snapshot', 'ap_snapshot'} <= set(bsn._REPORT_LABELS)


@pytest.mark.parametrize('rtype', ['ar_snapshot', 'ap_snapshot'])
def test_neither_can_be_previewed_or_committed_directly(tmp_path, rtype):
    p = tmp_path / 'x.txt'
    p.write_bytes(AR_HEADER.encode('cp874'))
    for fn in (import_router.preview_file, import_router.commit_file):
        with pytest.raises(ValueError):
            fn(str(p), rtype)


# ── the upload flow ─────────────────────────────────────────────────────────

def test_preview_blocks_the_row(tmp_db, tmp_path):
    c = _client(tmp_path)
    _token, rows = _stage(c, AR_HEADER)

    assert len(rows) == 1
    assert rows[0]['detected'] == 'ar_snapshot'
    assert rows[0]['blocked'] == 'retired'


def test_a_surviving_type_is_not_blocked(tmp_db, tmp_path):
    """CONTROL — without it, blocking EVERYTHING would pass the test above."""
    c = _client(tmp_path)
    _token, rows = _stage(c, RCV_HEADER, 'rcv.txt')

    assert rows[0]['detected'] == 'payments_in'
    assert rows[0]['blocked'] is None


def test_confirming_it_as_SALES_never_reaches_the_dispatcher(tmp_db, tmp_path, monkeypatch):
    """THE attack this file exists for. Spying the dispatcher rather than reading
    the page: a rendered error could equally mean the import was attempted and
    merely failed, which is a different and much worse outcome."""
    c = _client(tmp_path)
    token, _ = _stage(c, AR_HEADER)
    calls = []
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **kw: calls.append(a) or {'ok': True, 'summary': {}})

    r = c.post('/import-data/confirm', data={'token': token, 'type_0': 'sales'})

    assert r.status_code == 200
    assert calls == []
    assert 'zip' in r.get_data(as_text=True)      # the real reason reached the page


def test_the_spy_does_fire_for_a_type_that_is_still_allowed(tmp_db, tmp_path, monkeypatch):
    """CONTROL for the spy — a spy that never fires looks identical to a refusal
    that works."""
    c = _client(tmp_path)
    token, _ = _stage(c, RCV_HEADER, 'rcv.txt')
    calls = []
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **kw: calls.append(a) or {'ok': True, 'summary': {}})

    c.post('/import-data/confirm', data={'token': token, 'type_0': 'payments_in'})

    assert len(calls) == 1
