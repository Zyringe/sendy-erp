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


# ── orphan transaction row (Codex follow-up on 93e4472) ────────────────────
#
# A transaction line that appears BEFORE any product heading was counted as a
# candidate and then silently dropped. _validate_parse only noticed when the
# WHOLE file produced zero entries, so an orphan row sitting above otherwise
# valid data vanished and the import still reported success — precisely the
# silent partial-import this guard exists to prevent.

_ORPHAN_THEN_VALID = (
    SALES_SAMPLE_LINES[:6] + [
        '"      04/04/69   IV6900999-  1        99.00 ใบ          160.00  1                 15840.00                 15840.00"',
    ] + SALES_SAMPLE_LINES[6:]
)


@pytest.fixture
def orphan_then_valid_csv(tmp_path):
    return _write(tmp_path, "ขาย_orphan.csv", _ORPHAN_THEN_VALID)


def test_transaction_before_its_product_heading_blocks_the_file(orphan_then_valid_csv):
    """The rest of the file parses fine, so `entries` is non-empty and the
    zero-parse rule never fires. The orphan must be reported as a rejected row."""
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(orphan_then_valid_csv)
    msg = str(exc.value)
    assert 'ขาย_orphan.csv' in msg
    assert 'IV6900999' in msg, 'the error must quote the orphaned line'


def test_orphan_row_is_not_silently_dropped(orphan_then_valid_csv):
    """Companion to the above, stated as the data property that matters: the
    parser must never return a partial result for this file."""
    try:
        entries = parse_weekly.parse_sales(orphan_then_valid_csv)
    except ValueError:
        return                                   # refused outright: correct
    assert any(e['doc_no'].startswith('IV6900999') for e in entries), \
        'parsed successfully but lost the orphan row'


def test_sr_row_before_any_product_heading_is_still_exempt(tmp_path):
    """The SR exemption must survive the fix: a ใบลดหนี้ row has no product
    context either, and it is skipped by design, not rejected."""
    lines = (SALES_SAMPLE_LINES[:6] + [
        '"      30/03/69   SR6900010-  1         1.00 ผน Y          0.00  2                     0.00                     0.00"',
    ] + SALES_SAMPLE_LINES[6:])
    path = _write(tmp_path, "ขาย_sr_first.csv", lines)
    assert len(parse_weekly.parse_sales(path)) == 6


# ── product context must not survive a party change (Codex follow-up, 72418a6) ──
#
# The party branch updated current_party/current_party_code but left
# current_prod_name/current_prod_code alone. A transaction under a NEW customer,
# before that customer's first product heading, was therefore accepted and
# attributed to the PREVIOUS customer's product. Worse than the orphan case: the
# row is not dropped, it is silently mis-mapped onto the wrong product's stock.

_HDR7 = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                   หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"รหัสลูกค้า            ถึง  Zหน้าร้าน            วันที่ : 15/04/69"',
    '"วันที่จาก   12\xa0เม.ย.\xa02569     ถึง  31\xa0ธ.ค.\xa02569"',
    '"----------------------------------------------------------------"',
]

# customer A with its product and two good rows, then customer B whose first
# transaction has NO product heading of its own.
_PARTY_CHANGE_ORPHAN = _HDR7 + [
    '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
    '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
    '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
    '"      04/04/69   IV6900503-  2         3.00 ใบ          160.00  1                   480.00                   480.00"',
    '"  วรสวัสดิ์\xa0ฮาร์ดแวร์\xa0/01อ35"',
    '"      04/04/69   IV6900501-  1        48.00 ผง           30.00  2       1152.00       1152.00"',
]


@pytest.fixture
def party_change_orphan_csv(tmp_path):
    return _write(tmp_path, "ขาย_party_change.csv", _PARTY_CHANGE_ORPHAN)


def test_party_change_clears_product_context(party_change_orphan_csv):
    """The new customer's row has no product heading, so it is an orphan and the
    whole file must be refused."""
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(party_change_orphan_csv)
    msg = str(exc.value)
    assert 'ขาย_party_change.csv' in msg
    assert 'IV6900501' in msg, 'the error must quote the orphaned invoice line'


def test_a_row_is_never_attributed_to_the_previous_partys_product(party_change_orphan_csv):
    """The property that actually matters: no row may inherit the previous
    party's product. Silently mis-mapping stock is worse than dropping the row."""
    try:
        entries = parse_weekly.parse_sales(party_change_orphan_csv)
    except ValueError:
        return                                   # refused outright: correct
    bad = [e for e in entries
           if e['doc_no'].startswith('IV6900501') and e['product_code_raw'] == '031บ4120']
    assert not bad, f'row inherited the previous customer product: {bad}'


def test_party_change_with_its_own_product_heading_still_parses(tmp_path):
    """Control: the normal shape (every party followed by its own product
    heading) must be unaffected. Measured on all 19,864 transaction rows in the
    real Express exports: none relies on inheriting across a party boundary."""
    lines = _PARTY_CHANGE_ORPHAN[:-1] + [
        '"   กลอนห้องน้ำกลาง\xa0STL#430\xa0(P)\xa0/001ก3435"',
        '"      04/04/69   IV6900501-  1        48.00 ผง           30.00  2        20%       1152.00                  1152.00"',
    ]
    path = _write(tmp_path, "ขาย_party_change_ok.csv", lines)
    entries = parse_weekly.parse_sales(path)
    assert len(entries) == 3
    got = {e['doc_no']: e['product_code_raw'] for e in entries}
    assert got['IV6900501-1'] == '001ก3435', 'must use the NEW party product'
    assert got['IV6900503-1'] == '031บ4120'


# ── structural headings must fail CLOSED (Codex follow-up on d9484ca) ──────
#
# Both structural branches `continue` even when their regex fails, so a
# malformed heading left the PREVIOUS context in place and the next transaction
# was attributed to it. Same silent mis-mapping as the party-boundary bug, via a
# different door. A product heading before any party also let a row parse with
# party=None.
#
# Invariant now: a non-SR transaction requires BOTH a valid party and a valid
# product. Measured over every real Express export (19,864 transaction rows):
# zero rows lack either, and there are zero malformed party/product lines, so
# this cannot false-reject a real import.

# after '/' there is a space, so `^(.+?)\s*/(\S+)\s*$` cannot match
_BAD_PARTY_LINE = '"  ร้านค้าเสีย\xa0/รหัส ที่มีช่องว่าง"'
_BAD_PROD_LINE = '"   สินค้าเสีย\xa0/รหัส ที่มีช่องว่าง"'


def _sales_file(*body):
    return _HDR7 + list(body)


@pytest.fixture
def malformed_party_csv(tmp_path):
    return _write(tmp_path, "ขาย_bad_party.csv", _sales_file(
        '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
        '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
        '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
        _BAD_PARTY_LINE,
        '"      04/04/69   IV6900998-  1        10.00 ใบ          160.00  1                  1600.00                  1600.00"',
    ))


@pytest.fixture
def malformed_product_csv(tmp_path):
    return _write(tmp_path, "ขาย_bad_prod.csv", _sales_file(
        '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
        '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
        '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
        _BAD_PROD_LINE,
        '"      04/04/69   IV6900997-  1        10.00 ใบ          160.00  1                  1600.00                  1600.00"',
    ))


@pytest.fixture
def product_before_any_party_csv(tmp_path):
    """Product heading + transaction before any party, THEN a valid grouping.

    The trailing valid grouping is deliberate: without it the file parses to
    zero entries and _validate_parse's zero-parse branch fires first (correctly
    — see the ordering note there), whose message does not quote lines. The
    dangerous real shape is a context-less row hidden among good ones, which is
    what this fixture builds, and there the partial-parse message must name it.
    """
    return _write(tmp_path, "ขาย_no_party.csv", _sales_file(
        '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
        '"      04/04/69   IV6900996-  1        10.00 ใบ          160.00  1                  1600.00                  1600.00"',
        '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
        '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
        '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
    ))


@pytest.fixture
def only_a_context_less_row_csv(tmp_path):
    """The same defect with NO valid rows at all: still refused, via the
    zero-parse branch."""
    return _write(tmp_path, "ขาย_no_party_only.csv", _sales_file(
        '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
        '"      04/04/69   IV6900996-  1        10.00 ใบ          160.00  1                  1600.00                  1600.00"',
    ))


def test_a_context_less_row_alone_still_blocks_the_file(only_a_context_less_row_csv):
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(only_a_context_less_row_csv)
    assert 'ขาย_no_party_only.csv' in str(exc.value)


def test_malformed_party_heading_blocks_the_file(malformed_party_csv):
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(malformed_party_csv)
    assert 'IV6900998' in str(exc.value)


def test_malformed_product_heading_blocks_the_file(malformed_product_csv):
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(malformed_product_csv)
    assert 'IV6900997' in str(exc.value)


def test_transaction_before_any_party_blocks_the_file(product_before_any_party_csv):
    """Even with a valid product heading, a row with no customer/supplier must
    not parse — it would land with party=None."""
    with pytest.raises(ValueError) as exc:
        parse_weekly.parse_sales(product_before_any_party_csv)
    assert 'IV6900996' in str(exc.value)


def test_no_row_ever_inherits_context_across_a_malformed_heading(
        malformed_party_csv, malformed_product_csv):
    """The property, not the message: a malformed heading must never leave a row
    attached to the previous party or product."""
    for path, doc in ((malformed_party_csv, 'IV6900998'),
                      (malformed_product_csv, 'IV6900997')):
        try:
            entries = parse_weekly.parse_sales(path)
        except ValueError:
            continue                             # refused outright: correct
        stale = [e for e in entries if e['doc_no'].startswith(doc)
                 and (e['product_code_raw'] == '031บ4120' or e['party_code'] == '01พ02')]
        assert not stale, f'{doc} inherited stale context: {stale}'


def test_normal_sales_and_purchase_groupings_still_parse(
        sample_sales_file, sample_purchase_file):
    """Control for all three asserts above: the ordinary Express shape must be
    unaffected, with every row carrying its OWN party and product."""
    sales = parse_weekly.parse_sales(sample_sales_file)
    purch = parse_weekly.parse_purchases(sample_purchase_file)
    assert len(sales) == 6 and len(purch) == 2
    assert all(e['party_code'] and e['product_code_raw'] for e in sales + purch)
    by_doc = {e['doc_no']: (e['party_code'], e['product_code_raw']) for e in sales}
    assert by_doc['IV6900503-1'] == ('01พ02', '031บ4120')
    assert by_doc['IV6900501-1'] == ('01อ35', '001ก3435')
