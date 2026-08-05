"""Route-level wiring for the BSN weekly-import hardening (findings 1–3).

The unit-level contracts live in test_bsn_weekly_import_hardening.py; this file
pins what the OPERATOR actually sees and can do on /import-data:

  * a full-history CSV is refused in preview AND on a direct confirm POST, with
    a Thai explanation and a link to the Express ZIP importer;
  * source-line removal is off unless the per-file "complete weekly export"
    checkbox is ticked, and that choice rides the confirm FORM (not the signed
    session, which is ~4KB and already had to be slimmed once);
  * one bad file in a multi-file drop does not sink the good ones.
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

from io import BytesIO                                             # noqa: E402

import pytest                                                      # noqa: E402

from tests.conftest import SALES_SAMPLE_LINES                      # noqa: E402
from tests.test_bsn_weekly_import_hardening import (               # noqa: E402
    _HISTORY_SALES, _PARTIAL_SALES,
)


def _csv(lines):
    return BytesIO(("\n".join(lines) + "\n").encode('cp874'))


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def spy_import_weekly(monkeypatch):
    import models
    calls = []

    def _spy(entries, file_type, filename, apply_removals=True):
        calls.append({'file_type': file_type, 'filename': filename,
                      'apply_removals': apply_removals, 'n': len(entries)})
        return {'imported': len(entries), 'batch_id': None}

    monkeypatch.setattr(models, 'import_weekly', _spy)
    return calls


def _stage(client, files):
    resp = client.post('/import-data', data={'files': files},
                       content_type='multipart/form-data')
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        token = sess['import_stage']['token']
    return resp.data.decode('utf-8'), token


# The page chrome ALREADY links /import-express-dbf twice (sidebar + mobile
# drawer), so a bare `'/import-express-dbf' in body` assert can never fail.
# Scope every assertion to the block panel itself. The panel deliberately
# contains no nested <div>, so the non-greedy match is exact.
_BLOCK_RE = re.compile(r'<div[^>]*data-block="history"[^>]*>(.*?)</div>', re.S)


def _history_block(body):
    m = _BLOCK_RE.search(body)
    assert m, 'no history-block panel was rendered'
    return m.group(1)


# ── finding 1: history blocked, explained, and routed to the ZIP importer ───

def test_preview_shows_thai_block_and_express_zip_link(admin_client):
    body, _ = _stage(admin_client, [(_csv(_HISTORY_SALES), 'ประวัติการขาย.csv')])
    panel = _history_block(body)
    assert 'ประวัติ' in panel, 'panel must explain the rejection in Thai'
    assert '/import-express-dbf' in panel, \
        'the operator must be sent to the Express ZIP importer'


def test_normal_weekly_preview_has_no_history_block(admin_client):
    """Control for the two asserts above: the marker must be absent on a good
    file, so the block assertions are capable of failing."""
    body, _ = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    assert _BLOCK_RE.search(body) is None


def test_direct_confirm_of_history_file_is_refused(admin_client, spy_import_weekly):
    """Bypassing the preview (stale tab / crafted POST) must not import."""
    _, token = _stage(admin_client, [(_csv(_HISTORY_SALES), 'ประวัติการขาย.csv')])
    resp = admin_client.post('/import-data/confirm',
                             data={'token': token, 'type_0': 'sales'})
    body = resp.data.decode('utf-8')
    assert resp.status_code == 200
    assert spy_import_weekly == [], 'history file must never reach import_weekly'
    panel = _history_block(body)
    assert 'ประวัติ' in panel
    assert '/import-express-dbf' in panel


# ── finding 2: removal opt-in rides the form, not the session ──────────────

def test_preview_offers_the_complete_export_checkbox(admin_client):
    body, _ = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    assert 'name="removals_0"' in body, 'per-file removal opt-in must be exposed'


def test_confirm_without_checkbox_does_not_remove(admin_client, spy_import_weekly):
    _, token = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales'})
    assert spy_import_weekly[0]['apply_removals'] is False


def test_confirm_with_checkbox_applies_removals(admin_client, spy_import_weekly):
    _, token = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales', 'removals_0': 'on'})
    assert spy_import_weekly[0]['apply_removals'] is True


def test_staged_session_stays_slim(admin_client):
    """The choice must NOT be persisted into the signed cookie — it is made on
    the preview page and submitted with the confirm form."""
    _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    with admin_client.session_transaction() as sess:
        row = sess['import_stage']['rows'][0]
    # `blocked` and `removals_ok` are the preview's VERDICT and must survive to
    # /confirm (the submitted type cannot be trusted). Both are small scalars —
    # the ~4KB limit that forced this slimming was about per-row diff LISTS.
    assert set(row) == {'idx', 'filename', 'saved', 'detected',
                        'blocked', 'removals_ok'}
    assert all(not isinstance(v, (list, dict)) for v in row.values()), \
        'staged rows must stay scalar — no diff lists back in the signed cookie'


# ── mixed multi-file isolation ─────────────────────────────────────────────

def test_mixed_drop_isolates_bad_files(admin_client, spy_import_weekly):
    body, token = _stage(admin_client, [
        (_csv(SALES_SAMPLE_LINES), 'ขาย_good.csv'),
        (_csv(_HISTORY_SALES), 'ประวัติการขาย.csv'),
        (_csv(_PARTIAL_SALES), 'ขาย_partial.csv'),
    ])
    assert 'ขาย_good.csv' in body and 'ประวัติการขาย.csv' in body

    resp = admin_client.post('/import-data/confirm', data={
        'token': token, 'type_0': 'sales', 'type_1': 'sales', 'type_2': 'sales',
    })
    assert resp.status_code == 200
    assert [c['filename'] for c in spy_import_weekly] == ['ขาย_good.csv'], \
        'only the good file may commit; the other two must be isolated'


# ── the safe default's own failure mode must not itself be silent ──────────
#
# Independent review, 2026-08-03: with removals off by default, source-deleted
# lines now SURVIVE. That is the intended safe behaviour, but it was reported
# only as `removed_skipped: N` inside the raw dict repr of {{ r.summary }} —
# the least prominent text on the results page. A voided invoice line would
# silently over-state stock, week after week, with no alert.

@pytest.fixture
def spy_with_skipped_removals(monkeypatch):
    import models
    monkeypatch.setattr(models, 'import_weekly',
                        lambda entries, kind, fn, apply_removals=True: {
                            'imported': len(entries), 'batch_id': None,
                            'removed': 0, 'removed_skipped': 0 if apply_removals else 3})
    return None


def test_skipped_removals_get_their_own_warning(admin_client, spy_with_skipped_removals):
    _, token = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    res = admin_client.post('/import-data/confirm',
                            data={'token': token, 'type_0': 'sales'}).data.decode('utf-8')
    assert 'data-warn="skipped-removals"' in res, \
        'lines left un-reversed must be flagged, not buried in the summary dict'
    assert '3' in re.search(r'data-warn="skipped-removals"[^>]*>(.*?)</div>',
                            res, re.S).group(1), 'the warning must state how many'


def test_no_warning_when_the_operator_opted_in(admin_client, spy_with_skipped_removals):
    """Control: ticking the box reverses them, so there is nothing to warn about
    — without this the assertion above could pass on an always-on banner."""
    _, token = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    res = admin_client.post('/import-data/confirm',
                            data={'token': token, 'type_0': 'sales',
                                  'removals_0': 'on'}).data.decode('utf-8')
    assert 'data-warn="skipped-removals"' not in res


# ── affordance gaps found by the independent review ────────────────────────

# A file whose TITLE the detector cannot classify, but which is otherwise a
# perfectly good weekly: it carries the "วันที่ :" + "วันที่จาก" pair the history
# guard requires, plus a real party/product/transaction grouping. Without the
# date header the guard refuses it outright (correctly), which would exercise
# the wrong thing here — this test is about the operator classifying a file by
# hand, not about the header guard.
_UNKNOWN_TITLE_WEEKLY = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                       หน้า   :        1"',
    '"  รายงานสรุปพิเศษของทางร้าน"',
    '"รหัสลูกค้า        ถึง  Zหน้าร้าน       วันที่ : 15/04/69"',
    '"วันที่จาก   12\xa0เม.ย.\xa02569    ถึง  31\xa0ธ.ค.\xa02569"',
    '"--------------------------------------------------------"',
    '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
    '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
    '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
]


# NOTE: two tests here previously asserted that an `unknown` row offers the
# removal opt-in. Codex rejected that as unsafe on b7d2934 — unknown files skip
# preview_file() entirely, so enabling deletions there reverses lines the
# operator never saw. Replaced by test_unknown_row_does_not_offer_removals and
# test_crafted_removals_on_an_unpreviewed_row_is_ignored below. The affordance
# gap they covered is a UX nicety and stays open by design.


def test_confirming_a_blocked_row_as_is_shows_the_history_reason(admin_client):
    """A blocked row used to pre-select "ข้ามไฟล์นี้", so confirming without
    touching the dropdown hit the unknown-type early-continue and reported
    "ข้าม — ไม่ได้เลือกประเภท" for a file whose type WAS detected. That reads as
    "you forgot to choose" and invites a pointless re-upload."""
    body, token = _stage(admin_client, [(_csv(_HISTORY_SALES), 'ประวัติการขาย.csv')])
    # Submit exactly what the form has pre-selected, i.e. no operator edit.
    # Assert there is EXACTLY ONE selected option first: taking the first match
    # silently picked the wrong value when two options were both marked
    # selected, which made this test unable to fail.
    sel = re.search(r'<select name="type_0".*?</select>', body, re.S).group(0)
    chosen = re.findall(r'<option value="([^"]+)"[^>]*\bselected\b', sel)
    assert len(chosen) == 1, f'exactly one option must be preselected, got {chosen}'
    res = admin_client.post('/import-data/confirm',
                            data={'token': token, 'type_0': chosen[0]})
    body2 = res.data.decode('utf-8')
    assert 'ไม่ได้เลือกประเภท' not in body2, \
        'a detected-then-blocked file must not report as "no type chosen"'
    panel = _history_block(body2)
    assert 'ประวัติ' in panel and '/import-express-dbf' in panel


# ── the staged row's verdict must survive the dropdown ─────────────────────
#
# Codex on b7d2934: unified_import_confirm() trusts the submitted type_N, and
# commit_file() only runs the history guard for sales/purchase. So a file the
# PREVIEW refused could be re-routed to another importer and accepted. Verified:
# the history fixture submitted as payments_in reached models.import_payments().
#
# The guard cannot simply run for every type — a legitimate การรับชำระหนี้ export
# carries a wide "วันที่จาก" range and would be blocked as history. The verdict
# has to be remembered per staged row instead.

@pytest.fixture
def spy_all_importers(monkeypatch):
    """Record ANY importer the confirm loop might dispatch to."""
    import models, import_express
    import import_credit_notes as icn
    calls = []
    monkeypatch.setattr(models, 'import_weekly',
                        lambda e, k, f, apply_removals=True: calls.append('import_weekly') or {})
    monkeypatch.setattr(models, 'import_payments',
                        lambda p: calls.append('import_payments') or {})
    monkeypatch.setattr(icn, 'import_credit_notes',
                        lambda p, db_path=None: calls.append('import_credit_notes') or {})
    monkeypatch.setattr(import_express, 'run_import',
                        lambda ft, p, **kw: calls.append(f'express:{ft}'))
    return calls


@pytest.mark.parametrize('override', [
    'payments_in', 'payments_out', 'credit_notes_ar', 'credit_notes_ap',
    'ar_snapshot', 'ap_snapshot', 'purchase',
])
def test_blocked_row_stays_blocked_under_any_type_override(
        admin_client, spy_all_importers, override):
    _, token = _stage(admin_client, [(_csv(_HISTORY_SALES), 'ประวัติการขาย.csv')])
    res = admin_client.post('/import-data/confirm',
                            data={'token': token, 'type_0': override})
    assert res.status_code == 200
    assert spy_all_importers == [], \
        f'blocked row reached an importer when re-typed as {override}'
    assert _history_block(res.data.decode('utf-8'))


def test_blocked_row_refusal_survives_a_stale_session_row(admin_client, spy_all_importers):
    """Defence in depth: the refusal must come from the STAGED verdict, not from
    re-detecting at confirm time."""
    _, token = _stage(admin_client, [(_csv(_HISTORY_SALES), 'ประวัติการขาย.csv')])
    with admin_client.session_transaction() as sess:
        assert sess['import_stage']['rows'][0].get('blocked'), \
            'the preview verdict must be persisted on the staged row'


# ── removals require a successful typed preview ────────────────────────────
#
# Unknown files skip preview_file() entirely (bsn.py: `if rtype != 'unknown'`),
# so they have no parsed count and no removal plan. Offering the checkbox there
# let an operator classify AND request source-line deletion in one request,
# reversing lines they never saw. Reverted, and enforced server-side so a stale
# or crafted removals_N cannot re-open it.

def test_unknown_row_does_not_offer_removals(admin_client):
    body, _ = _stage(admin_client, [(_csv(_UNKNOWN_TITLE_WEEKLY), 'ไม่รู้จัก.csv')])
    assert 'ตรวจไม่พบชนิด' in body, 'fixture must detect as unknown'
    assert 'name="removals_0"' not in body, \
        'no removal opt-in without a typed preview showing the deletion count'


def test_crafted_removals_on_an_unpreviewed_row_is_ignored(admin_client, spy_import_weekly):
    """The checkbox is gone from the DOM, so this can only arrive from a stale
    tab or a hand-built POST. It must not take effect."""
    _, token = _stage(admin_client, [(_csv(_UNKNOWN_TITLE_WEEKLY), 'ไม่รู้จัก.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales', 'removals_0': 'on'})
    assert spy_import_weekly, 'the file itself should still import'
    assert spy_import_weekly[0]['apply_removals'] is False, \
        'removals must not apply to a row that was never previewed as sales'


def test_removals_ignored_when_the_type_changed_after_preview(admin_client, spy_import_weekly):
    """A sales preview's removal plan is meaningless if the operator switches the
    row to purchase — it was computed against sales_transactions.

    Asserted as a property rather than a call signature: in practice the parser
    refuses a ขาย file read with the ซื้อ pattern before import_weekly is reached,
    so the importer may legitimately not be called at all. What must never happen
    is removals being APPLIED after a type switch."""
    _, token = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'purchase', 'removals_0': 'on'})
    assert all(c['apply_removals'] is False for c in spy_import_weekly), \
        f'removals applied after a type switch: {spy_import_weekly}'


def test_detected_sales_removals_still_work(admin_client, spy_import_weekly):
    """Control: the supported path is unchanged."""
    _, token = _stage(admin_client, [(_csv(SALES_SAMPLE_LINES), 'ขาย_x.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales', 'removals_0': 'on'})
    assert spy_import_weekly[0]['apply_removals'] is True


@pytest.mark.parametrize('classified_as', [
    'payments_in', 'payments_out', 'credit_notes_ar', 'credit_notes_ap',
    'ar_snapshot', 'ap_snapshot',
])
def test_unknown_classified_as_a_non_weekly_type_never_applies_removals(
        admin_client, spy_all_importers, classified_as):
    """Completing the review's required matrix. `removals_ok` is False for an
    unpreviewed row, so no classification can turn removals on — but the gate
    being structurally impossible is not the same as it being pinned."""
    _, token = _stage(admin_client, [(_csv(_UNKNOWN_TITLE_WEEKLY), 'ไม่รู้จัก.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': classified_as,
                            'removals_0': 'on'})
    assert 'import_weekly' not in spy_all_importers, \
        'a non-weekly classification must not reach the weekly importer'
