"""#610: every importer that writes a unit translates it through the unit map.

The writers #598's census left `pending:#610`:
  - the DBF sales-order-lines writer (express_sales_order_lines, replaced from
    raw TQUCOD on every upload),
  - both credit-note writers (express_credit_note_lines via
    scripts/import_express.py, credit_note_imports via import_credit_notes.py),
  - the supplier catalogue importer, which gets Sendy's spelling variants only
    (book '*'), never an Express code: 64 prod rows write `ขด` to mean a coil of
    rope, which Express's `ขด` (ขีด) would silently rename.

Each test asserts the stored word (external behaviour), with a CONTROL in the
same run: a spelling the map does not know is stored as written, so a writer
that blanked or constant-ed the column cannot pass. `empty_db` starts with an
EMPTY unit_map, so every test seeds exactly the rows it asserts on.
"""
import ast
import datetime
import os
import sqlite3
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, 'scripts')
_APP = os.path.join(_ROOT, 'inventory_app')
# scripts/ goes on sys.path LAST, never insert(0) (#489).
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)

MAP_ROWS = [
    ('BSN5657', 'หล', 'โหล'),
    ('BSN5657', 'ช5', 'ชุด5'),
    ('BSN5657', 'หอ', 'ห่อ'),
    ('BSN5657', 'ขด', 'ขีด'),
    ('xp5', 'หอ', 'หลอด'),
    ('xp5', 'หล', 'โหล'),
    ('*', 'กิโล', 'กิโลกรัม'),
    ('*', 'แพค', 'แพ็ค'),
]


def _seed_map(db):
    conn = sqlite3.connect(db)
    try:
        conn.executemany("INSERT INTO unit_map (book, spelling, word) VALUES (?, ?, ?)",
                         MAP_ROWS)
        conn.commit()
    finally:
        conn.close()


def _q(db, sql, args=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ── DBF sales-order lines (express_sales_order_lines) ────────────────────────

def _oeso(sonum):
    return {'SONUM': sonum, 'SODAT': datetime.date(2026, 9, 1), 'CUSCOD': '01ว18',
            'SLMCOD': '02', 'YOUREF': '', 'PAYTRM': 30, 'DLVDAT': None,
            'CMPLDAT': None, 'TOTAL': 100.0, 'DISCAMT': 0.0, 'VATAMT': 0.0,
            'NETAMT': 100.0, 'DOCSTAT': 'N'}


def _oesoit(sonum, seq, tqucod):
    return {'SONUM': sonum, 'SEQNUM': seq, 'STKCOD': f'X{seq}', 'STKDES': 'ของ',
            'ORDQTY': 1.0, 'CANCELQTY': 0.0, 'REMQTY': 0.0, 'TQUCOD': tqucod,
            'UNITPR': 10.0, 'TRNVAL': 10.0}


def _patch_dbf(monkeypatch, tables):
    import express_dbf_source as eds

    def fake(_dir, name):
        key = name.upper()
        if key in tables:
            return list(tables[key])
        if key in ('OESO', 'OESOIT', 'BKTRN', 'ARBIL', 'GLACC', 'GLJNL', 'GLJNLIT'):
            raise FileNotFoundError(f'{key}.DBF')
        return []
    monkeypatch.setattr(eds, 'open_table', fake)


def _seed_company(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO companies (code, name_th, short_name) "
                      "VALUES ('BSN', 'บุญสวัสดิ์ นำชัย', 'BSN') ON CONFLICT(code) DO NOTHING")
        conn.commit()
    finally:
        conn.close()


def _order_units(db):
    return dict(_q(db, "SELECT line_seq, unit FROM express_sales_order_lines "
                       "WHERE so_no = 'SO610' ORDER BY line_seq"))


@pytest.mark.parametrize('book,hoo', [(None, 'ห่อ'), ('BSN5657', 'ห่อ'), ('xp5', 'หลอด')])
def test_dbf_sales_order_lines_store_the_word_of_their_book(empty_db, monkeypatch, book, hoo):
    """The census control for `express_registers.py::replace` (through_map_
    transitive): commit_express_dbf, the one caller that hands it a unit
    column, translates every line against the upload's own book first. `หอ`
    is the one code the two books disagree on, so it proves the book is
    threaded, not defaulted."""
    import import_router
    _seed_map(empty_db)
    _seed_company(empty_db)
    _patch_dbf(monkeypatch, {
        'OESO': [_oeso('SO610')],
        'OESOIT': [_oesoit('SO610', 1, 'หล'), _oesoit('SO610', 2, 'ช5'),
                   _oesoit('SO610', 3, 'หอ'), _oesoit('SO610', 4, 'ZZ'),
                   _oesoit('SO610', 5, '')],
    })

    out = import_router.commit_express_dbf('/x', db_path=empty_db,
                                           snapshot_date='2026-09-22', book=book)

    assert out['sales_orders'] == {'orders': 1, 'lines': 5}, out['sales_orders']
    units = _order_units(empty_db)
    assert units[1] == 'โหล'
    assert units[3] == hoo
    assert units[4] == 'ZZ'          # CONTROL: an unknown code is stored as sent
    assert units[5] == ''            # an empty TQUCOD stays empty
    # ช5 is a BSN5657 code only; xp5's own unit list does not carry it
    assert units[2] == ('ช5' if book == 'xp5' else 'ชุด5')


def test_only_commit_express_dbf_replaces_the_sales_order_register():
    """The transitive claim rests on commit_express_dbf being the ONLY code
    path that hands express_registers.replace() the sales-order register (the
    one register with a unit column). A second caller would bypass the
    translation while the test above stayed green."""
    callers = []
    for base in (_APP, _SCRIPTS):
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'static', 'tests')]
            for n in names:
                if not n.endswith('.py'):
                    continue
                path = os.path.join(root, n)
                with open(path, encoding='utf-8') as f:
                    tree = ast.parse(f.read())
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for call in ast.walk(fn):
                        if (isinstance(call, ast.Call)
                                and isinstance(call.func, ast.Attribute)
                                and call.func.attr == 'replace'
                                and call.args
                                and isinstance(call.args[0], ast.Constant)
                                and call.args[0].value == 'sales_orders'):
                            callers.append((os.path.relpath(path, _ROOT), fn.name))
    assert callers == [('inventory_app/import_router.py', 'commit_express_dbf')], callers


# ── credit notes, AP side (express_credit_note_lines) ────────────────────────

def _cn_record(doc_no, units):
    return {
        'doc_no': doc_no, 'date_iso': '2026-09-01', 'supplier_name': 'ซัพพลายเออร์ 610',
        'ref_doc': 'RR610', 'v_flag': 0, 'discount': 0.0, 'vat': 0.0, 'total': 10.0,
        'is_cleared': False, 'is_void': False, 'type_code': None, 'note': '',
        'lines': [{'line_no': i, 'product_code': f'P{i}', 'product_name': 'ของ',
                   'qty': 1.0, 'unit': u, 'unit_price': 10.0, 'discount': '',
                   'line_total': 10.0, 'is_cleared': False}
                  for i, u in enumerate(units, start=1)],
    }


def _cn_units(db, doc_no):
    return [r[0] for r in _q(
        db, "SELECT l.unit FROM express_credit_note_lines l JOIN express_credit_notes h "
            "ON h.id = l.credit_note_id WHERE h.doc_no = ? ORDER BY l.line_no", (doc_no,))]


@pytest.mark.parametrize('book,hoo', [(None, 'ห่อ'), ('xp5', 'หลอด')])
def test_credit_note_lines_store_the_word_of_their_book(empty_db, book, hoo):
    """The DBF path (run_import_records, as commit_express_dbf calls it for
    both books) and the text-report path share one writer."""
    import import_express as ie
    _seed_map(empty_db)
    _seed_company(empty_db)
    kwargs = {'book': book} if book else {}

    ie.run_import_records('credit_notes', [_cn_record('GR610', ['หล', 'หอ', 'กิโล', 'ZZ'])],
                          db_path=empty_db, **kwargs)

    assert _cn_units(empty_db, 'GR610') == ['โหล', hoo, 'กิโลกรัม', 'ZZ']   # ZZ: CONTROL


def test_commit_express_dbf_threads_its_book_to_the_credit_note_writer(empty_db, monkeypatch):
    """The VAT-book build calls commit_express_dbf(book=xp5); its credit-note
    lines must be read against xp5, not the default book."""
    import import_router
    import express_dbf_source as eds
    _seed_map(empty_db)
    _seed_company(empty_db)
    _patch_dbf(monkeypatch, {})
    monkeypatch.setattr(eds, 'build_credit_notes_ap_records',
                        lambda *a, **k: [_cn_record('GR611', ['หอ'])])

    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-09-22',
                                     book='xp5')

    assert _cn_units(empty_db, 'GR611') == ['หลอด']


def test_payments_out_refuses_a_book_it_does_not_use(empty_db):
    """`book` reaches only the credit-note writer. Handing it to a records
    importer that has no unit column fails loudly instead of being ignored."""
    import import_express as ie
    _seed_company(empty_db)
    with pytest.raises(TypeError):
        ie.run_import_records('payments_out', [], db_path=empty_db, book='xp5')


# ── credit notes, AR side (credit_note_imports) ──────────────────────────────

_CN_HEADER = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                                                      หน้า   :        1"',
    '"  รายงานใบลดหนี้/รับคืนสินค้า\xa0เรียงตามเลขที่"',
    '"---------------------------------------------------------------------------------------------------------------------------------------------------------------"',
    '"   เลขที่       วันที่   ลูกค้า                               พนักงานขาย\xa0\xa0อ้างถึงใบกำกับ\xa0\xa0V  ส่วนลด     มูลค่าสินค้า     VAT.       รวมทั้งสิ้น ตัดหนี้แล้ว\xa0ประเภท"',
    '"---------------------------------------------------------------------------------------------------------------------------------------------------------------"',
]


def _sr(doc, unit):
    return [
        f'"  {doc}    10/01/67  ร้านทดสอบC                           31         IV8800300    1                   750.00         0.00        750.00        Y      2"',
        f'"     Y   1 031บ4124\xa0\xa0ใบตัดเพชร\xa04.5"                   3.00{unit:<16}250.00                   750.00                                IV8800300-  1"',
        '',
    ]


def test_credit_note_imports_store_the_word(empty_db, tmp_path):
    import import_credit_notes as icn
    _seed_map(empty_db)
    path = tmp_path / 'ใบลดหนี้_610.csv'
    path.write_text('\n'.join(_CN_HEADER + _sr('SR6100001', 'หล') + _sr('SR6100002', 'ZZ'))
                    + '\n', encoding='cp874')

    res = icn.import_credit_notes(str(path), db_path=empty_db)

    assert res['new_recorded'] == 2, res
    units = dict(_q(empty_db, "SELECT doc_base, unit FROM credit_note_imports"))
    assert units == {'SR6100001': 'โหล', 'SR6100002': 'ZZ'}      # ZZ: CONTROL


# ── supplier catalogue: Sendy spellings only ─────────────────────────────────

def _supplier_row(name, unit):
    return {'name_raw': name, 'name_normalized': name, 'name_tokens': '[]',
            'category_hint': None, 'sheet_name': 'ก', 'unit': unit,
            'min_order_qty': None, 'list_price': 10.0, 'trade_discount_pct': None,
            'cash_discount_pct': None, 'net_cash_price': 10.0, 'price_change_flag': 'same'}


def test_supplier_catalogue_writes_sendy_spellings_never_express_codes(empty_db):
    """`กิโล` and `แพค` are Sendy spelling variants (book '*') and become the
    word. `ขด` and `หล` are Express CODES: a supplier writing `ขด` means a coil
    of rope, not ขีด, so both are stored as the supplier wrote them."""
    import import_supplier_catalogue as isc
    _seed_map(empty_db)
    conn = sqlite3.connect(empty_db)
    try:
        sid = conn.execute("INSERT INTO suppliers (name) VALUES ('ผู้ขาย 610')").lastrowid
        vid = conn.execute("INSERT INTO supplier_catalogue_versions (supplier_id, source_file) "
                           "VALUES (?, 'x.xlsx')", (sid,)).lastrowid
        for name, unit in (('ตะปู', 'กิโล'), ('เชือก', 'ขด'), ('ลวด', 'หล'),
                           ('สี', 'แพค'), ('ถาด', 'ปอนด์')):
            isc.upsert_item(conn, sid, vid, _supplier_row(name, unit))
        # the UPDATE branch too: a re-import of an existing item
        isc.upsert_item(conn, sid, vid, _supplier_row('ตะปู', 'กิโล'))
        conn.commit()
    finally:
        conn.close()

    want = {'ตะปู': 'กิโลกรัม', 'สี': 'แพ็ค', 'เชือก': 'ขด', 'ลวด': 'หล', 'ถาด': 'ปอนด์'}
    assert dict(_q(empty_db, "SELECT name_raw, unit FROM supplier_catalogue_items")) == want
    assert dict(_q(empty_db, "SELECT i.name_raw, h.unit FROM supplier_catalogue_price_history h "
                             "JOIN supplier_catalogue_items i ON i.id = h.item_id")) == want
