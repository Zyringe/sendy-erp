"""/import-data refuses a weekly text export older than one already imported (#648).

The vector this closes, measured on a copy of prod 2026-09-23/25 and pinned as an
executable fact by `test_full_history_import_window.py`: re-uploading an ARCHIVED
weekly is not a history export. `parse_weekly.is_history_export` measures a file's
filter start against THAT FILE'S OWN report date, so a July weekly with a 7-day
reach-back is accepted no matter how long ago July was — and that file carries
HP6900041, whose `product_id` was hand-re-pointed by
`scripts/2026_09_19_split_belco_582.py`. A re-upload replaces the row and drops
pid 1305's cost from ฿24.50 to ฿7.00.

The DBF route has guarded this since 2026-08-18 (`_claim_export_date` +
`force_older`); the text path had no counterpart. It does now, per report:
`BSN:weekly:sales` and `BSN:weekly:purchase` are separate marks because the two
reports are exported independently, so one may legitimately be older.

Two things worth knowing before editing this file:

  * "nothing was imported" is asserted at the `models.import_weekly` boundary
    (`spy_import_weekly`, the idiom of tests/test_bsn_weekly_import_hardening.py)
    AND by the watermark row not moving. A 302/200 from a Sendy route proves
    nothing — these routes render the same page on success, on a caught exception
    and on a refusal.
  * the snapshot-derived fallback inside `_claim_export_date` is meaningful only
    for `entity='BSN'`. The seeded DB holds `express_ar_outstanding` up to
    2026-09-23, so without the entity gate a brand-new weekly mark would start
    life refusing every file older than that — including a perfectly current
    weekly. `test_first_weekly_is_accepted_although_a_newer_ar_snapshot_exists`
    pins it against that real value, and
    `test_bsn_entity_keeps_its_snapshot_derived_fallback` pins that the DBF
    route's own behaviour did not change.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

from io import BytesIO                                             # noqa: E402

import pytest                                                      # noqa: E402

from tests.conftest import (                                       # noqa: E402
    SALES_SAMPLE_LINES, PURCHASE_SAMPLE_LINES,
)

SALES_ENTITY = 'BSN:weekly:sales'
PURCHASE_ENTITY = 'BSN:weekly:purchase'

# The real value in the seeded prod snapshot — the number the §1 fallback gate
# exists to stop leaking into a weekly mark. Asserted live below, not trusted.
SEEDED_AR_SNAPSHOT = '2026-09-23'


# ── fixture files ───────────────────────────────────────────────────────────
#
# Only the two header lines matter here, and they are load-bearing (conftest says
# so): `date_filter_is_readable` refuses a file whose range cannot be read, and
# `is_history_export` measures the reach-back between exactly these two dates.
# Every file below keeps the reach-back small, so each one is a WEEKLY and the
# history gate lets it through — that is the whole point of the vector.

def _redate(lines, report, filter_start):
    out = list(lines)
    prefix = out[2].split('วันที่ :')[0]
    out[2] = f'{prefix}วันที่ : {report}"'
    out[3] = f'"วันที่จาก   {filter_start}         ถึง  31\xa0ธ.ค.\xa02569"'
    return out


def _sales(report='15/04/69', filter_start='12\xa0เม.ย.\xa02569'):
    return _redate(SALES_SAMPLE_LINES, report, filter_start)


def _purchase(report='24/04/69', filter_start='23\xa0เม.ย.\xa02569'):
    return _redate(PURCHASE_SAMPLE_LINES, report, filter_start)


# The archived July weekly from the #648 report (report 20/07/69, filter start
# 13 ก.ค. 2569 — a 7-day reach-back), and a current one.
ARCHIVED_SALES = _sales('20/07/69', '13\xa0ก.ค.\xa02569')
CURRENT_SALES = _sales('25/09/69', '20\xa0ก.ย.\xa02569')
ARCHIVED_PURCHASE = _purchase('20/07/69', '13\xa0ก.ค.\xa02569')
CURRENT_PURCHASE = _purchase('25/09/69', '20\xa0ก.ย.\xa02569')


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
    """Record every models.import_weekly call without touching the ledger."""
    import models
    calls = []

    def _spy(entries, file_type, filename, apply_removals=True):
        calls.append({'file_type': file_type, 'filename': filename,
                      'apply_removals': apply_removals, 'n': len(entries)})
        return {'imported': len(entries), 'batch_id': None}

    monkeypatch.setattr(models, 'import_weekly', _spy)
    return calls


def _stage(client, files):
    """Upload → preview. Returns (rendered page, confirm token)."""
    resp = client.post('/import-data', data={'files': files},
                       content_type='multipart/form-data')
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        token = sess['import_stage']['token']
    return resp.data.decode('utf-8'), token


def _mark(db, entity):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            'SELECT last_export_date FROM express_import_watermark WHERE entity = ?',
            (entity,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _set_mark(db, entity, date_iso):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            'INSERT INTO express_import_watermark '
            '(entity, last_export_date, last_export_at, updated_at) '
            "VALUES (?, ?, ?, datetime('now')) "
            'ON CONFLICT(entity) DO UPDATE SET last_export_date = excluded.last_export_date, '
            ' last_export_at = excluded.last_export_at',
            (entity, date_iso, date_iso + 'T00:00:00'))
        conn.commit()
    finally:
        conn.close()


def _forced_audit_rows(db):
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT changed_fields FROM audit_log "
            "WHERE table_name = 'express_import_watermark' "
            "  AND changed_fields LIKE '%forced_older_authorized%'").fetchall()]
    finally:
        conn.close()


# ── §2: the public report-date accessor ─────────────────────────────────────

def test_export_report_date_reads_the_header(tmp_path):
    import datetime

    import parse_weekly
    p = tmp_path / 'ขาย_x.csv'
    p.write_text("\n".join(ARCHIVED_SALES) + "\n", encoding='cp874')
    assert parse_weekly.export_report_date(str(p)) == datetime.date(2026, 7, 20)


def test_export_report_date_is_none_when_the_header_has_no_report_date(tmp_path):
    import parse_weekly
    p = tmp_path / 'ขาย_no_report_date.csv'
    p.write_text('"ไม่มีหัวรายงาน"\n', encoding='cp874')
    assert parse_weekly.export_report_date(str(p)) is None


# ── §3/§4: an archived weekly is refused, and the tick is the only way past ──

def test_archived_weekly_is_refused_at_confirm_without_the_tick(
        admin_client, tmp_db, spy_import_weekly):
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')
    _, token = _stage(admin_client, [(_csv(ARCHIVED_SALES), 'ขาย_archived.csv')])

    resp = admin_client.post('/import-data/confirm',
                             data={'token': token, 'type_0': 'sales'})
    body = resp.data.decode('utf-8')

    assert spy_import_weekly == [], 'a stale file must never reach import_weekly'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25', \
        'a refused file must not move the mark'
    assert '2026-07-20' in body and '2026-09-25' in body, \
        'the operator must be told BOTH dates'


def test_archived_weekly_imports_when_the_operator_ticks_the_box(
        admin_client, tmp_db, spy_import_weekly):
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')
    _, token = _stage(admin_client, [(_csv(ARCHIVED_SALES), 'ขาย_archived.csv')])

    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales',
                            'force_older_0': 'on'})

    assert [c['file_type'] for c in spy_import_weekly] == ['sales'], \
        'the tick must let the import through'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25', \
        'a forced OLDER import must not drag the mark backwards'
    assert _forced_audit_rows(tmp_db), \
        "the operator's override must be recorded, as the DBF route records it"


def test_a_current_weekly_advances_its_own_watermark(
        admin_client, tmp_db, spy_import_weekly):
    assert _mark(tmp_db, SALES_ENTITY) is None, 'precondition: no sales mark yet'
    _, token = _stage(admin_client, [(_csv(CURRENT_SALES), 'ขาย_current.csv')])

    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales'})

    assert len(spy_import_weekly) == 1
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25'


def test_re_uploading_the_same_file_is_accepted(
        admin_client, tmp_db, spy_import_weekly):
    """Strictly-older. Re-running the same export is the team's normal recovery
    move when something looked wrong, and must keep working."""
    for _ in range(2):
        _, token = _stage(admin_client, [(_csv(CURRENT_SALES), 'ขาย_current.csv')])
        admin_client.post('/import-data/confirm',
                          data={'token': token, 'type_0': 'sales'})

    assert len(spy_import_weekly) == 2, 'the second upload of the same file was refused'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25'


def test_the_two_weekly_marks_are_independent(
        admin_client, tmp_db, spy_import_weekly):
    """ขาย and ซื้อ are exported independently, so a stale purchase file must not
    block a current sales file (and the purchase mark must stay put)."""
    _set_mark(tmp_db, PURCHASE_ENTITY, '2026-09-25')
    _, token = _stage(admin_client, [
        (_csv(ARCHIVED_PURCHASE), 'ซื้อ_archived.csv'),
        (_csv(CURRENT_SALES), 'ขาย_current.csv'),
    ])

    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'purchase', 'type_1': 'sales'})

    assert [c['file_type'] for c in spy_import_weekly] == ['sales'], \
        'the stale purchase must be refused and the current sales must import'
    assert _mark(tmp_db, PURCHASE_ENTITY) == '2026-09-25'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25'


# ── §1: the snapshot-derived fallback is BSN-only ───────────────────────────

def test_first_weekly_is_accepted_although_a_newer_ar_snapshot_exists(
        admin_client, tmp_db, spy_import_weekly):
    """The §1 gate. `_snapshot_derived_watermark` reads the newest AR/AP SNAPSHOT
    date, which says nothing about weekly text exports. Ungated, a brand-new
    weekly mark starts life refusing everything older than the snapshot."""
    conn = sqlite3.connect(tmp_db)
    try:
        snap = conn.execute(
            "SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding "
            "WHERE entity = 'BSN'").fetchone()[0]
    finally:
        conn.close()
    assert snap == SEEDED_AR_SNAPSHOT, (
        f'the seeded DB no longer holds an AR snapshot at {SEEDED_AR_SNAPSHOT} '
        f'(found {snap}) — this test needs one NEWER than the file below to mean '
        f'anything; re-point it at the real value')
    assert _mark(tmp_db, SALES_ENTITY) is None, 'precondition: no sales mark yet'

    # Dated BEFORE the snapshot, and the very first weekly for this mark.
    _, token = _stage(admin_client, [(_csv(_sales()), 'ขาย_first.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales'})

    assert len(spy_import_weekly) == 1, \
        'the first weekly import was refused by an AR snapshot date'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-04-15'


def test_bsn_entity_keeps_its_snapshot_derived_fallback(tmp_db):
    """Do not regress the DBF route: with no 'BSN' row, the fallback still reads
    the newest snapshot, so a zip older than it is still refused."""
    import datetime

    from blueprints import bsn
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute("DELETE FROM express_import_watermark WHERE entity = 'BSN'")
        conn.commit()
    finally:
        conn.close()

    older = datetime.datetime(2026, 7, 20)
    ok, previous = bsn._claim_export_date(older, advance=False)
    assert previous == SEEDED_AR_SNAPSHOT, \
        "entity='BSN' must still fall back to the snapshot-derived date"
    assert ok is False

    # Control: the same read for a weekly entity sees no fallback at all.
    ok2, previous2 = bsn._claim_export_date(
        older, entity=SALES_ENTITY, advance=False)
    assert (ok2, previous2) == (True, None)


# ── §3 trap: the preview's verdict must survive the session round-trip ──────

def test_stale_over_survives_the_session_round_trip(admin_client, tmp_db):
    """`unified_import` rebuilds `session['import_stage']` from an EXPLICIT key
    list. A verdict missing from that list never reaches /confirm, and the whole
    guard is inert while looking implemented."""
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')
    _stage(admin_client, [(_csv(ARCHIVED_SALES), 'ขาย_archived.csv')])
    with admin_client.session_transaction() as sess:
        rows = sess['import_stage']['rows']
    assert rows[0]['stale_over'] == '2026-09-25'


def test_preview_does_not_mark_a_current_file_stale(admin_client, tmp_db):
    """Control for the test above: `stale_over` must be absent on a good file, so
    these assertions are capable of failing."""
    _set_mark(tmp_db, SALES_ENTITY, '2026-04-15')
    _stage(admin_client, [(_csv(CURRENT_SALES), 'ขาย_current.csv')])
    with admin_client.session_transaction() as sess:
        rows = sess['import_stage']['rows']
    assert not rows[0].get('stale_over')


# ── §5: the checkbox is rendered only for a row the preview marked ──────────

def test_preview_offers_the_force_older_checkbox_for_a_stale_row(
        admin_client, tmp_db):
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')
    body, _ = _stage(admin_client, [(_csv(ARCHIVED_SALES), 'ขาย_archived.csv')])
    assert 'name="force_older_0"' in body
    assert '2026-09-25' in body, 'the label must name what has already been imported'


def test_preview_has_no_force_older_checkbox_for_a_current_row(
        admin_client, tmp_db):
    _set_mark(tmp_db, SALES_ENTITY, '2026-04-15')
    body, _ = _stage(admin_client, [(_csv(CURRENT_SALES), 'ขาย_current.csv')])
    assert 'force_older_0' not in body


# ── §4: a tick the preview did not authorize is ignored, not trusted ────────

def test_force_older_is_ignored_for_a_row_the_preview_did_not_mark_stale(
        admin_client, tmp_db, spy_import_weekly):
    """A `force_older_N` for an unmarked row is a stale tab or a hand-built POST
    — the same reasoning `removals_N` already documents. Reproduced as the real
    race: the preview ran while the mark was behind the file, then the mark moved
    ahead of it (a sibling worker's import) before confirm."""
    _, token = _stage(admin_client, [(_csv(ARCHIVED_SALES), 'ขาย_archived.csv')])
    with admin_client.session_transaction() as sess:
        assert not sess['import_stage']['rows'][0].get('stale_over'), \
            'precondition: the preview must NOT have marked this row'
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')

    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'sales',
                            'force_older_0': 'on'})

    assert spy_import_weekly == [], \
        'an unauthorized tick must not import over a newer file'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25'
    assert not _forced_audit_rows(tmp_db), \
        'nothing was authorized, so nothing may be recorded as authorized'


def test_the_tick_does_not_survive_a_type_change(
        admin_client, tmp_db, spy_import_weekly):
    """The preview marked this row against the ขาย mark. Submitted as ซื้อ, the
    refusal is about a different mark and a different date, so the tick is
    authorizing something the operator was never shown — refuse, as `removals_N`
    does on a type change."""
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')
    _set_mark(tmp_db, PURCHASE_ENTITY, '2026-09-25')
    _, token = _stage(admin_client, [(_csv(ARCHIVED_SALES), 'ขาย_archived.csv')])
    with admin_client.session_transaction() as sess:
        assert sess['import_stage']['rows'][0]['stale_over'] == '2026-09-25', \
            'precondition: the preview must have marked this row as ขาย'

    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'purchase',
                            'force_older_0': 'on'})

    assert spy_import_weekly == []
    assert not _forced_audit_rows(tmp_db)


# ── the guard must not touch the types that do not replace ledger lines ─────

# A การรับชำระหนี้ export, the shape test_backup_routes.py uses. Its date range is
# deliberately WIDE (2567–2569): receipts clear invoices of any age, which is why
# the history gate does not run for this type and why the weekly marks must not
# either.
#
# ⚠ It carries a READABLE report date (2026-07-20, older than the mark the test
# sets) on purpose. Without one, `_weekly_export_stamp` returns nothing for any
# type and the test cannot see a gate added here at all — measured: adding
# payments_in to `_WEEKLY_WATERMARK_ENTITIES` left this test green until the
# "วันที่ :" line was added.
_PAYMENTS_IN = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                หน้า   :        1"',
    '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"',
    '"รหัสลูกค้า        ถึง  Zหน้าร้าน        วันที่ : 20/07/69"',
    '"วันที่จาก   1 ม.ค. 2567  ถึง  31 ธ.ค. 2569"',
    '">>>> จบรายงาน <<<<"',
]


def test_a_payment_report_is_not_gated_by_a_weekly_mark(
        admin_client, tmp_db, monkeypatch):
    """Only ขาย/ซื้อ replace ledger lines, and they are the only two types
    commit_file runs the history gate for. A การรับชำระหนี้ export must keep
    importing whatever the weekly marks say, and must not move them."""
    import models
    calls = []

    def _fake_import_payments(path, **kwargs):
        calls.append(path)
        return {'imported': 1, 'updated': 0, 'skipped': 0}

    monkeypatch.setattr(models, 'import_payments', _fake_import_payments)
    _set_mark(tmp_db, SALES_ENTITY, '2026-09-25')

    _, token = _stage(admin_client, [(_csv(_PAYMENTS_IN), 'การรับชำระหนี้_x.csv')])
    admin_client.post('/import-data/confirm',
                      data={'token': token, 'type_0': 'payments_in'})

    assert calls, 'a payments report must not be gated by the weekly marks'
    assert _mark(tmp_db, SALES_ENTITY) == '2026-09-25', \
        'a non-weekly type must not touch a weekly mark'


def test_only_the_two_ledger_replacing_reports_are_gated():
    """Scope, pinned: adding a type here gates a report whose date range says
    nothing about what it replaces. The end-to-end proof for the types NOT in
    this map is the payments test above."""
    from blueprints import bsn
    assert set(bsn._WEEKLY_WATERMARK_ENTITIES) == {'sales', 'purchase'}
