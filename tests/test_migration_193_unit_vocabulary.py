"""Migration 193 — GH #610 (#595 · 7/7): the unit map learns the approved
vocabulary, and every stored unit the importers now translate is translated
in the same change.

The invariant (the #609 blocker, erp-engineering-discipline.md "A migration
that normalises stored values must write exactly what the IMPORTER would
write"): a stored unit only ever becomes what `bsn_units.normalize_unit`
produces for it, in the book its column is read against. Anything else and
the next import sees the stored word differ from the raw code, rewrites the
line, loses its conversion and moves stock (pid 436 +70 on a prod copy).

A second guard is new here. Translating a line can change what it RESOLVES
to without any conversion disagreeing: pid 436's two unsynced `ช3` lines have
no `ช3` conversion but the product has a `ชุด3` one, so translating them would
make them syncable and move stock on the next import. 193 translates a bill
line only when its `_get_base_qty` ratio is unchanged (COGS reads the same
ratio, with 1 for none); the others keep their spelling and are listed in
migration_193_skipped.

Assert external behaviour only: the word on a row, what a re-import does,
stock before vs after. `tmp_db` clones the live dev DB WITH its data, so
every test seeds exactly the rows it needs.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import bsn_units
import database
from tests.test_migration_186_unit_code_cleanup import (
    _entry, _new_product, _purchase, _sale, _uc, _units)

MIG = '193_unit_vocabulary.sql'
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MIG_193 = os.path.join(REPO, 'data', 'migrations', MIG)
ROLLBACK_193 = os.path.join(REPO, 'data', 'migrations', '193_unit_vocabulary.rollback.sql')

# The approved vocabulary (#595's word list, #610's body), per book. Read
# against each book's own ISTAB (TABTYP '20') on 2026-09-22: every Express
# code below is listed in that book with exactly this meaning. The '*' rows
# are Sendy spelling variants, not Express codes.
APPROVED = {
    ('BSN5657', 'ช5'): 'ชุด5', ('BSN5657', 'ช3'): 'ชุด3', ('BSN5657', 'คค'): 'ครั้ง',
    ('BSN5657', 'ใบ'): 'ใบ', ('BSN5657', 'ขว'): 'ขวด', ('BSN5657', 'คร'): 'เครื่อง',
    ('BSN5657', 'ดม'): 'ด้าม', ('BSN5657', 'มด'): 'เม็ด', ('BSN5657', 'เม'): 'เมตร',
    ('BSN5657', 'ตล'): 'ตลับ', ('BSN5657', 'ปป'): 'ปิ๊ป', ('BSN5657', 'ทน'): 'แท่น',
    ('BSN5657', 'หบ'): 'หีบ', ('BSN5657', 'บา'): 'บาน', ('BSN5657', 'เก'): 'เกล็ด',
    ('BSN5657', 'คล'): 'ครึ่งโล',
    ('xp5', 'ขว'): 'ขวด', ('xp5', 'คร'): 'เครื่อง', ('xp5', 'ตล'): 'ตลับ',
    ('xp5', 'ทน'): 'แท่น', ('xp5', 'ใบ'): 'ใบ',
    ('*', 'กิโล'): 'กิโลกรัม', ('*', 'กก.'): 'กิโลกรัม', ('*', '1กิโล'): 'กิโลกรัม',
    ('*', 'กล.เล็ก'): 'กล่องเล็ก', ('*', 'แพค'): 'แพ็ค',
}

# Independent oracle: each book's ISTAB unit list (TYPCOD -> TYPDES, or
# SHORTNAM where TYPDES is blank), dumped 2026-09-22 from
# projects/express-integration/data/{BSN5657,xp5}. Never read by runtime code.
ISTAB = {
    'BSN5657': {
        'กล': 'กล่อง', 'คร': 'เครื่อง', 'ชด': 'ชุด', 'ชน': 'ชิ้น', 'ตว': 'ตัว', 'หบ': 'หีบ',
        'หล': 'โหล', 'หด': 'หลอด', 'อน': 'อัน', 'ผง': 'แผง', 'ปน': 'ปื้น', 'แก': 'แกลลอน',
        'ผน': 'แผ่น', 'กร': 'กุรุส', 'มน': 'ม้วน', 'กน': 'ก้อน', 'ทง': 'แท่ง', 'ใบ': 'ใบ',
        'ดก': 'ดอก', 'กป': 'กระป๋อง', 'ขว': 'ขวด', 'ลก': 'ลูก', 'คู': 'คู่', 'ลง': 'ลัง',
        'กก': 'กิโล', 'หค': 'โหลคู่', 'ซง': 'ซอง', 'สน': 'เส้น', 'แพ': 'แพค', 'ถง': 'ถัง',
        'คค': 'ครั้ง', 'ตล': 'ตลับ', 'ปป': 'ปิ๊ป', 'กส': 'กระสอบ', 'บา': 'บาน', 'ถุ': 'ถุง',
        'ทน': 'แท่น', 'หอ': 'ห่อ', 'คน': 'คัน', 'เก': 'เกล็ด', 'ดม': 'ด้าม', 'ผื': 'ผืน',
        'บล': 'บล็อก', 'ขด': 'ขีด', 'คล': 'ครึ่งโล', 'เม': 'เมตร', 'มด': 'เม็ด',
        'ช5': 'ชุด5', 'ช3': 'ชุด3',
    },
    'xp5': {
        'กล': 'กล่อง', 'คร': 'เครื่อง', 'ชด': 'ชุด', 'ชน': 'ชิ้น', 'ตว': 'ตัว', 'หล': 'โหล',
        'หอ': 'หลอด', 'อน': 'อัน', 'ผง': 'แผง', 'ปน': 'ปื้น', 'แก': 'แกลลอน', 'ผน': 'แผ่น',
        'กร': 'กุรุส', 'มน': 'ม้วน', 'กน': 'ก้อน', 'ทง': 'แท่ง', 'ใบ': 'ใบ', 'ดก': 'ดอก',
        'กป': 'กระป๋อง', 'ขว': 'ขวด', 'ลก': 'ลูก', 'คู': 'คู่', 'ลง': 'ลัง', 'กก': 'กิโล',
        'หค': 'โหลคู่', 'ซง': 'ซอง', 'สน': 'เส้น', 'แพ': 'แพค', 'ถง': 'ถัง', 'ตล': 'ตลับ',
        'ทน': 'แท่น', 'ดว': 'ดวง', 'คน': 'คัน', 'ขด': 'ขีด',
    },
}
# Sendy's spelling of an Express meaning (ADR 0018: Express decides the
# meaning, Sendy the spelling).
SENDY_SPELLING = {'กิโล': 'กิโลกรัม', 'แพค': 'แพ็ค'}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _block(sql, name):
    start = sql.index(f'-- >>> {name}')
    end = sql.index(f'-- <<< {name}')
    return sql[start:end]


@pytest.fixture
def pre193_db(tmp_db):
    """The true pre-193 state: once the live dev DB has booted this branch,
    `tmp_db`'s clone already carries 193, and a bare init_db() would skip it."""
    conn = sqlite3.connect(tmp_db)
    try:
        applied = {r[0] for r in conn.execute("SELECT filename FROM applied_migrations")}
        if MIG in applied:
            conn.executescript(_read(ROLLBACK_193))
            conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
            conn.commit()
    finally:
        conn.close()
    return tmp_db


def _migrate():
    database.init_db()


def _ledger(conn, pid):
    """(stock level, (ledger rows, ledger sum)) for one product; a product that
    never posted has no stock_levels row, read as 0."""
    stock = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return (stock[0] if stock else 0,
            tuple(conn.execute("SELECT COUNT(*), SUM(quantity_change) FROM transactions "
                               "WHERE product_id=?", (pid,)).fetchone()))


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


# ── the map ─────────────────────────────────────────────────────────────────

def test_the_map_learns_every_approved_entry(pre193_db):
    conn = _conn(pre193_db)
    try:
        missing = [k for k in APPROVED if bsn_units.translate(k[1], k[0], conn=conn) is not None]
    finally:
        conn.close()
    assert missing == [], f'already known before 193 -- the test cannot see 193 seed them: {missing}'

    _migrate()

    conn = _conn(pre193_db)
    try:
        wrong = {k: bsn_units.translate(k[1], k[0], conn=conn) for k, w in APPROVED.items()
                 if bsn_units.translate(k[1], k[0], conn=conn) != w}
        assert wrong == {}
        # the '*' variants reach every book (translate falls back to '*')
        for book in ('BSN5657', 'xp5'):
            for (b, s), w in APPROVED.items():
                if b == '*':
                    assert bsn_units.normalize_unit(s, book, conn=conn) == w, (book, s)
        # the one code the books disagree on is untouched
        assert bsn_units.translate('หอ', 'BSN5657', conn=conn) == 'ห่อ'
        assert bsn_units.translate('หอ', 'xp5', conn=conn) == 'หลอด'
        # a supplier's ขด (a coil) is not a Sendy spelling: '*' does not know it
        assert bsn_units.translate('ขด', bsn_units.BOOK_ANY, conn=conn) is None
        assert bsn_units.translate('ขด', 'BSN5657', conn=conn) == 'ขีด'     # control
        # ใบ is a code equal to its word: known, never rewritten
        assert bsn_units.is_known('ใบ', conn=conn) and bsn_units.normalize_unit('ใบ', conn=conn) == 'ใบ'
    finally:
        conn.close()


BANG = ('!กล', '!คู', '!ลก', '!หด', '!หล')


def test_the_five_bang_rows_leave_the_map(pre193_db):
    """#595's approved list removes them: `!` is a warning mark Express prints
    in text reports, not part of a unit, and the parsers strip it. Their exact
    rows are kept in migration_193_unit_map_removed for the rollback."""
    conn = _conn(pre193_db)
    try:
        before = {r['spelling']: tuple(r) for r in conn.execute(
            "SELECT id, book, spelling, word, created_at FROM unit_map WHERE spelling IN (?,?,?,?,?)",
            BANG)}
    finally:
        conn.close()
    assert sorted(before) == sorted(BANG), 'the clone lacks the ! rows -- the test cannot see 193 remove them'

    _migrate()

    conn = _conn(pre193_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM unit_map WHERE spelling IN (?,?,?,?,?)",
                            BANG).fetchone()[0] == 0
        for code in BANG:
            assert bsn_units.translate(code, 'BSN5657', conn=conn) is None, code
        assert sorted(tuple(r) for r in conn.execute(
            "SELECT id, book, spelling, word, created_at FROM migration_193_unit_map_removed")) == \
            sorted(before.values())
        assert bsn_units.translate('หล', 'BSN5657', conn=conn) == 'โหล'      # CONTROL: the code itself stays
    finally:
        conn.close()


def test_a_stored_bang_code_aborts(pre193_db):
    """They match nothing on prod. If one ever does, removing its map row would
    make the next import rewrite that line (the #609 shape), so 193 refuses."""
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 bang line')
    _sale(conn, 'IV193BANG-1', pid, '!หล')
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: a stored unit still holds a ! code')


def test_every_code_in_both_books_unit_lists_is_known(pre193_db):
    """User story 19: the map holds every code in both books' unit lists, so a
    rare code is translated on the first bill that uses it. Checked against
    the ISTAB oracle above, never against the migration's own list."""
    _migrate()
    conn = _conn(pre193_db)
    try:
        bad = {}
        for book, codes in ISTAB.items():
            for code, meaning in codes.items():
                want = SENDY_SPELLING.get(meaning, meaning)
                got = bsn_units.translate(code, book, conn=conn)
                if got != want:
                    bad[(book, code)] = (got, want)
        assert bad == {}
        assert sum(len(c) for c in ISTAB.values()) == 83      # control: 49 + 34 codes checked
    finally:
        conn.close()


def test_both_books_agree_on_every_spelling_193_adds(pre193_db):
    """#610 seeds both books TOGETHER rather than threading the book into
    bsn_line._unit_same and bsn_sync's conversion-role check, which normalise
    with the default book even inside the VAT-book import. That choice is
    only safe while BSN5657 and xp5 give the same word for every spelling
    193 adds; this pins it. (หอ/ดว, #601's, are the known exceptions and are
    not 193's.)"""
    _migrate()
    conn = _conn(pre193_db)
    try:
        spellings = {s for (_b, s) in APPROVED}
        disagree = {s: (bsn_units.normalize_unit(s, 'BSN5657', conn=conn),
                        bsn_units.normalize_unit(s, 'xp5', conn=conn))
                    for s in spellings
                    if bsn_units.translate(s, 'xp5', conn=conn) is not None
                    and bsn_units.normalize_unit(s, 'BSN5657', conn=conn)
                    != bsn_units.normalize_unit(s, 'xp5', conn=conn)}
        assert disagree == {}
        # control: the check really compared xp5 translations
        assert bsn_units.translate('ขว', 'xp5', conn=conn) == 'ขวด'
    finally:
        conn.close()


def test_migration_map_equals_the_runtime_map_both_directions(pre193_db):
    """THE INVARIANT. The pairs the migration translates by, per source, must
    equal what bsn_units' public API produces for every spelling the map
    holds -- nothing it translates that the importer would not, nothing the
    importer translates that it leaves behind. Read by executing the
    migration's own map block after the seeds landed (no regex over SQL)."""
    _migrate()
    conn = _conn(pre193_db)
    try:
        # A '*' row for a spelling a book also holds: translate() lets the book
        # row win, so the migration must too (a flipped precedence is otherwise
        # invisible on today's map, where no such pair disagrees).
        conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('*', 'หล', 'โหลพิเศษ')")
        conn.executescript(_block(_read(MIG_193), 'mig193 map'))
        embedded = {(r['source'], r['spelling']): r['word']
                    for r in conn.execute("SELECT source, spelling, word FROM temp._mig193_map")}

        derived = {}
        for source in ('BSN5657', bsn_units.BOOK_ANY):
            for spelling in bsn_units.load_unit_map(source, conn=conn):
                word = bsn_units.normalize_unit(spelling, source, conn=conn)
                if word != spelling:
                    derived[(source, spelling)] = word
        assert derived[('BSN5657', 'ช5')] == 'ชุด5' and derived[('*', 'กิโล')] == 'กิโลกรัม'  # control
        assert derived[('BSN5657', 'หล')] == 'โหล' and derived[('*', 'หล')] == 'โหลพิเศษ'
        assert sorted(set(embedded.items()) - set(derived.items())) == [], \
            'the migration translates a spelling to a word the importer never produces'
        assert sorted(set(derived.items()) - set(embedded.items())) == [], \
            'the importer translates a spelling the migration leaves behind'
        # a translated word must survive a second normalize, or _unit_same flags it
        assert {(s, w) for (s, _sp), w in embedded.items()
                if bsn_units.normalize_unit(w, s, conn=conn) != w} == set()
    finally:
        conn.close()


# The columns 193 covers and the book each is read against.
BSN_COLUMNS = (
    ('products', 'unit_type'), ('promotions', 'bundle_unit'),
    ('pending_product_suggestions', 'bsn_unit'),
    ('pending_product_suggestions', 'suggested_unit_type'),
    ('credit_note_imports', 'unit'), ('express_sales', 'unit'),
    ('express_sales_order_lines', 'unit'), ('express_credit_note_lines', 'unit'),
)
SUPPLIER_COLUMNS = (('supplier_catalogue_items', 'unit'),
                    ('supplier_catalogue_price_history', 'unit'))


def _plant(conn, table, col, value, n):
    """One row holding `value` in `table.col`, with just enough else to insert."""
    if table == 'products':
        return _new_product(conn, f'mig193 plant {n}', unit_type=value)
    if table == 'promotions':
        pid = _new_product(conn, f'mig193 promo plant {n}')
        return conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, bundle_buy, bundle_free, "
            "bundle_unit, date_start, is_active) VALUES (?, 'mig193', 'bundle', 10, 1, ?, "
            "'2031-01-01', 1)",
            (pid, value)).lastrowid
    if table == 'pending_product_suggestions':
        other = 'suggested_unit_type' if col == 'bsn_unit' else 'bsn_unit'
        return conn.execute(
            f"INSERT INTO pending_product_suggestions (bsn_code, bsn_name, {col}, {other}) "
            f"VALUES (?, 'x', ?, 'อัน')", (f'T193-{col}-{n}', value)).lastrowid
    if table == 'credit_note_imports':
        return conn.execute(
            "INSERT INTO credit_note_imports (doc_no, doc_base, date_iso, unit) "
            "VALUES (?, ?, '2026-01-01', ?)", (f'SR193{n}-1', f'SR193{n}', value)).lastrowid
    if table == 'express_sales':
        return conn.execute("INSERT INTO express_sales (batch_id, doc_no, line_no, doc_type, "
                            "date_iso, company_id, unit) VALUES (1, ?, 1, 'IV', '2026-01-01', 1, ?)",
                            (f'IV193{n}', value)).lastrowid
    if table == 'express_sales_order_lines':
        return conn.execute("INSERT INTO express_sales_order_lines (entity, so_no, line_seq, unit) "
                            "VALUES ('BSN', ?, 1, ?)", (f'SO193{n}', value)).lastrowid
    if table == 'express_credit_note_lines':
        return conn.execute("INSERT INTO express_credit_note_lines (credit_note_id, line_no, unit) "
                            "VALUES (999999, ?, ?)", (n, value)).lastrowid
    sid = conn.execute("INSERT OR IGNORE INTO suppliers (name) VALUES ('mig193 supplier')").lastrowid
    sid = conn.execute("SELECT id FROM suppliers WHERE name='mig193 supplier'").fetchone()[0]
    item = conn.execute("INSERT INTO supplier_catalogue_items (supplier_id, name_raw, name_normalized, "
                        "unit) VALUES (?, ?, ?, ?)", (sid, f'i{n}', f'i{n}', value)).lastrowid
    if table == 'supplier_catalogue_items':
        return item
    return conn.execute("INSERT INTO supplier_catalogue_price_history (item_id, version_id, unit) "
                        "VALUES (?, 1, ?)", (item, value)).lastrowid


def test_every_translated_row_holds_what_the_runtime_map_produces(pre193_db):
    """The invariant as BEHAVIOUR: plant every spelling the map holds (and one
    it does not) in every covered column, migrate, and require each row to
    hold exactly normalize_unit(raw, <that column's book>) -- supplier columns
    '*' only. Bill lines, conversions and tiers have their own tests below
    (their translation is conditional on resolution / collisions)."""
    conn = _conn(pre193_db)
    spellings = sorted({s for (_b, s) in APPROVED} | set(ISTAB['BSN5657']) | {'ZZ-unknown', 'ขด'})
    planted = []
    n = 0
    for table, col in BSN_COLUMNS + SUPPLIER_COLUMNS:
        for s in spellings:
            n += 1
            planted.append((table, col, _plant(conn, table, col, s, n), s))
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        wrong = []
        for table, col, rid, raw in planted:
            book = bsn_units.BOOK_ANY if (table, col) in SUPPLIER_COLUMNS else 'BSN5657'
            want = bsn_units.normalize_unit(raw, book, conn=conn)
            got = conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (rid,)).fetchone()[0]
            if got != want:
                wrong.append((table, col, raw, got, want))
        assert wrong == []
        # controls: the planting reached both kinds of column, both ways
        def val(t, c, raw):
            rid = next(r for tt, cc, r, s in planted if (tt, cc, s) == (t, c, raw))
            return conn.execute(f"SELECT {c} FROM {t} WHERE id=?", (rid,)).fetchone()[0]
        assert val('express_sales_order_lines', 'unit', 'ช5') == 'ชุด5'
        assert val('express_sales_order_lines', 'unit', 'กร') == 'กุรุส'
        assert val('supplier_catalogue_items', 'unit', 'กิโล') == 'กิโลกรัม'
        assert val('supplier_catalogue_items', 'unit', 'ขด') == 'ขด'      # a coil stays a coil
        assert val('supplier_catalogue_items', 'unit', 'หล') == 'หล'      # never an Express code
        assert val('products', 'unit_type', 'ZZ-unknown') == 'ZZ-unknown'
    finally:
        conn.close()


# ── the #609 blocker, kept as a test: re-import is a no-op after 193 ────────

# (tag, file_type, doc_no, raw unit, stored unit, unit_type, conversions, synced, word)
REIMPORT_CASES = [
    # pid 436 shape: synced ช5 line on its code-only conversion (rename)
    ('c5', 'sales', 'IV1935-1', 'ช5', 'ช5', 'อัน', {'ช5': 5.0, 'ชุด': 5.0, 'ชุด3': 3.0}, 1, 'ชุด5'),
    # pid 623 shape: code + word twin at the same ratio (merge)
    ('c5t', 'sales', 'IV1936-1', 'ช5', 'ช5', 'ดอก', {'ช5': 5.0, 'ชุด5': 5.0}, 1, 'ชุด5'),
    # pid 1823 shape: an SR line in กิโล, twin กิโล/กิโลกรัม on a กิโลกรัม product
    ('kilo', 'sales', 'SR6700088-1', 'กิโล', 'กิโล', 'กิโลกรัม',
     {'กิโล': 1.0, 'กิโลกรัม': 1.0}, 1, 'กิโลกรัม'),
    # pid 1211 shape: an unsynced คค line with no conversion at all
    ('kk', 'sales', 'HS1937-3', 'คค', 'คค', 'ตัว', {}, 0, 'ครั้ง'),
    # a purchase line whose code is new to the map
    ('bt', 'purchase', 'RR1938', 'ขว', 'ขว', 'ตัว', {'ขว': 12.0}, 1, 'ขวด'),
]


def _seed_line(conn, tag, file_type, doc_no, stored, unit_type, convs, synced):
    from models.bsn_sync import _sync_bsn_to_stock
    code = f'T193-{tag}'
    pid = _new_product(conn, f'mig193 reimport {tag}', unit_type=unit_type)
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
                 "VALUES (?, 'x', ?)", (code, pid))
    for unit, ratio in convs.items():
        _uc(conn, pid, unit, ratio)
    table = 'purchase_transactions' if file_type == 'purchase' else 'sales_transactions'
    if file_type == 'purchase':
        _purchase(conn, doc_no, pid, stored, qty=7, price=24, code=code, synced=0)
    else:
        _sale(conn, doc_no, pid, stored, qty=14, price=66, code=code, synced=0)
    if synced:
        _sync_bsn_to_stock(conn, table, file_type, product_ids=[pid])
    conn.commit()
    return pid, table


@pytest.mark.parametrize('tag,file_type,doc_no,raw,stored,unit_type,convs,synced,word', REIMPORT_CASES)
def test_reimport_of_raw_lines_is_unchanged_after_193(pre193_db, tag, file_type, doc_no, raw,
                                                       stored, unit_type, convs, synced, word):
    import models
    conn = _conn(pre193_db)
    conn.execute("DELETE FROM sales_transactions WHERE doc_no=?", (doc_no,))
    pid, table = _seed_line(conn, tag, file_type, doc_no, stored, unit_type, convs, synced)
    before = _ledger(conn, pid)
    if synced:
        assert before[1][0] >= 1, 'seed never posted to the ledger -- the test would be vacuous'
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    rows = conn.execute(f"SELECT * FROM {table} WHERE product_id=? ORDER BY id", (pid,)).fetchall()
    assert [r['unit'] for r in rows] == [word], 'the migration did not translate the stored line'
    entries = [_entry(r, file_type) for r in rows]
    entries[0]['unit'] = raw          # Express sends the RAW code again on the next upload
    conn.close()

    res = models.import_weekly(entries, file_type, 'mig193-reimport-test', apply_removals=False)

    conn = _conn(pre193_db)
    try:
        assert res['unchanged'] == 1, res
        assert res['overwritten'] == 0 and res['imported'] == 0, res
        assert _ledger(conn, pid) == before
        assert [tuple(r) for r in conn.execute(
            f"SELECT id, unit, synced_to_stock FROM {table} WHERE product_id=?", (pid,))] == [
            (rows[0]['id'], word, synced)]
    finally:
        conn.close()


def test_reimport_goes_red_with_a_stock_move_when_the_map_forgets_an_entry(pre193_db):
    """Break-it-once for the test above, kept as a control: remove ONE map entry
    after the migration (the importer no longer produces the word 193 stored)
    and the same pid-436-shaped line must be rewritten and move stock."""
    import models
    conn = _conn(pre193_db)
    pid, table = _seed_line(conn, 'forget', 'sales', 'IV1939-1', 'ช5', 'อัน', {'ช5': 5.0}, 1)
    before = _ledger(conn, pid)
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    conn.execute("DELETE FROM unit_map WHERE book='BSN5657' AND spelling='ช5'")
    conn.commit()
    row = conn.execute(f"SELECT * FROM {table} WHERE product_id=?", (pid,)).fetchone()
    assert row['unit'] == 'ชุด5'
    entry = _entry(row, 'sales')
    entry['unit'] = 'ช5'
    conn.close()

    res = models.import_weekly([entry], 'sales', 'mig193-forgotten-entry', apply_removals=False)

    assert res['overwritten'] == 1, res
    conn = _conn(pre193_db)
    try:
        after = _ledger(conn, pid)
    finally:
        conn.close()
    assert after[0] - before[0] == 14 * 5.0, 'stock did not move -- the control cannot see the blocker'


def test_a_dbf_upload_after_193_leaves_translated_order_lines_as_they_are(pre193_db, monkeypatch):
    """express_sales_order_lines is replaced from raw TQUCOD on every upload,
    so the migration's word and the writer's word must be the same one."""
    import datetime
    import express_dbf_source as eds
    import import_router
    codes = ['หล', 'ช5', 'กร', 'ขว', 'หอ', 'ZZ']
    conn = _conn(pre193_db)
    conn.execute("DELETE FROM express_sales_order_lines")
    conn.execute("DELETE FROM express_sales_orders")
    conn.execute("INSERT INTO express_sales_orders (entity, so_no) VALUES ('BSN', 'SO193')")
    for i, c in enumerate(codes, start=1):
        conn.execute("INSERT INTO express_sales_order_lines (entity, so_no, line_seq, unit) "
                     "VALUES ('BSN', 'SO193', ?, ?)", (i, c))
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    migrated = dict(conn.execute("SELECT line_seq, unit FROM express_sales_order_lines"))
    conn.close()
    assert migrated[2] == 'ชุด5' and migrated[6] == 'ZZ'          # control: 193 did translate

    oeso = [{'SONUM': 'SO193', 'SODAT': datetime.date(2026, 9, 1), 'CUSCOD': 'C', 'SLMCOD': '',
             'YOUREF': '', 'PAYTRM': 0, 'DLVDAT': None, 'CMPLDAT': None, 'TOTAL': 0.0,
             'DISCAMT': 0.0, 'VATAMT': 0.0, 'NETAMT': 0.0, 'DOCSTAT': 'N'}]
    oesoit = [{'SONUM': 'SO193', 'SEQNUM': i, 'STKCOD': 'X', 'STKDES': 'x', 'ORDQTY': 1.0,
               'CANCELQTY': 0.0, 'REMQTY': 0.0, 'TQUCOD': c, 'UNITPR': 1.0, 'TRNVAL': 1.0}
              for i, c in enumerate(codes, start=1)]
    group = {'OESO': oeso, 'OESOIT': oesoit}
    monkeypatch.setattr(eds, 'open_table', lambda _d, name: list(group.get(name.upper(), [])))
    import_router.commit_express_dbf('/x', since_days=1, snapshot_date='2026-09-22')

    conn = _conn(pre193_db)
    try:
        assert dict(conn.execute("SELECT line_seq, unit FROM express_sales_order_lines")) == migrated
    finally:
        conn.close()


def test_sales_order_lines_are_translated_without_a_snapshot(pre193_db):
    """The DBF upload replaces the whole register from TQUCOD every time, so a
    snapshot of its ~48.5k rows would only cost prod's small /data volume
    (~5 MB, ENOSPC this month) to restore codes the next upload writes as
    words anyway. CONTROL in the same run: a credit-note line, whose writer
    does not replace it wholesale, is snapshotted."""
    conn = _conn(pre193_db)
    so = conn.execute("INSERT INTO express_sales_order_lines (entity, so_no, line_seq, unit) "
                      "VALUES ('BSN', 'SO193NOSNAP', 1, 'หล')").lastrowid
    cn = conn.execute("INSERT INTO express_credit_note_lines (credit_note_id, line_no, unit) "
                      "VALUES (999999, 1, 'หล')").lastrowid
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        assert conn.execute("SELECT unit FROM express_sales_order_lines WHERE id=?", (so,)).fetchone()[0] == 'โหล'
        assert conn.execute("SELECT COUNT(*) FROM migration_193_snapshot "
                            "WHERE table_name = 'express_sales_order_lines'").fetchone()[0] == 0
        assert conn.execute("SELECT old_value, new_value FROM migration_193_snapshot "
                            "WHERE table_name = 'express_credit_note_lines' AND row_id = ?",
                            (cn,)).fetchone() == ('หล', 'โหล')              # CONTROL
    finally:
        conn.close()


def test_a_credit_note_reimport_after_193_writes_the_same_word(pre193_db):
    import sys
    scripts = os.path.join(REPO, 'scripts')
    if scripts not in sys.path:
        sys.path.append(scripts)
    import import_express as ie
    conn = _conn(pre193_db)
    cid = conn.execute("SELECT id FROM companies WHERE code='BSN'").fetchone()[0]
    hid = conn.execute("INSERT INTO express_credit_notes (batch_id, doc_no, date_iso, company_id, "
                       "supplier_name, total_amount) VALUES (1, 'GR1930001', '2026-01-01', ?, 's', 10)",
                       (cid,)).lastrowid
    for i, u in enumerate(('หล', 'กิโล', 'ZZ'), start=1):
        conn.execute("INSERT INTO express_credit_note_lines (credit_note_id, line_no, unit) "
                     "VALUES (?, ?, ?)", (hid, i, u))
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    migrated = [r[0] for r in conn.execute(
        "SELECT unit FROM express_credit_note_lines WHERE credit_note_id=? ORDER BY line_no", (hid,))]
    conn.close()
    assert migrated == ['โหล', 'กิโลกรัม', 'ZZ']

    record = {'doc_no': 'GR1930001', 'date_iso': '2026-01-01', 'supplier_name': 's', 'ref_doc': None,
              'discount': 0.0, 'vat': 0.0, 'total': 10.0, 'is_cleared': False, 'is_void': False,
              'type_code': None, 'note': '',
              'lines': [{'line_no': i, 'product_code': 'P', 'product_name': 'x', 'qty': 1.0,
                         'unit': u, 'unit_price': 1.0, 'discount': '', 'line_total': 1.0,
                         'is_cleared': False} for i, u in enumerate(('หล', 'กิโล', 'ZZ'), start=1)]}
    ie.run_import_records('credit_notes', [record], db_path=pre193_db, book='BSN5657')

    conn = _conn(pre193_db)
    try:
        assert [r[0] for r in conn.execute(
            "SELECT l.unit FROM express_credit_note_lines l JOIN express_credit_notes h "
            "ON h.id = l.credit_note_id WHERE h.doc_no='GR1930001' ORDER BY l.line_no")] == migrated
    finally:
        conn.close()


# ── bill lines: translated only when what they resolve to is unchanged ──────

def test_a_line_whose_resolution_would_change_keeps_its_spelling(pre193_db):
    """pid 436 on prod: two UNSYNCED ช3 lines, no ช3 conversion, but a ชุด3
    conversion at 3. As ชุด3 they would become syncable and deduct stock on
    the next import (COGS reads the same ratio, with 1 for nothing).
    Control in the same run: a ช3 line whose OWN conversion exists moves with
    it and is translated."""
    conn = _conn(pre193_db)
    held = _new_product(conn, 'mig193 held ช3', unit_type='อัน')
    _uc(conn, held, 'ชุด3', 3.0)
    held_line = _sale(conn, 'IV1931-2', held, 'ช3', synced=0)
    moved = _new_product(conn, 'mig193 moved ช3', unit_type='อัน')
    _uc(conn, moved, 'ช3', 3.0)
    moved_line = _sale(conn, 'IV1932-2', moved, 'ช3', synced=0)
    # the other way a word can newly resolve: it IS the product's own unit
    own = _new_product(conn, 'mig193 held คร', unit_type='เครื่อง')
    own_line = _sale(conn, 'IV1930-1', own, 'คร', synced=0)
    conn.commit()
    stock_before = dict(conn.execute("SELECT product_id, quantity FROM stock_levels"))
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        unit = lambda i: conn.execute("SELECT unit FROM sales_transactions WHERE id=?", (i,)).fetchone()[0]
        assert unit(held_line) == 'ช3'
        assert unit(own_line) == 'คร'
        assert unit(moved_line) == 'ชุด3'                                   # CONTROL
        assert _units(conn, moved) == {'ชุด3': 3.0}
        assert _units(conn, held) == {'ชุด3': 3.0}
        assert [tuple(r) for r in conn.execute(
            "SELECT table_name, row_id, unit, word FROM migration_193_skipped "
            "WHERE product_id IN (?, ?, ?) ORDER BY row_id", (held, moved, own))] == sorted([
            ('sales_transactions', held_line, 'ช3', 'ชุด3'),
            ('sales_transactions', own_line, 'คร', 'เครื่อง')], key=lambda r: r[1])
        assert dict(conn.execute("SELECT product_id, quantity FROM stock_levels")) == stock_before
    finally:
        conn.close()


def test_the_postcondition_aborts_when_a_line_would_resolve_differently(pre193_db, tmp_path):
    """Break-it-once for the skip rule, kept as a test: a copy of 193 whose
    skip step is removed must be refused by the resolution postcondition,
    with the DB left untouched."""
    sql = _read(MIG_193)
    skip = _block(sql, 'mig193 skip')
    mutated = sql.replace(skip, '-- >>> mig193 skip (removed)\n')
    assert _block(sql, 'mig193 skip') in sql and 'mig193 skip (removed)' in mutated
    conn = _conn(pre193_db)
    pid = _new_product(conn, 'mig193 postcondition ช3', unit_type='อัน')
    _uc(conn, pid, 'ชุด3', 3.0)
    line = _sale(conn, 'IV1933-2', pid, 'ช3', synced=0)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='mig 193 postcondition FAILED: a bill line'):
        conn.executescript(mutated)
    conn.rollback()
    try:
        assert conn.execute("SELECT unit FROM sales_transactions WHERE id=?", (line,)).fetchone()[0] == 'ช3'
        assert conn.execute("SELECT COUNT(*) FROM unit_map WHERE spelling='ช3'").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_leftover_postcondition_aborts_when_an_update_does_not_land(pre193_db):
    """The second postcondition is a net under every UPDATE: a copy of 193 that
    loses one (here the credit-note lines') must be refused, not commit a
    half-translated table."""
    sql = _read(MIG_193)
    lost = ("UPDATE express_credit_note_lines\n"
            "   SET unit = (SELECT word FROM _mig193_map\n"
            "                WHERE source = 'BSN5657' AND spelling = express_credit_note_lines.unit)\n"
            " WHERE unit IN (SELECT spelling FROM _mig193_map WHERE source = 'BSN5657');\n")
    assert sql.count(lost) == 1
    conn = _conn(pre193_db)
    line = conn.execute("INSERT INTO express_credit_note_lines (credit_note_id, line_no, unit) "
                        "VALUES (999999, 1, 'หล')").lastrowid
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='mig 193 postcondition FAILED: a covered column'):
        conn.executescript(sql.replace(lost, ''))
    conn.rollback()
    try:
        assert conn.execute("SELECT unit FROM express_credit_note_lines WHERE id=?",
                            (line,)).fetchone()[0] == 'หล'
    finally:
        conn.close()


def test_bill_lines_are_translated_through_the_declared_change_path(pre193_db):
    conn = _conn(pre193_db)
    pid = _new_product(conn, 'mig193 declared product')
    _uc(conn, pid, 'คร', 1.0)
    sid = _sale(conn, 'IV1934-1', pid, 'คร', price=100)
    pur = _purchase(conn, 'RR1934', pid, 'คร', price=80)
    conn.execute("UPDATE sales_transactions SET change_reason='old human reason here' WHERE id=?", (sid,))
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        for table, rid in (('sales_transactions', sid), ('purchase_transactions', pur)):
            row = conn.execute(f"SELECT unit, change_source, change_actor, change_token, "
                               f"change_reason FROM {table} WHERE id=?", (rid,)).fetchone()
            assert tuple(row)[:3] == ('เครื่อง', 'import', 'mig193-unit-vocabulary')
            assert row['change_token'] and row['change_reason'] is None
            audit = conn.execute(
                "SELECT user, change_source, change_reason FROM audit_log WHERE table_name=? "
                "AND row_id=? AND action='UPDATE' AND changed_fields LIKE '%\"unit\"%' "
                "ORDER BY id DESC LIMIT 1", (table, rid)).fetchone()
            assert tuple(audit) == ('mig193-unit-vocabulary', 'import', None)
    finally:
        conn.close()


# ── conversions, tiers, product units ───────────────────────────────────────

def test_conversions_move_with_their_rows(pre193_db):
    conn = _conn(pre193_db)
    twin = _new_product(conn, 'mig193 twin')
    _uc(conn, twin, 'ช5', 5.0)
    _uc(conn, twin, 'ชุด5', 5.0)
    rename = _new_product(conn, 'mig193 rename')
    _uc(conn, rename, 'ช5', 5.0)
    two = _new_product(conn, 'mig193 two spellings')
    keep = _uc(conn, two, 'กิโล', 1000.0)
    gone = _uc(conn, two, 'กก.', 1000.0)
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        assert _units(conn, twin) == {'ชุด5': 5.0}
        assert _units(conn, rename) == {'ชุด5': 5.0}
        assert [tuple(r) for r in conn.execute(
            "SELECT id, bsn_unit FROM unit_conversions WHERE product_id=?", (two,))] == [
            (keep, 'กิโลกรัม')]
        assert [tuple(r) for r in conn.execute(
            "SELECT id, bsn_unit FROM migration_193_uc_deleted WHERE product_id=?", (two,))] == [
            (gone, 'กก.')]
    finally:
        conn.close()


def test_gross_bucket_and_block_conversions_survive_193_unchanged(pre193_db):
    """กร/ถง/บล conversion keys are left alone (lead's ruling, 2026-09-22).
    After #600 no bill line reads them and each agrees with its กุรุส/ถัง/บล็อก
    twin, so they are harmless; #603's hasp plan pins 1187/1188's to
    {'กร': 1.0, 'ตัว': 1.0}, and merging them would make it refuse. Their
    clean-up is a follow-up after that rebase. CONTROL in the same run: a ช5
    twin (193's own vocabulary) still merges."""
    conn = _conn(pre193_db)
    kept = []
    for key, word, ratio in (('กร', 'กุรุส', 1.0), ('ถง', 'ถัง', 18.0), ('บล', 'บล็อก', 3.0)):
        twin = _new_product(conn, f'mig193 kept twin {key}')
        _uc(conn, twin, key, ratio)
        _uc(conn, twin, word, ratio)
        alone = _new_product(conn, f'mig193 kept alone {key}')
        _uc(conn, alone, key, ratio * 2)
        kept += [twin, alone]
    disagree = _new_product(conn, 'mig193 kept disagreeing กร')   # would abort as twin_ratio
    _uc(conn, disagree, 'กร', 144.0)
    _uc(conn, disagree, 'กุรุส', 1.0)
    kept.append(disagree)
    ctl = _new_product(conn, 'mig193 merged ช5 twin')
    _uc(conn, ctl, 'ช5', 5.0)
    _uc(conn, ctl, 'ชุด5', 5.0)
    conn.commit()
    q = ("SELECT id, product_id, bsn_unit, ratio, created_at FROM unit_conversions "
         "WHERE product_id IN (%s) ORDER BY id" % ','.join('?' * len(kept)))
    before = [tuple(r) for r in conn.execute(q, kept)]
    assert len(before) == 11
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        assert [tuple(r) for r in conn.execute(q, kept)] == before
        ids = [r[0] for r in before]
        marks = ','.join('?' * len(ids))
        assert conn.execute(f"SELECT COUNT(*) FROM migration_193_uc_deleted WHERE id IN ({marks})",
                            ids).fetchone()[0] == 0
        assert conn.execute(f"SELECT COUNT(*) FROM migration_193_snapshot WHERE table_name = "
                            f"'unit_conversions' AND row_id IN ({marks})", ids).fetchone()[0] == 0
        assert _units(conn, ctl) == {'ชุด5': 5.0}                          # CONTROL
    finally:
        conn.close()


def test_product_units_and_tier_labels_are_translated(pre193_db):
    conn = _conn(pre193_db)
    kg = _new_product(conn, 'mig193 kg', unit_type='กก.')
    box = _new_product(conn, 'mig193 small box', unit_type='กล.เล็ก')
    for label, price in (('1กิโล', 85.0), ('20 กิโล', 1600.0), ('ลัง', 1700.0),
                         ('1 โหล (special)', 5.0)):
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (kg, label, price))
    conn.commit()
    conn.close()

    _migrate()

    conn = _conn(pre193_db)
    try:
        assert conn.execute("SELECT unit_type FROM products WHERE id=?", (kg,)).fetchone()[0] == 'กิโลกรัม'
        assert conn.execute("SELECT unit_type FROM products WHERE id=?", (box,)).fetchone()[0] == 'กล่องเล็ก'
        tiers = dict(conn.execute("SELECT qty_label, price FROM product_price_tiers WHERE product_id=?",
                                  (kg,)))
        assert tiers == {'1กิโลกรัม': 85.0, '20 กิโลกรัม': 1600.0, 'ลัง': 1700.0,
                         '1 โหล (special)': 5.0}
        for label in tiers:     # the importer's own tier normaliser agrees with every label
            assert bsn_units.normalize_tier_label(label, conn=conn) == label
    finally:
        conn.close()


# ── preconditions: abort with the DB untouched ──────────────────────────────

def _assert_aborts_unchanged(db, message):
    conn = sqlite3.connect(db)
    tables = ('unit_conversions', 'product_code_mapping', 'product_price_tiers', 'products',
              'sales_transactions', 'purchase_transactions', 'unit_map')
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in tables}
    applied = conn.execute("SELECT filename FROM applied_migrations ORDER BY 1").fetchall()
    conn.close()

    with pytest.raises(Exception, match=message):
        database.init_db()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'migration_193%'"
                            ).fetchall() == []
        for t, rows in before.items():
            assert conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() == rows, t
        assert conn.execute("SELECT filename FROM applied_migrations ORDER BY 1").fetchall() == applied
    finally:
        conn.close()


def test_a_map_row_named_differently_aborts(pre193_db):
    """Put may have named ช5 on /unit-conversions already. A different word is
    a human decision the migration must not overwrite."""
    conn = sqlite3.connect(pre193_db)
    conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'ช5', 'ชุด')")
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: the unit map already')


def test_the_same_map_row_already_named_is_kept(pre193_db):
    conn = sqlite3.connect(pre193_db)
    rid = conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'ช5', 'ชุด5')"
                       ).lastrowid
    conn.commit()
    conn.close()

    _migrate()

    conn = sqlite3.connect(pre193_db)
    try:
        assert conn.execute("SELECT id FROM unit_map WHERE book='BSN5657' AND spelling='ช5'"
                            ).fetchall() == [(rid,)]
        assert ('BSN5657', 'ช5') not in set(conn.execute(
            "SELECT book, spelling FROM migration_193_unit_map_added"))
    finally:
        conn.close()


def test_disagreeing_twin_aborts(pre193_db):
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 twin conflict')
    _uc(conn, pid, 'ช5', 5.0)
    _uc(conn, pid, 'ชุด5', 6.0)
    _sale(conn, 'IV193ABORT-1', pid, 'คร')        # proves nothing else ran either
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: code and word conversion')


def test_two_spellings_for_one_word_at_different_ratios_abort(pre193_db):
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 two-spelling conflict')
    _uc(conn, pid, 'กิโล', 1.0)
    _uc(conn, pid, 'กก.', 1000.0)
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: two spellings for one word')


def test_unit_type_landing_on_a_non_unit_ratio_aborts(pre193_db):
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 unit_type ratio', unit_type='กก.')
    _uc(conn, pid, 'กิโลกรัม', 1000.0)
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: unit_type')


def test_mapping_collision_aborts(pre193_db):
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 mapping collision')
    for unit in ('ช5', 'ชุด5'):
        conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
                     "VALUES ('T193-PCM', 'x', ?, ?)", (pid, unit))
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: product_code_mapping')


def test_tier_label_collision_aborts(pre193_db):
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 tier collision')
    for label, price in (('1กิโล', 85.0), ('1กิโลกรัม', 90.0)):
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (pid, label, price))
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre193_db, 'mig 193 precondition FAILED: product_price_tiers')


# ── apply / rerun / rollback ────────────────────────────────────────────────

def test_runner_applies_once(pre193_db):
    _migrate()
    _migrate()
    conn = sqlite3.connect(pre193_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM applied_migrations WHERE filename=?",
                            (MIG,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_hand_rerun_keeps_the_first_runs_records(pre193_db):
    conn = sqlite3.connect(pre193_db)
    pid = _new_product(conn, 'mig193 rerun')
    _uc(conn, pid, 'ช5', 5.0)
    conn.commit()
    conn.close()
    _migrate()

    conn = sqlite3.connect(pre193_db)
    try:
        counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            'migration_193_snapshot', 'migration_193_unit_map_added', 'migration_193_uc_deleted',
            'migration_193_skipped')]
        assert counts[0] >= 1 and counts[1] >= 1
        conn.executescript(_read(MIG_193))
        assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            'migration_193_snapshot', 'migration_193_unit_map_added', 'migration_193_uc_deleted',
            'migration_193_skipped')] == counts
    finally:
        conn.close()


# express_sales_order_lines is left out on purpose: 193 does not snapshot it
# (the next DBF upload rewrites the register), so its rollback leaves the words;
# test_rollback_restores_data_exactly asserts that explicitly.
UNIT_COLUMNS = (('sales_transactions', 'unit'), ('purchase_transactions', 'unit'),
                ('products', 'unit_type'), ('promotions', 'bundle_unit'),
                ('product_code_mapping', 'bsn_unit'), ('pending_product_suggestions', 'bsn_unit'),
                ('pending_product_suggestions', 'suggested_unit_type'),
                ('credit_note_imports', 'unit'), ('express_sales', 'unit'),
                ('product_price_tiers', 'qty_label'),
                ('express_credit_note_lines', 'unit'), ('supplier_catalogue_items', 'unit'),
                ('supplier_catalogue_price_history', 'unit'))


def _state(conn):
    out = {f'{t}.{c}': conn.execute(f"SELECT id, {c} FROM {t} ORDER BY id").fetchall()
           for t, c in UNIT_COLUMNS}
    out['uc'] = conn.execute("SELECT id, product_id, bsn_unit, ratio, created_at "
                             "FROM unit_conversions ORDER BY id").fetchall()
    out['unit_map'] = conn.execute("SELECT id, book, spelling, word, created_at "
                                   "FROM unit_map ORDER BY id").fetchall()
    return out


def _run_rollback(conn):
    conn.executescript(_read(ROLLBACK_193))
    conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
    conn.commit()
    return conn.execute("SELECT table_name, row_id, reason FROM temp.mig193_rollback_skipped "
                        "ORDER BY 1, 2, 3").fetchall()


def _seed_rollback_fixture(conn):
    twin = _new_product(conn, 'mig193 rb twin')
    _uc(conn, twin, 'ช5', 5.0)
    _uc(conn, twin, 'ชุด5', 5.0)
    kg = _new_product(conn, 'mig193 rb kg', unit_type='กก.')
    _uc(conn, kg, 'กิโล', 1.0)
    _sale(conn, 'IV193RB-1', twin, 'ช5')
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?, '1กิโล', 9)",
                 (kg,))
    conn.execute("INSERT INTO express_sales_order_lines (entity, so_no, line_seq, unit) "
                 "VALUES ('BSN', 'SO193RB', 1, 'หล')")
    held = _new_product(conn, 'mig193 rb held', unit_type='อัน')
    _uc(conn, held, 'ชุด3', 3.0)
    _sale(conn, 'IV193RB-2', held, 'ช3', synced=0)
    conn.commit()
    return twin, kg


def test_rollback_restores_data_exactly(pre193_db):
    conn = sqlite3.connect(pre193_db)
    _seed_rollback_fixture(conn)
    before = _state(conn)
    schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2").fetchall()
    conn.close()

    _migrate()

    conn = sqlite3.connect(pre193_db)
    try:
        assert _state(conn) != before, 'migration made no change -- rollback check would be vacuous'
        assert _run_rollback(conn) == []
        assert _state(conn) == before
        # the register the next DBF upload rewrites keeps its words (no snapshot)
        assert conn.execute("SELECT unit FROM express_sales_order_lines "
                            "WHERE so_no = 'SO193RB'").fetchone()[0] == 'โหล'
        assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2").fetchall() == schema
    finally:
        conn.close()


def test_rollback_leaves_later_edits_alone(pre193_db):
    conn = sqlite3.connect(pre193_db)
    twin, kg = _seed_rollback_fixture(conn)
    conn.close()

    _migrate()

    conn = sqlite3.connect(pre193_db)
    try:
        # re-create the merged-away code row, edit a translated product unit,
        # and let Put rename a seeded map row
        _uc(conn, twin, 'ช5', 5.0)
        conn.execute("UPDATE products SET unit_type='ถุง' WHERE id=?", (kg,))
        conn.execute("UPDATE unit_map SET word='ชุด' WHERE book='BSN5657' AND spelling='ช3'")
        rebang = conn.execute("INSERT INTO unit_map (book, spelling, word) "
                              "VALUES ('BSN5657', '!หล', 'โหล')").lastrowid
        conn.commit()

        skipped = _run_rollback(conn)

        assert _units(conn, twin) == {'ช5': 5.0, 'ชุด5': 5.0}
        assert conn.execute("SELECT unit_type FROM products WHERE id=?", (kg,)).fetchone()[0] == 'ถุง'
        assert conn.execute("SELECT word FROM unit_map WHERE book='BSN5657' AND spelling='ช3'"
                            ).fetchall() == [('ชุด',)]
        assert sorted({(t, r) for t, _i, r in skipped}) == [
            ('products', 'changed after 193, left as is'),
            ('unit_conversions', 'code row exists again, not restored'),
            ('unit_map', 'changed after 193, left as is'),
            ('unit_map', 'value exists again, not restored'),
        ]
        assert conn.execute("SELECT id FROM unit_map WHERE spelling='!หล'").fetchall() == [(rebang,)]
        assert conn.execute("SELECT COUNT(*) FROM unit_map WHERE spelling IN ('!กล', '!คู', '!ลก', "
                            "'!หด')").fetchone()[0] == 4                     # the others came back
        # the untouched parts still roll back
        assert conn.execute("SELECT unit FROM sales_transactions WHERE doc_no='IV193RB-1'"
                            ).fetchone()[0] == 'ช5'
        assert conn.execute("SELECT COUNT(*) FROM unit_map WHERE spelling IN ('ช5', 'กิโล')"
                            ).fetchone()[0] == 0
    finally:
        conn.close()
