"""BSN weekly-import hardening — Codex review findings 1–3 (policy A, 2026-08-03).

1. Full-history Express exports must be BLOCKED by the weekly importer, in both
   preview and confirm/commit, with a Thai explanation pointing the operator at
   the separate Express ZIP importer. `parse_weekly.is_history_export()` already
   existed and its unit tests passed, but nothing in production called it.

2. Source-line REMOVAL must default to off. A product-code/salesperson FILTERED
   Express export yields partial invoices whose filtered-out lines look
   "deleted"; reversing them mass-deletes real stock. The operator opts in
   per-file by confirming the file is a complete weekly export.

3. `parse_weekly._parse()` silently dropped transaction lines whose regex or
   numeric conversion failed, so a zero/partial parse was reported as a
   successful import. Rejections now raise with Thai file/line context.

   SR (ใบลดหนี้/return) rows are the one legitimate exemption: they are
   interleaved into the same ขาย report but carry an extra Y/N clearing marker,
   so the transaction regex never matches them, and they are imported by
   `import_credit_notes` instead. Measured over every real Express CSV in
   inventory_app/imports/ on 2026-08-03: 269 of 272 unmatched candidate lines
   were SR docs. (The other 3 were IV lines with a NEGATIVE unit price — a real
   silent data loss this guard now surfaces.)
"""
import os

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

import import_router                      # noqa: E402
import parse_weekly                       # noqa: E402

from tests.conftest import SALES_SAMPLE_LINES, PURCHASE_SAMPLE_LINES  # noqa: E402


# ── fixture files ───────────────────────────────────────────────────────────

def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="cp874")
    return str(p)


# Full-history sales export: title gate + a filter start far before the report
# date (the single-Buddhist-year shape that is_history_export() detects).
_HISTORY_SALES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                       หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า                       ถึง  Zหน้าร้าน                 วันที่ : 20/04/69"',
    '"วันที่จาก   1\xa0มี.ค.\xa02569         ถึง  19\xa0เม.ย.\xa02569"',
    '"-------------------------------------------------------------------------"',
    '"  เกียรติทวีฮาร์ดแวร์\xa0/01ก11"',
    '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
    '"      02/03/69   IV6900100-  1        10.00 ใบ          149.54  2                  1495.40                  1495.40"',
]

_HISTORY_PURCH = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                       หน้า   :        1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"รหัสผู้จำหน่ายจาก  AA01             ถึง  ZZ99                   วันที่ : 29/05/69"',
    '"วันที่จาก           1\xa0ม.ค.\xa02567   ถึง  31\xa0ธ.ค.\xa02569"',
    '"-------------------------------------------------------------------------"',
    '"  ย้งเจริญการพิมพ์\xa0/ย้ง"',
    '"   กล่องในปุ๊ก#7\xa0/Pกล่อง3"',
    '"        24/04/69   HP6900023       22965.00 กล            0.69  0                 15845.85                 15845.85"',
]

# One good line + one line the transaction regex cannot match: a NEGATIVE unit
# price (real shape, seen 3× in the production history export).
_PARTIAL_SALES = SALES_SAMPLE_LINES + [
    '"  ทดสอบราคาติดลบ\xa0/01ล99"',
    '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
    '"      04/04/69   IV6900777-  1         1.00 ใบ          -10.00  1                   -10.00                   -10.00"',
]

# Transaction-looking lines with NO product heading before them → every line is
# skipped and the file parses to ZERO entries while looking like a real import.
_ZERO_PARSE_SALES = SALES_SAMPLE_LINES[:5] + [
    '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
    '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
    '"      04/04/69   IV6900504-  1        12.00 ใบ          160.00  1                  1920.00                  1920.00"',
]

# A normal weekly file that also carries an SR (ใบลดหนี้) return row — the one
# unmatched shape the parser is allowed to skip silently.
_SALES_WITH_SR = SALES_SAMPLE_LINES + [
    '"      30/03/69   SR6900010-  1         1.00 ผน Y          0.00  2                     0.00                     0.00"',
]


@pytest.fixture
def history_sales_csv(tmp_path):
    return _write(tmp_path, "ประวัติการขาย_แยกตามลูกค้า.csv", _HISTORY_SALES)


@pytest.fixture
def history_purch_csv(tmp_path):
    return _write(tmp_path, "ประวัติการซื้อ_แยกตามผู้จำหน่าย.csv", _HISTORY_PURCH)


@pytest.fixture
def partial_sales_csv(tmp_path):
    return _write(tmp_path, "ขาย_partial.csv", _PARTIAL_SALES)


@pytest.fixture
def zero_parse_sales_csv(tmp_path):
    return _write(tmp_path, "ขาย_zero.csv", _ZERO_PARSE_SALES)


@pytest.fixture
def sales_with_sr_csv(tmp_path):
    return _write(tmp_path, "ขาย_with_sr.csv", _SALES_WITH_SR)


@pytest.fixture
def spy_import_weekly(monkeypatch):
    """Record every models.import_weekly call without touching the ledger."""
    import models
    calls = []

    def _spy(entries, file_type, filename, apply_removals=True):
        calls.append({'entries': entries, 'file_type': file_type,
                      'filename': filename, 'apply_removals': apply_removals})
        return {'imported': len(entries), 'batch_id': None}

    monkeypatch.setattr(models, 'import_weekly', _spy)
    return calls


# ── finding 1: full-history blocking ────────────────────────────────────────

def test_preview_blocks_full_history_sales(history_sales_csv):
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router.preview_file(history_sales_csv, 'sales')


def test_preview_blocks_full_history_purchase(history_purch_csv):
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router.preview_file(history_purch_csv, 'purchase')


def test_commit_blocks_full_history_sales(history_sales_csv, spy_import_weekly):
    """A stale or crafted POST that skips preview must still be refused —
    the guard lives inside commit_file, not only in the preview path."""
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router.commit_file(history_sales_csv, 'sales')
    assert spy_import_weekly == [], 'blocked file must never reach import_weekly'


def test_commit_blocks_full_history_purchase(history_purch_csv, spy_import_weekly):
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router.commit_file(history_purch_csv, 'purchase')
    assert spy_import_weekly == []


def test_history_block_message_is_thai_and_names_the_express_zip_route(history_sales_csv):
    with pytest.raises(import_router.HistoryExportBlocked) as exc:
        import_router.preview_file(history_sales_csv, 'sales')
    msg = str(exc.value)
    assert 'ประวัติ' in msg, 'message must explain WHAT was rejected, in Thai'
    assert 'Express' in msg and 'zip' in msg.lower(), \
        'message must point the operator at the Express ZIP importer'


def test_normal_weekly_file_is_not_blocked(sample_sales_file, spy_import_weekly):
    out = import_router.commit_file(sample_sales_file, 'sales', filename='ขาย_x.csv')
    assert out['ok'] is True
    assert len(spy_import_weekly) == 1


# ── finding 2: removal default + explicit opt-in ────────────────────────────

def test_commit_defaults_to_no_source_line_removals(sample_sales_file, spy_import_weekly):
    """Destructive default is the bug: a FILTERED export's missing lines must
    NOT be reversed unless the operator says the export is complete."""
    import_router.commit_file(sample_sales_file, 'sales', filename='ขาย_x.csv')
    assert spy_import_weekly[0]['apply_removals'] is False


def test_commit_propagates_explicit_complete_export_optin(sample_sales_file, spy_import_weekly):
    import_router.commit_file(sample_sales_file, 'sales', filename='ขาย_x.csv',
                              apply_removals=True)
    assert spy_import_weekly[0]['apply_removals'] is True


def test_commit_purchase_defaults_to_no_removals(sample_purchase_file, spy_import_weekly):
    import_router.commit_file(sample_purchase_file, 'purchase', filename='ซื้อ_x.csv')
    assert spy_import_weekly[0]['apply_removals'] is False


# ── finding 3: zero parse / partial parse ──────────────────────────────────

def test_zero_parse_with_transaction_candidates_raises(zero_parse_sales_csv):
    """Transaction-looking lines present but nothing parsed = a silent no-op
    import today. It must fail loudly instead."""
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(zero_parse_sales_csv)
    assert 'ขาย_zero.csv' in str(exc.value), 'error must name the offending file'


def test_partial_parse_raises_with_line_context(partial_sales_csv):
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(partial_sales_csv)
    msg = str(exc.value)
    assert 'ขาย_partial.csv' in msg
    assert 'บรรทัด' in msg, 'error must give Thai line context'
    assert 'IV6900777' in msg, 'error must quote the rejected line'


def test_valid_weekly_file_still_parses(sample_sales_file, sample_purchase_file):
    """Headers, party/product headings, separators and blank lines must stay
    legitimate — the guard must not fire on a good file."""
    assert len(parse_weekly.parse_sales(sample_sales_file)) == 6
    assert len(parse_weekly.parse_purchases(sample_purchase_file)) == 2


def test_sr_credit_note_rows_are_skipped_not_rejected(sales_with_sr_csv):
    """SR rows belong to the ใบลดหนี้ importer; the weekly parser skips them by
    design, so they must not be treated as parse failures."""
    entries = parse_weekly.parse_sales(sales_with_sr_csv)
    assert len(entries) == 6
    assert all(not e['doc_no'].startswith('SR') for e in entries)


def test_preview_and_commit_reject_a_bad_file_identically(partial_sales_csv, spy_import_weekly):
    """Parity: the same file must fail the same way on both paths, and a direct
    commit must not be able to bypass the check."""
    with pytest.raises(ValueError) as prev_exc:
        import_router.preview_file(partial_sales_csv, 'sales')
    with pytest.raises(ValueError) as commit_exc:
        import_router.commit_file(partial_sales_csv, 'sales', filename='ขาย_partial.csv')
    assert str(prev_exc.value) == str(commit_exc.value)
    assert spy_import_weekly == [], 'a rejected file must never reach import_weekly'


def test_a_week_of_only_sr_returns_is_not_a_zero_parse(tmp_path):
    """A quiet week whose only rows are ใบลดหนี้ returns parses to zero entries
    LEGITIMATELY — every SR row belongs to import_credit_notes. Counting SR
    lines as transaction candidates would make the zero-parse rule fire on a
    perfectly good file and block the week's import."""
    only_sr = SALES_SAMPLE_LINES[:7] + [
        '"      30/03/69   SR6900010-  1         1.00 ผน Y          0.00  2                     0.00                     0.00"',
        '"      30/03/69   SR6900011-  1         2.00 ผน Y          0.00  2                     0.00                     0.00"',
    ]
    path = _write(tmp_path, "ขาย_only_sr.csv", only_sr)
    assert parse_weekly.parse_sales(path) == []


# ── policy-A tightening: an unreadable date header is REFUSED, not imported ──

_NO_DATE_FILTER = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                        หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า          ถึง  Zหน้าร้าน       วันที่ : 29/05/69"',
    '"------------------------------------------------------------"',
    '"  ลูกค้าทดสอบ\xa0/01ท01"',
    '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
    '"      02/03/69   IV6900100-  1        10.00 ใบ          149.00  1                  1490.00                  1490.00"',
]


@pytest.fixture
def no_date_filter_csv(tmp_path):
    return _write(tmp_path, "ขาย_ไม่มีช่วงวันที่.csv", _NO_DATE_FILTER)


def test_preview_refuses_a_file_whose_date_header_cannot_be_read(no_date_filter_csv):
    """Put's policy A, tightened 2026-08-03: a full-history export with no date
    filter used to import SILENTLY, because the guard returned False on any
    header it could not parse. Refuse instead and let the operator re-export."""
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router.preview_file(no_date_filter_csv, 'sales')


def test_commit_refuses_a_file_whose_date_header_cannot_be_read(
        no_date_filter_csv, spy_import_weekly):
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router.commit_file(no_date_filter_csv, 'sales')
    assert spy_import_weekly == []


def test_unreadable_header_message_tells_the_operator_what_to_do(no_date_filter_csv):
    with pytest.raises(import_router.HistoryExportBlocked) as exc:
        import_router.preview_file(no_date_filter_csv, 'sales')
    assert 'วันที่จาก' in str(exc.value), \
        'the message must name the header field that is missing'
