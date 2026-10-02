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


@pytest.fixture(autouse=True)
def _upload_folder_restored(monkeypatch):
    """`_client` points the shared app's UPLOAD_FOLDER at this test's tmp_path.
    Restore it at teardown, or every later test in the process stages its
    uploads into a dead test's directory and recreates it after cleanup (#714)."""
    from app import app as flask_app
    monkeypatch.setitem(flask_app.config, 'UPLOAD_FOLDER',
                        flask_app.config['UPLOAD_FOLDER'])


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
    """That one place is now `report_types` (card 10, 2026-09-07).

    `import_router.RETIRED_REPORT_TYPES` is kept as a derived alias, so the
    first assertion still pins the set the rest of this file relies on. The
    reason became PER TYPE rather than one shared module constant — checking
    each one separately is the point: a shared constant would report ar's
    reason for ap without anything noticing.
    """
    import report_types

    assert import_router.RETIRED_REPORT_TYPES == {'ar_snapshot', 'ap_snapshot',
                                                  'sales', 'purchase'}
    assert import_router.RETIRED_REPORT_TYPES == report_types.retired_keys(), \
        'the alias drifted from the registry it is supposed to derive from'

    for key in ('ar_snapshot', 'ap_snapshot', 'sales', 'purchase'):
        reason = report_types.retired_reason_for(key)
        assert reason and 'zip' in reason, \
            f'{key} has no reason pointing the operator at the daily zip'

    # A live type must NOT carry one — otherwise "is it retired?" stops meaning
    # anything and every row could be blocked with an explanation.
    assert report_types.retired_reason_for('payments_in') is None
    assert report_types.retired_reason_for('nonsense-key') is None


def test_the_types_are_still_offered_in_the_dropdown():
    """Not cosmetic: an <option> that does not exist cannot be `selected`, and
    the browser then submits the first one instead."""
    from blueprints import bsn
    assert {'ar_snapshot', 'ap_snapshot', 'sales', 'purchase'} <= set(bsn._REPORT_LABELS)


@pytest.mark.parametrize('rtype', ['ar_snapshot', 'ap_snapshot', 'sales', 'purchase'])
def test_none_can_be_previewed_or_committed_directly(tmp_path, rtype):
    p = tmp_path / 'x.txt'
    p.write_bytes(AR_HEADER.encode('cp874'))
    with pytest.raises(import_router.RetiredReportType):
        import_router.preview_file(str(p), rtype)
    with pytest.raises(import_router.RetiredReportType):
        import_router.commit_file(str(p), rtype, apply_removals=False)
    # RetiredReportType IS a ValueError: callers that caught the old error
    # for AR/AP keep working.
    assert issubclass(import_router.RetiredReportType, ValueError)


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


# ── ขาย / ซื้อ (Card E, ADR 0020): one field, three layers ──────────────────

SALES_HEADER = ("บริษัท บุญสวัสดิ์ นำชัย จำกัด\nรายงานการขาย\n"
                "ณ วันที่ 31/07/2569\n\n")


def test_retired_reason_is_the_single_switch():
    """The suffix and the removal capability are DERIVED from `retired_reason`,
    not declared beside it. Break it once: with the field cleared the type is
    plain 'ขาย' and offered removals again (test_bsn_line etc. run in that
    state under `unretired_text_door`)."""
    import report_types

    labels = report_types.labels()
    assert labels['sales'] == 'ขาย — ปิดแล้ว ใช้ zip รายวัน'
    assert labels['purchase'] == 'ซื้อ — ปิดแล้ว ใช้ zip รายวัน'
    assert labels['ar_snapshot'] == 'ลูกหนี้คงค้าง — ปิดแล้ว ใช้ zip รายวัน'
    assert labels['payments_in'] == 'การรับชำระหนี้ (ลูกหนี้)'      # control: live, no suffix
    assert report_types.removal_capable_keys() == {'payments_in'}


def test_the_unretire_fixture_reopens_every_layer(unretired_text_door):
    import report_types

    assert report_types.retired_keys() == {'ar_snapshot', 'ap_snapshot'}
    assert report_types.labels()['sales'] == 'ขาย'
    assert report_types.removal_capable_keys() == {'sales', 'purchase', 'payments_in'}


def test_the_dispatcher_refuses_a_retired_type_before_parsing(tmp_path):
    """A file that would fail to PARSE is refused with the retirement reason,
    not the parse error: the guard sits above every read of the file."""
    p = tmp_path / 'garbage.txt'
    p.write_bytes(b'\xff\xfe not a report at all')
    for fn, kw in ((import_router.preview_file, {}),
                   (import_router.commit_file, {'apply_removals': False})):
        with pytest.raises(import_router.RetiredReportType) as exc:
            fn(str(p), 'sales', **kw)
        assert 'zip' in str(exc.value)


def test_the_dispatcher_guard_reads_the_registry_at_call_time(tmp_path, unretired_text_door):
    """CONTROL: with the door reopened the same call reaches the parser (and
    fails THERE), so the refusal above is the guard and not a bad file."""
    p = tmp_path / 'garbage.txt'
    p.write_bytes(b'\xff\xfe not a report at all')
    with pytest.raises(Exception) as exc:
        import_router.preview_file(str(p), 'sales')
    assert not isinstance(exc.value, import_router.RetiredReportType)


def test_a_sales_file_is_blocked_as_retired_at_preview(tmp_db, tmp_path):
    c = _client(tmp_path)
    _token, rows = _stage(c, SALES_HEADER, 'sales.txt')

    assert rows[0]['detected'] == 'sales'
    assert rows[0]['blocked'] == 'retired'


@pytest.mark.parametrize('submitted', ['sales', 'purchase', 'payments_in',
                                       'credit_notes_ar', 'unknown', 'ar_snapshot'])
def test_no_dropdown_value_reaches_an_importer_for_a_retired_row(
        tmp_db, tmp_path, monkeypatch, submitted):
    """Spy over EVERY dropdown value for a row the preview blocked as retired:
    neither the dispatcher nor the model importers fire."""
    import models
    c = _client(tmp_path)
    token, _ = _stage(c, SALES_HEADER, 'sales.txt')
    calls = []
    for name in ('import_weekly', 'import_payments'):
        monkeypatch.setattr(models, name,
                            lambda *a, _n=name, **kw: calls.append(_n) or {})
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **kw: calls.append('commit_file') or {'ok': True})

    r = c.post('/import-data/confirm', data={'token': token, 'type_0': submitted})

    assert r.status_code == 200
    assert calls == []
    assert 'zip' in r.get_data(as_text=True)


@pytest.mark.parametrize('retired', ['sales', 'purchase'])
def test_an_unknown_file_overridden_to_a_retired_type_is_refused_at_confirm(
        tmp_db, tmp_path, monkeypatch, retired):
    """The bypass the preview block cannot see: an `unknown` file (no block was
    set) that the operator overrides to ขาย in the dropdown."""
    import models
    c = _client(tmp_path)
    token, rows = _stage(c, "ไฟล์อะไรก็ไม่รู้\nไม่มีหัวรายงาน\n", 'mystery.txt')
    assert rows[0]['detected'] == 'unknown' and not rows[0].get('blocked')
    calls = []
    monkeypatch.setattr(models, 'import_weekly',
                        lambda *a, **kw: calls.append('import_weekly') or {})
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **kw: calls.append('commit_file') or {'ok': True})

    r = c.post('/import-data/confirm', data={'token': token, 'type_0': retired})

    assert calls == []
    assert 'zip' in r.get_data(as_text=True)


def test_an_unknown_file_overridden_to_a_live_type_still_reaches_the_importer(
        tmp_db, tmp_path, monkeypatch):
    """CONTROL for the test above: same unknown file, live type -> the spy fires."""
    c = _client(tmp_path)
    token, _ = _stage(c, "ไฟล์อะไรก็ไม่รู้\nไม่มีหัวรายงาน\n", 'mystery.txt')
    calls = []
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **kw: calls.append(a) or {'ok': True, 'summary': {}})

    c.post('/import-data/confirm', data={'token': token, 'type_0': 'payments_in'})

    assert len(calls) == 1


def test_a_retired_row_shows_the_panel_and_no_ticks(tmp_db, tmp_path):
    from lxml import html as lh
    c = _client(tmp_path)
    r = c.post('/import-data',
               data={'files': (io.BytesIO(SALES_HEADER.encode('cp874')), 'sales.txt')},
               content_type='multipart/form-data')
    doc = lh.fromstring(r.get_data(as_text=True))

    panels = doc.xpath('//*[@data-block="retired"]')
    assert len(panels) == 1
    assert 'zip' in panels[0].text_content()
    assert panels[0].xpath('.//a[contains(@href, "import-express-dbf")]')
    assert doc.xpath('//input[starts-with(@name, "removals_")]') == []
    assert 'อ่านไม่ได้' not in doc.xpath('//table')[0].text_content()


def test_a_live_row_gets_no_retired_panel(tmp_db, tmp_path):
    """CONTROL for the panel test: payments_in previews normally."""
    from lxml import html as lh
    c = _client(tmp_path)
    r = c.post('/import-data',
               data={'files': (io.BytesIO(RCV_HEADER.encode('cp874')), 'rcv.txt')},
               content_type='multipart/form-data')
    doc = lh.fromstring(r.get_data(as_text=True))
    assert doc.xpath('//*[@data-block="retired"]') == []
