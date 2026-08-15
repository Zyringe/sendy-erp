"""TDD tests for the hardened import_customers_from_bsn function.

Tests are written FIRST and must fail against the current implementation.
Run: cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_customer_import_hardening.py -q

Contract:
  - Protected rows (contact_normalized_at IS NOT NULL): only operational fields updated
    (salesperson, zone, customer_type, credit_days, tax_id, imported_at).
    Contact fields (name, phone, fax, contact, address, nickname, contact_orig_json,
    contact_normalized_at/by) are untouched.
  - Un-normalized rows / new rows: run through normalize_customer.
    If confidence=='auto' AND a field actually changes -> apply fax split + mark normalized.
    If confidence=='review' OR no actual change -> keep raw, leave contact_normalized_at NULL.
  - Return value is (inserted, updated, protected) — caller receives named tuple or 3-int
    tuple; route updated to unpack correctly.
  - Lossless: every >=7-digit number in the imported phone survives in the resulting row.
"""
import json
import os
import re
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_customer(code, **kwargs):
    """Return a minimal customer import dict (all BSN export keys present)."""
    return {
        'code': code,
        'name': kwargs.get('name', f'ร้านทดสอบ {code}'),
        'salesperson': kwargs.get('salesperson', 'SP01'),
        'zone': kwargs.get('zone', 'ZONE1'),
        'customer_type': kwargs.get('customer_type', 'R'),
        'address': kwargs.get('address', ''),
        'phone': kwargs.get('phone', ''),
        'tax_id': kwargs.get('tax_id', ''),
        'credit_days': kwargs.get('credit_days', 0),
        'contact': kwargs.get('contact', ''),
    }


def _fetch_customer(conn, code):
    row = conn.execute(
        "SELECT * FROM customers WHERE code=?", (code,)
    ).fetchone()
    return dict(row) if row else None


def _insert_protected_row(conn, code, phone, fax, credit_days=0, zone='ZONE_OLD'):
    """Insert a customers row that is already normalized (contact_normalized_at IS NOT NULL)."""
    conn.execute("""
        INSERT INTO customers
            (code, name, salesperson, zone, customer_type, phone, fax,
             credit_days, contact_normalized_at, contact_normalized_by,
             imported_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'),'manual_clean',datetime('now','localtime'))
    """, (code, f'ร้าน {code}', 'SP_OLD', zone, 'R', phone, fax, credit_days))
    conn.commit()


def _digit_runs_7plus(text):
    """All 7+ digit runs in text (for lossless check)."""
    if not text:
        return set()
    runs = set()
    for m in re.finditer(r"\d[\d().\-/]*\d", text or ""):
        digs = re.sub(r"\D", "", m.group(0))
        if len(digs) >= 7:
            runs.add(digs)
    return runs


def _all_digits_in_row(row):
    """Collect all 7+ digit runs from phone + fax + contact columns of a DB row."""
    combined = " ".join([
        row.get('phone') or '',
        row.get('fax') or '',
        row.get('contact') or '',
    ])
    return re.sub(r"\D", "", combined)  # return concat for substring search


# ---------------------------------------------------------------------------
# Fixture: import_customers_from_bsn via models
# ---------------------------------------------------------------------------
@pytest.fixture
def run_import(tmp_db):
    """Return a callable that invokes models.import_customers_from_bsn with a
    list of customer dicts, and returns the result + a fresh connection for
    assertions."""
    import models
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    def _run(customers):
        return models.import_customers_from_bsn(customers)

    yield _run, conn
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: Protected row keeps cleaned contact, updates operational fields
# ---------------------------------------------------------------------------
def test_protected_row_keeps_contact_updates_operational(tmp_db, run_import):
    """A row with contact_normalized_at IS NOT NULL must not have its contact
    fields overwritten. Operational fields (credit_days, zone) must update."""
    run, conn = run_import

    CODE = 'TEST_PROTECTED_001'
    CLEAN_PHONE = '099-CLEAN'
    CLEAN_FAX = '02-FAXCLEAN'

    _insert_protected_row(conn, CODE, phone=CLEAN_PHONE, fax=CLEAN_FAX,
                          credit_days=0, zone='ZONE_OLD')

    # Re-import with messy phone + new operational data
    customers = [_make_customer(
        CODE,
        phone='เฮีย081-0433196 mess',
        credit_days=30,
        zone='NEWZONE',
    )]

    result = run(customers)

    # Return must carry a 'protected' count
    assert len(result) == 3, (
        f"Expected (inserted, updated, protected) 3-tuple, got {result!r}"
    )
    inserted, updated, protected = result
    assert protected == 1, f"Expected 1 protected, got {protected}"
    assert updated == 0, f"Expected 0 updated (row was protected, not a normal update)"

    row = _fetch_customer(conn, CODE)
    assert row is not None

    # Contact fields UNTOUCHED
    assert row['phone'] == CLEAN_PHONE, (
        f"phone was clobbered: expected {CLEAN_PHONE!r}, got {row['phone']!r}"
    )
    assert row['fax'] == CLEAN_FAX, (
        f"fax was clobbered: expected {CLEAN_FAX!r}, got {row['fax']!r}"
    )
    assert row['contact_normalized_at'] is not None, "contact_normalized_at was cleared"

    # Operational fields UPDATED
    assert row['credit_days'] == 30, (
        f"credit_days not updated: got {row['credit_days']}"
    )
    assert row['zone'] == 'NEWZONE', (
        f"zone not updated: got {row['zone']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Fresh clean-except-fax import gets fax split + marked normalized
# ---------------------------------------------------------------------------
def test_fresh_fax_split_auto_marked(tmp_db, run_import):
    """A new customer row whose phone carries 'F:' is auto-split and marked
    contact_normalized_at='bsn_import'."""
    run, conn = run_import

    CODE = 'TEST_AUTO_FAX_002'
    RAW_PHONE = '02-1234567,F:02-7654321'

    customers = [_make_customer(CODE, phone=RAW_PHONE, contact='')]
    inserted, updated, protected = run(customers)
    assert inserted == 1, f"Expected 1 inserted, got {inserted}"

    row = _fetch_customer(conn, CODE)
    assert row is not None

    # Fax split
    assert row['phone'] == '02-1234567', (
        f"phone after fax split: {row['phone']!r}"
    )
    assert row['fax'] == '02-7654321', (
        f"fax after fax split: {row['fax']!r}"
    )

    # Marked as auto-normalized
    assert row['contact_normalized_at'] is not None, (
        "contact_normalized_at should be set for auto-applied rows"
    )
    assert row['contact_normalized_by'] == 'bsn_import', (
        f"contact_normalized_by: {row['contact_normalized_by']!r}"
    )

    # contact_orig_json must carry the original phone string
    assert row['contact_orig_json'] is not None, "contact_orig_json must be set"
    orig = json.loads(row['contact_orig_json'])
    assert orig['phone'] == RAW_PHONE, (
        f"contact_orig_json.phone: {orig.get('phone')!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: Fresh messy import stays raw, not marked
# ---------------------------------------------------------------------------
def test_fresh_messy_import_stays_raw_not_marked(tmp_db, run_import):
    """A new customer whose phone has a person-name prefix (review confidence)
    must be stored raw, with fax=NULL and contact_normalized_at=NULL."""
    run, conn = run_import

    CODE = 'TEST_MESSY_003'
    RAW_PHONE = 'เฮีย081-0433196,056-352038'

    customers = [_make_customer(CODE, phone=RAW_PHONE, contact='')]
    inserted, updated, protected = run(customers)
    assert inserted == 1

    row = _fetch_customer(conn, CODE)
    assert row is not None

    # Raw phone stored unchanged
    assert row['phone'] == RAW_PHONE, (
        f"Messy phone should be stored raw, got: {row['phone']!r}"
    )
    # fax stays NULL
    assert row['fax'] is None, f"fax should be NULL for review rows, got: {row['fax']!r}"
    # NOT marked normalized
    assert row['contact_normalized_at'] is None, (
        "contact_normalized_at must stay NULL for review-confidence rows"
    )


# ---------------------------------------------------------------------------
# Test 4: Lossless — every >=7-digit number in imported phone survives in row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,raw_phone,contact", [
    ('TEST_LOSSLESS_AUTO_004A', '02-1234567,F:02-7654321', ''),
    ('TEST_LOSSLESS_MESSY_004B', 'เฮีย081-0433196,056-352038', ''),
])
def test_lossless_digits_survive(tmp_db, run_import, code, raw_phone, contact):
    """Every 7+ digit run in the imported phone must appear somewhere in the
    resulting row (phone + fax + contact combined)."""
    run, conn = run_import

    customers = [_make_customer(code, phone=raw_phone, contact=contact)]
    run(customers)

    row = _fetch_customer(conn, code)
    assert row is not None

    orig_runs = _digit_runs_7plus(raw_phone)
    row_digits = _all_digits_in_row(row)

    for run_digits in orig_runs:
        assert run_digits in row_digits, (
            f"Lost digit run {run_digits!r} from phone {raw_phone!r}. "
            f"Row phone={row.get('phone')!r} fax={row.get('fax')!r} "
            f"contact={row.get('contact')!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: Existing un-normalized row re-imports and updates normally
# ---------------------------------------------------------------------------
def test_existing_unnormalized_row_updates_normally(tmp_db, run_import):
    """An existing row with contact_normalized_at IS NULL is NOT in the
    protected branch — name/phone/zone refresh normally."""
    run, conn = run_import

    CODE = 'TEST_UNNORM_005'

    # Insert a row WITHOUT contact_normalized_at (un-normalized)
    conn.execute("""
        INSERT INTO customers
            (code, name, salesperson, zone, customer_type, phone,
             credit_days, imported_at)
        VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))
    """, (CODE, 'ชื่อเก่า', 'SP_OLD', 'OLD_ZONE', 'R', '02-0000000', 0))
    conn.commit()

    # Re-import with fresh data (clean phone so auto path applies or raw stored)
    NEW_PHONE = '02-9999999'
    customers = [_make_customer(
        CODE,
        name='ชื่อใหม่',
        phone=NEW_PHONE,
        zone='NEW_ZONE',
        credit_days=15,
    )]
    inserted, updated, protected = run(customers)

    # Must NOT count as protected
    assert protected == 0, f"Un-normalized row should not be protected, got protected={protected}"
    assert updated == 1, f"Expected 1 updated, got {updated}"

    row = _fetch_customer(conn, CODE)
    # Name refreshed from import (import is source of truth for name when un-normalized)
    assert row['name'] == 'ชื่อใหม่', f"name should update for un-normalized row: {row['name']!r}"
    # Zone and credit_days updated
    assert row['zone'] == 'NEW_ZONE'
    assert row['credit_days'] == 15



# ═══════════════════════════════════════════════════════════════════════════
# Finding 6 — the customer-master parser imports EVERY Express code and
# refuses a zero/partial parse.
#
# Put ruled 2026-08-15: every customer code present in Express is legitimate
# and must be imported with all of its information. Fixtures are written in
# real cp874 with the real column geometry, because the whole defect class is
# "the format shifted and nobody noticed".
# ═══════════════════════════════════════════════════════════════════════════

_HDR = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                 หน้า   :        1"',
    '"  รายละเอียดลูกค้า\xa0แยกตามประเภทลูกค้า"',
    '"ประเภทลูกค้าจาก                  ถึง  ๙๙                                                                 วันที่ : 24/04/69"',
    '"--------------------------------------------------------------------------------------------------------------------------"',
    '"  รหัส       คำนำหน้า+ชื่อลูกค้า                                                 พนักงานขาย  เขต   ประเภทราคา     ส่วนลด"',
    '"--------------------------------------------------------------------------------------------------------------------------"',
    '"  ประเภท : ลูกค้าประจำ"',
]
_FOOTER = ['">>>> จบรายงาน <<<<"']

# The real hazard line for the column anchor: a bare postcode ~39 columns in,
# followed by the เครดิต field. Verbatim geometry from line 51 of the real
# export. `lstrip()` on it starts with two digits, so the plan's unanchored
# candidate rule would read it as a broken customer row.
_POSTCODE_CONTINUATION = (
    '"                                       10240                         '
    'เครดิต    :  60  วัน        วงเงิน    :          0.00"')


def _cust_block(code, name, sp='01', zone='กท', tail_discount='0'):
    return [
        f'"  {code}      {name}                                                     {sp}          {zone}       {tail_discount}"',
        '"      ที่อยู่  : 233\xa0ซ.ทดสอบ\xa07                                    ผู้ติดต่อ : คุณเอ"',
        '"                 เขตทดสอบ\xa0กรุงเทพ                                     เลขที่บ/ช : 11-02-01-00     ขนส่งโดย  : 01"',
        _POSTCODE_CONTINUATION,
        '"      โทร.     : 02-7303880-1,F:02-7303882                           เงื่อนไข  :"',
        '"      Tax ID   : 0103544032448         \xa0สำนักงานใหญ่\xa0"',
        '',
    ]


def _write_cp874(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text('\n'.join(lines) + '\n', encoding='cp874')
    return str(p)


@pytest.fixture
def valid_customer_file(tmp_path):
    return _write_cp874(tmp_path, 'ok.csv',
                        _HDR + _cust_block('01ก01', 'หจก.\xa0ทดสอบหนึ่ง')
                        + _cust_block('01ก02', '\xa0ทดสอบสอง', sp='03') + _FOOTER)


def test_valid_file_parses_every_customer(valid_customer_file):
    from blueprints.partners import _parse_bsn_customers
    rows = _parse_bsn_customers(valid_customer_file)
    assert [r['code'] for r in rows] == ['01ก01', '01ก02']
    assert rows[0]['salesperson'] == '01' and rows[0]['zone'] == 'กท'
    assert rows[0]['credit_days'] == 60
    assert rows[0]['contact'] == 'คุณเอ'
    assert rows[0]['tax_id'] == '0103544032448'


# Every code shape Express actually holds — Put's ruling is that all of them
# are legitimate. The pre-ruling regex (`\d{2}[ก-ฮA-Za-z]\d{2,3}`) matched only
# the first of these and silently dropped 1,186 real customers.
@pytest.mark.parametrize('code', [
    '01ก01',        # two-digit prefix (the only shape the old regex allowed)
    '032ท03',       # three-digit prefix — 899 rows in the real export
    '042บ021',      # three-digit prefix, three-digit suffix
    '1101ค01',      # four-digit prefix
    '23ทธ01',       # two Thai letters
    'L1004ค01',     # Laos branch code — 280 rows
    'Bหน้าร้าน',     # shop-front code, no digits at all
    'S02',          # short salesperson-style code
    'Zหน้าร้าน',
])
def test_every_express_code_shape_is_imported(tmp_path, code):
    from blueprints.partners import _parse_bsn_customers
    path = _write_cp874(tmp_path, 'shapes.csv',
                        _HDR + _cust_block(code, 'ร้านทดสอบ') + _FOOTER)
    rows = _parse_bsn_customers(path)
    assert [r['code'] for r in rows] == [code]
    assert rows[0]['name'] == 'ร้านทดสอบ'
    assert rows[0]['salesperson'] == '01' and rows[0]['zone'] == 'กท'


def test_format_shifted_customer_row_is_refused_with_its_source_line(tmp_path):
    """The second row still sits in the code column but has lost its trailing
    discount field, so the strict row regex refuses it. Silently dropping it is
    the defect."""
    from blueprints.partners import _parse_bsn_customers
    shifted = _cust_block('01ก02', '\xa0ทดสอบสอง', sp='03')
    shifted[0] = shifted[0].rstrip('"').rstrip().rstrip('0').rstrip() + '"'
    path = _write_cp874(tmp_path, 'shifted.csv',
                        _HDR + _cust_block('01ก01', 'หจก.\xa0ทดสอบหนึ่ง') + shifted + _FOOTER)

    with pytest.raises(ValueError) as exc:
        _parse_bsn_customers(path)

    msg = str(exc.value)
    assert 'อ่านข้อมูลลูกค้าไม่ครบ' in msg
    assert '01ก02' in msg, 'error must quote the offending source line'
    # the shifted row is line 15: 7 header lines + a 7-line customer block + 1
    assert 'บรรทัด 15:' in msg, f'error must name the source line number: {msg}'


def test_report_shaped_file_with_zero_customers_is_refused(tmp_path):
    from blueprints.partners import _parse_bsn_customers
    path = _write_cp874(tmp_path, 'empty_report.csv', _HDR + _FOOTER)
    with pytest.raises(ValueError) as exc:
        _parse_bsn_customers(path)
    assert 'ไม่พบรายการลูกค้าในรายงาน' in str(exc.value)


def test_non_report_file_is_refused(tmp_path):
    """A DIFFERENT Express report that still mentions ลูกค้า and carries codes
    in a left column — the shape a bare "contains รายงาน" gate would wave
    through, and the one that would write garbage name/zone."""
    from blueprints.partners import _parse_bsn_customers
    path = _write_cp874(tmp_path, 'ar_report.csv', [
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                                    หน้า   :        1"',
        '"  รายงานลูกหนี้คงค้าง\xa0แยกตามลูกค้า"',
        '"  01ก01      ลูกค้าทดสอบ                                     01          กท       0"',
        '">>>> จบรายงาน <<<<"',
    ])
    with pytest.raises(ValueError) as exc:
        _parse_bsn_customers(path)
    assert 'ไฟล์ผิดประเภท' in str(exc.value)


def test_address_continuation_lines_are_not_customer_candidates(tmp_path):
    """Pins the COLUMN ANCHOR, the one deviation from the plan's rule.

    The fixture contains a real postcode-continuation line whose `lstrip()`
    starts with two digits. The plan's unanchored rule flags 434 such lines in
    the real export (first at line 51) and would refuse every real import.
    """
    from blueprints.partners import (_parse_bsn_customers,
                                     _CUSTOMER_CANDIDATE_RE, _CUSTOMER_RE)
    hazard = _POSTCODE_CONTINUATION.strip('"').replace('\xa0', ' ')

    # control: the hazard line IS the shape the unanchored rule would catch
    assert hazard.lstrip()[:2].isdigit(), 'fixture lost its postcode line'
    assert not _CUSTOMER_RE.match(hazard), 'hazard line must not parse as a row'
    # …and the anchored rule correctly ignores it
    assert not _CUSTOMER_CANDIDATE_RE.match(hazard), \
        'an address continuation was read as a broken customer row'

    path = _write_cp874(tmp_path, 'ok2.csv',
                        _HDR + _cust_block('01ก01', 'ร้านหนึ่ง') + _FOOTER)
    assert len(_parse_bsn_customers(path)) == 1


def test_repeated_page_column_header_is_not_a_candidate(tmp_path):
    """The column header repeats once per page break (529× in the real export)
    and sits in the code column — it must be structural, not a broken row."""
    from blueprints.partners import _parse_bsn_customers
    # Verbatim page-break geometry from the real export (lines 40-45): banner,
    # title, range, dashes, column header, dashes. The 6-line shape is
    # load-bearing — the parser's `(BSN)` handler skips a fixed 5 lines.
    page_two = _HDR[:6]
    path = _write_cp874(tmp_path, 'paged.csv',
                        _HDR + _cust_block('01ก01', 'ร้านหนึ่ง')
                        + page_two + _cust_block('01ก02', 'ร้านสอง') + _FOOTER)
    assert [r['code'] for r in _parse_bsn_customers(path)] == ['01ก01', '01ก02']


@pytest.mark.skipif(
    not os.path.exists('/Users/putty/Sendai-Boonsawat/sendy_erp/data/source/bsn_customer_info.csv'),
    reason='real BSN customer export not present')
def test_real_export_parses_every_express_code():
    """Integration anchor for Put's ruling. Shifts when the export is
    refreshed — recompute against ARMAS.DBF, do not guess."""
    from blueprints.partners import _parse_bsn_customers
    rows = _parse_bsn_customers(
        '/Users/putty/Sendai-Boonsawat/sendy_erp/data/source/bsn_customer_info.csv')
    assert len(rows) == 2663, f'expected 2,663 customer rows, got {len(rows)}'
    codes = {r['code'] for r in rows}
    assert len(codes) == len(rows), 'a code was parsed twice'
    # every row carries its identifying information, not just a code
    assert all(r['name'] and r['salesperson'] and r['zone'] and r['customer_type']
               for r in rows)
    # the shapes the old regex dropped are present
    for code in ('032ท03', '1101ค01', '23ทธ01', 'L1004ค01', 'Bหน้าร้าน'):
        assert code in codes, f'{code} still missing'
