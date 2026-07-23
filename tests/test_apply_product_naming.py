"""Phase 3 apply-engine tests (TDD) — compile decisions → strict ops, then apply.

Covers: mechanical compose (multi-fix products), name-insertion helpers,
no-change no-op, locked-sku skip, D7 collision (active wins bare; lower pid),
pid-1768 exclusion, --apply guard stack on a tmp DB copy.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import apply_product_naming as apn
import compile_apply_operations as cao


# ── name-transform helpers ────────────────────────────────────────────────────

def test_mech_inch_to_in():
    assert cao.mech_transform("INCH_TO_IN", 'ดจ.สแตนเลส META 17/64นิ้ว') == 'ดจ.สแตนเลส META 17/64in'
    assert cao.mech_transform("INCH_TO_IN", 'แผ่นเจียหนา RESIBON 7" สีแดง') == 'แผ่นเจียหนา RESIBON 7in สีแดง'


def test_mech_mm_cm():
    assert cao.mech_transform("MM_CM_FORMAT", 'อะไหล่ลูกกลิ้งทาสีหนา 13 mm. Microfiber Sendai 1in') == \
        'อะไหล่ลูกกลิ้งทาสีหนา 13mm Microfiber Sendai 1in'
    assert cao.mech_transform("MM_CM_FORMAT", 'กรอบ 5 CM. สีทอง') == 'กรอบ 5cm สีทอง'


def test_mech_typo_and_packaging():
    assert cao.mech_transform("TYPO_CURATED", 'ปุ๊กตะกั่ว 1/4"') == 'พุกตะกั่ว 1/4"'
    assert cao.mech_transform("PACKAGING_LEGACY", 'บานพับ 4นิ้ว(P)#3043') == 'บานพับ 4นิ้ว(แผง)#3043'
    assert cao.mech_transform("PACK_VARIANT_SUFFIX", 'กลอนมะยม Sendai #230-4in สีรมดำ (AC) (ตัว) 1') == \
        'กลอนมะยม Sendai #230-4in สีรมดำ (AC) (ตัว)'


def test_mech_compose_multi_fix():
    # pid 1818: TYPO + INCH compose to one final name
    name = '[MERGED→947] ปุ๊กตะกั่ว 1/4"'
    out = cao.mech_transform("TYPO_CURATED", cao.mech_transform("INCH_TO_IN", name))
    assert out == '[MERGED→947] พุกตะกั่ว 1/4in'
    # pid 1249: INCH + MM_CM
    name = "ดจ.โรตารี 'GOLDEN LION' 8นิ้วx110 mm."
    out = cao.mech_transform("MM_CM_FORMAT", cao.mech_transform("INCH_TO_IN", name))
    assert out == "ดจ.โรตารี 'GOLDEN LION' 8inx110mm"


def test_insert_color_before_packaging_bracket():
    assert cao.insert_color('ลูกบิด Sendai #5112 (แผง)', 'สีทองด้าน (SB)') == \
        'ลูกบิด Sendai #5112 สีทองด้าน (SB) (แผง)'
    # no bracket → append
    assert cao.insert_color('กาวร้อน TH 18', 'สีใส (TRN)') == 'กาวร้อน TH 18 สีใส (TRN)'
    # condition bracket only → before it
    assert cao.insert_color('กลอน Sendai #230 (เก่า)', 'สีดำ (BLK)') == 'กลอน Sendai #230 สีดำ (BLK) (เก่า)'


def test_insert_brand_per_template():
    # before #model
    assert cao.insert_brand('พุกพลาสติกพร้อมน็อต #6 สีฟ้า', 'Sendai') == \
        'พุกพลาสติกพร้อมน็อต Sendai #6 สีฟ้า'
    # before size when no model — the rule doc's own worked example
    assert cao.insert_brand('กรอบจตุคาม 5cm สีทอง (แผง)', 'Sendai') == \
        'กรอบจตุคาม Sendai 5cm สีทอง (แผง)'
    # before สี token when no model/size
    assert cao.insert_brand('กันชนสแตนเลส DOME สีรมดำ (AC)', 'Sendai') == \
        'กันชนสแตนเลส DOME Sendai สีรมดำ (AC)'
    # nothing to anchor on → append
    assert cao.insert_brand('ตะขอแขวนสินค้า', 'Sendai') == 'ตะขอแขวนสินค้า Sendai'


# ── apply engine on a tmp DB ─────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY, product_name TEXT NOT NULL,
    brand_id INTEGER, category_id INTEGER, color_code TEXT,
    series TEXT, model TEXT, size TEXT, packaging_th TEXT, packaging_short TEXT,
    condition TEXT, pack_variant TEXT, sub_category_short_code TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    sku_code TEXT, sku_code_locked INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_products_sku_code ON products(sku_code);
CREATE TABLE brands (id INTEGER PRIMARY KEY, name TEXT, name_th TEXT, short_code TEXT);
CREATE TABLE color_finish_codes (code TEXT PRIMARY KEY, name_th TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, short_code TEXT);
CREATE TABLE product_code_mapping (id INTEGER PRIMARY KEY, bsn_code TEXT, product_id INTEGER);
CREATE TABLE import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
    rows_imported INTEGER NOT NULL, rows_skipped INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), notes TEXT
);
"""


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO brands VALUES (3,'Sendai','เซ็นได','SD'), (22,'HORSE SHOE',NULL,'HORSE')")
    conn.execute("INSERT INTO color_finish_codes VALUES ('JSN','สีนิกเกิล'), ('NK','สีนิกเกิล')")
    conn.execute("INSERT INTO categories VALUES (1,'BLT')")
    conn.commit()
    conn.close()
    return path


def _add_product(path, pid, name, sku=None, active=1, locked=0, brand=None, color=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO products (id, product_name, sku_code, is_active, sku_code_locked, brand_id, color_code, category_id)"
        " VALUES (?,?,?,?,?,?,?,1)", (pid, name, sku, active, locked, brand, color))
    conn.commit()
    conn.close()


def test_apply_name_field_dict_ops(db):
    _add_product(db, 10, 'ลูกบิด Sendai #5112 (แผง)', 'S10')
    ops = [
        {'op': 'name', 'product_id': '10', 'before': 'ลูกบิด Sendai #5112 (แผง)',
         'after': 'ลูกบิด Sendai #5112 สีทองด้าน (SB) (แผง)', 'field': '', 'value': '', 'source': 't'},
        {'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
         'before': '', 'after': 'SB', 'source': 't'},
        {'op': 'brand_name_th', 'product_id': '', 'field': '22', 'value': 'เกือกม้า',
         'before': '', 'after': 'เกือกม้า', 'source': 't'},
    ]
    res = apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT product_name, color_code FROM products WHERE id=10").fetchone()
    assert row == ('ลูกบิด Sendai #5112 สีทองด้าน (SB) (แผง)', 'SB')
    assert conn.execute("SELECT name_th FROM brands WHERE id=22").fetchone()[0] == 'เกือกม้า'
    assert conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 1
    assert res['applied'] == 3


def test_apply_name_op_stale_before_rolls_back(db):
    _add_product(db, 10, 'ชื่อจริงใน DB', 'S10')
    ops = [{'op': 'name', 'product_id': '10', 'before': 'ชื่อเก่าที่ stale',
            'after': 'ใหม่', 'field': '', 'value': '', 'source': 't'}]
    with pytest.raises(apn.ApplyError):
        apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT product_name FROM products WHERE id=10").fetchone()[0] == 'ชื่อจริงใน DB'
    assert conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 0


def test_sku_regen_skips_locked_and_d7_collision(db):
    # both products canonically build to the same code; active must win bare, inactive gets -{id}
    _add_product(db, 20, 'กลอน A', 'OLD-A', active=1, brand=3, color='AC')
    _add_product(db, 21, 'กลอน B', 'OLD-B', active=0, brand=3, color='AC')
    _add_product(db, 22, 'กลอน L', 'KEEP-ME', locked=1, brand=3, color='AC')
    plans = apn.plan_sku_regen(db, field_overrides={})
    by_pid = {p['product_id']: p for p in plans}
    assert 22 not in by_pid                      # locked skipped
    assert by_pid[20]['after'] == 'BLT-SD-AC'    # active wins bare
    assert by_pid[21]['after'] == 'BLT-SD-AC-21' # inactive disambiguated
    apn.apply_sku_plans(db, plans, backup_dir=os.path.dirname(db))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT sku_code FROM products WHERE id=22").fetchone()[0] == 'KEEP-ME'
    codes = {r[0] for r in conn.execute("SELECT sku_code FROM products WHERE id IN (20,21)")}
    assert codes == {'BLT-SD-AC', 'BLT-SD-AC-21'}


def test_sku_swap_chain_two_phase(db):
    # A's new code == B's OLD code while B moves away — single-pass UPDATE
    # order would hit UNIQUE; two-phase parking must succeed
    _add_product(db, 30, 'ก', 'CODE-B', active=1)
    _add_product(db, 31, 'ข', 'CODE-C', active=1)
    plans = [
        {'product_id': 30, 'before': 'CODE-B', 'after': 'CODE-C', 'is_active': 1},
        {'product_id': 31, 'before': 'CODE-C', 'after': 'CODE-D', 'is_active': 1},
    ]
    apn.apply_sku_plans(db, plans, backup_dir=os.path.dirname(db))
    conn = sqlite3.connect(db)
    got = {r[0]: r[1] for r in conn.execute("SELECT id, sku_code FROM products WHERE id IN (30,31)")}
    assert got == {30: 'CODE-C', 31: 'CODE-D'}


def test_compiler_excludes_pending_and_noop():
    rows = [
        {'product_id': '1768', 'issue': 'MODEL_MISMATCH', 'current_name': 'x', 'current_fields': '',
         'decision': "pending: excluded from this apply round — Put to read Express bill (Put 2026-07-07)",
         'proposed_fix': 'anything', 'triage': 'AMBIGUOUS'},
        {'product_id': '367', 'issue': 'MODEL_MISMATCH', 'current_name': 'x', 'current_fields': '',
         'decision': 'approved', 'proposed_fix': 'no change — BSN raw confirms same codes', 'triage': 'CLEAR'},
    ]
    ops, errors = cao.compile_judgment(rows)
    assert ops == [] and errors == []


def test_symbol_only_series_omitted_from_sku():
    from sku_code_utils import build_sku_code
    base = {'id': 1797, 'cat_short_code': 'SDR', 'sub_category_short_code': 'SDR+',
            'brand_short_code': 'ANS', 'size': '4mm/5in', 'color_code': 'BLK'}
    assert build_sku_code({**base, 'series': '+'}) == 'SDR-SDR+-ANS-4mm-5in-BLK'
    assert build_sku_code({**base, 'series': '-'}) == 'SDR-SDR+-ANS-4mm-5in-BLK'
    # alphanumeric series still included
    assert build_sku_code({**base, 'series': 'DOME'}) == 'SDR-SDR+-ANS-DOME-4mm-5in-BLK'


def test_color_name_th_op(db):
    ops = [{'op': 'color_name_th', 'product_id': '', 'field': 'JSN', 'value': 'สีนิกเกิ้ล',
            'before': 'สีนิกเกิล', 'after': 'สีนิกเกิ้ล', 'source': 'Put chat 2026-07-07'}]
    apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name_th FROM color_finish_codes WHERE code='JSN'").fetchone()[0] == 'สีนิกเกิ้ล'
    assert conn.execute("SELECT name_th FROM color_finish_codes WHERE code='NK'").fetchone()[0] == 'สีนิกเกิล'


def test_compiler_explicit_name_target():
    rows = [{'product_id': '53', 'issue': 'BRAND_MISMATCH', 'current_name': 'กรอบจตุคาม 5cm สีทอง (แผง)',
             'current_fields': '', 'triage': 'AMBIGUOUS',
             'decision': "approved (batch 2): add 'Sendai' to name -> 'กรอบจตุคาม Sendai 5cm สีทอง (แผง)' (Put 2026-07-07)",
             'proposed_fix': ''}]
    ops, errors = cao.compile_judgment(rows)
    assert errors == []
    assert ops == [{'op': 'name_target', 'product_id': '53', 'field': '',
                    'value': 'กรอบจตุคาม Sendai 5cm สีทอง (แผง)', 'before': '', 'after': '',
                    'source': 'judgment BRAND_MISMATCH explicit target'}]


def test_compiler_fails_loud_on_unknown_shape():
    rows = [{'product_id': '9', 'issue': 'COLOR_MISMATCH', 'current_name': 'x', 'current_fields': '',
             'decision': 'approved', 'proposed_fix': 'do something mysterious', 'triage': 'CLEAR'}]
    ops, errors = cao.compile_judgment(rows)
    assert errors and '9' in errors[0]


# ── round-2 fixes: extended whitelist + field-op optimistic lock ────────────
# (product-naming-round2, code-review fix package, item 8)

def test_whitelist_extended_to_structured_sync_columns():
    for col in ('size', 'packaging_th', 'packaging_short', 'series',
                'condition', 'pack_variant', 'sub_category_short_code'):
        assert col in apn._FIELD_WHITELIST
    # originals still present
    for col in ('color_code', 'brand_id', 'model'):
        assert col in apn._FIELD_WHITELIST


def test_field_op_empty_before_is_backward_compatible_no_lock(db):
    """Round-1 legacy field ops always leave 'before' empty (cosmetic/preview
    only, never an assertion) — this must keep working unchanged even though
    field ops now support an optimistic lock. Pins the exact scenario
    test_apply_name_field_dict_ops already covers, isolated to the field op."""
    _add_product(db, 10, 'x', 'S10')  # color_code is NULL
    ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
            'before': '', 'after': 'SB', 'source': 't'}]
    res = apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    assert res['applied'] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT color_code FROM products WHERE id=10").fetchone()[0] == 'SB'


def test_field_op_populated_before_stale_rolls_back(db):
    """A field op that DOES carry a real 'before' now gets the same
    optimistic lock name ops already have: current DB value must match."""
    _add_product(db, 10, 'x', 'S10', color='AC')  # DB actually has AC, not the SN the op expects
    ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
            'before': 'SN', 'after': 'SB', 'source': 't'}]
    with pytest.raises(apn.ApplyError):
        apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT color_code FROM products WHERE id=10").fetchone()[0] == 'AC'
    assert conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 0


def test_field_op_populated_before_matching_current_succeeds(db):
    _add_product(db, 10, 'x', 'S10', color='AC')
    ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
            'before': 'AC', 'after': 'SB', 'source': 't'}]
    res = apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    assert res['applied'] == 1


def test_field_op_null_sentinel_before_matches_genuine_null(db):
    _add_product(db, 10, 'x', 'S10')  # color_code NULL
    ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
            'before': 'NULL', 'after': 'SB', 'source': 't'}]
    res = apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))
    assert res['applied'] == 1


def test_field_op_null_sentinel_before_rejects_non_null_current(db):
    _add_product(db, 10, 'x', 'S10', color='AC')  # NOT null
    ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
            'before': 'NULL', 'after': 'SB', 'source': 't'}]
    with pytest.raises(apn.ApplyError):
        apn.apply_ops(db, ops, dry_run=False, backup_dir=os.path.dirname(db))


def test_validate_ops_dry_run_catches_stale_field_before():
    """validate_ops (the --dry-run path) must catch a stale field 'before'
    too, mirroring its existing name-op staleness check — same coverage,
    earlier surfacing."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 't.db')
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO products (id, product_name, color_code) VALUES (10, 'x', 'AC')")
        conn.commit()
        conn.close()
        ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
                'before': 'SN', 'after': 'SB', 'source': 't'}]
        errs = apn.validate_ops(path, ops)
        assert errs and '10' in errs[0]


def test_validate_ops_dry_run_passes_empty_before_field_op():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 't.db')
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO products (id, product_name, color_code) VALUES (10, 'x', 'AC')")
        conn.commit()
        conn.close()
        ops = [{'op': 'field', 'product_id': '10', 'field': 'color_code', 'value': 'SB',
                'before': '', 'after': 'SB', 'source': 't'}]
        errs = apn.validate_ops(path, ops)
        assert errs == []
