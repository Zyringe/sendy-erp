"""Migration 190 — GH #599 (#595 · 4/7): BSN5657's `กร` / `ถง` / `บล` take
Express's own meaning (กุรุส / ถัง / บล็อก), and every affected product gains a
conversion under the NEW word at the ratio its OLD reading resolves to.

Asserts external behaviour only: the word the map produces, the ratio a stored
row resolves at, what a real re-import does to stock, and the rows a rollback
puts back. `tmp_db` clones the live dev DB WITH its data, so every test forces
the rows it needs instead of inheriting them.

What this file CANNOT check: the CONTENT of the migration's embedded stock-code
list. It comes from the BSN5657 DBF snapshot, which lives outside this repo and
is not on CI — `scripts/derive_599_express_unit_codes.py` regenerates it and the
PR records that run. Only the list's SHAPE is pinned here.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import bsn_units
import database
import models
import sales_filters

MIG = '190_express_unit_meanings.sql'
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MIG_190 = os.path.join(REPO, 'data', 'migrations', MIG)
ROLLBACK_190 = os.path.join(REPO, 'data', 'migrations',
                            '190_express_unit_meanings.rollback.sql')

MEANINGS = {'กร': ('ตัว', 'กุรุส'), 'ถง': ('ถุง', 'ถัง'), 'บล': ('แผง', 'บล็อก')}


def _open(path):
    """models._get_base_qty / _sync_bsn_to_stock index rows by NAME."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _block(sql, name):
    """The text between `-- >>> <name>` and `-- <<< <name>` in the migration."""
    start = sql.index(f'-- >>> {name}')
    end = sql.index(f'-- <<< {name}')
    return sql[start:end]


def _embedded(name, table, cols):
    """Execute one of the migration's own blocks and read the rows back, so the
    test sees exactly what the migration will use (no regex over SQL)."""
    conn = sqlite3.connect(':memory:')
    try:
        conn.executescript(_block(_read(MIG_190), name))
        return [tuple(r) for r in conn.execute(f'SELECT {cols} FROM {table}')]
    finally:
        conn.close()


@pytest.fixture
def pre190_db(tmp_db):
    """A pre-190 DB whose OWN data the migration cannot see, so every test
    decides exactly what 190 finds.

    It deliberately does NOT run the rollback on the clone. The rollback is
    guarded: it refuses when a row 190 inserted has been edited since, and the
    live DB is ABOUT to hold such rows (#603 upserts 1187/1188 กุรุส to 144).
    Undoing 190 there would make every test in this file error, and even an
    unguarded undo cannot recover the true pre-190 state of rows a later
    change has touched. A re-run over post-#603 hasps would also trip
    code_vs_old (กร = 144 against a ตัว base) on data no test put there.

    So the fixture forces the state instead of inheriting it
    (erp-engineering-discipline.md, `tmp_db` clones the live DB WITH its data):
      * the three BSN5657 map rows back to the pre-190 words;
      * 190 unstamped, and its forensic tables dropped (a pre-190 DB has none);
      * every conversion under the three raw codes or the three new words
        deleted, which empties arm (a) and rules out new_conflict;
      * every mapping row for the migration's embedded stock codes deleted,
        which empties arm (b).
    What the clone's REAL rows do under 190 is the prod-copy rehearsal's job
    (PR #628), not this file's."""
    codes = [c for c, _b in _embedded('mig190 stkcod', '_mig190_stkcod', 'code, bsn_code')]
    stkcods = [b for _c, b in _embedded('mig190 stkcod', '_mig190_stkcod', 'code, bsn_code')]
    assert set(codes) == set(MEANINGS) and stkcods, 'the embedded list did not parse'
    conn = _open(tmp_db)
    try:
        for code, (old_word, _new) in MEANINGS.items():
            conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', ?, ?) "
                         "ON CONFLICT(book, spelling) DO UPDATE SET word = excluded.word",
                         (code, old_word))
        conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
        conn.execute("DROP TABLE IF EXISTS migration_190_snapshot")
        conn.execute("DROP TABLE IF EXISTS migration_190_uc_inserted")
        words = list(MEANINGS) + [new for _old, new in MEANINGS.values()]
        conn.execute(f"DELETE FROM unit_conversions WHERE bsn_unit IN ({','.join('?' * len(words))})",
                     words)
        conn.execute(f"DELETE FROM product_code_mapping WHERE bsn_code IN "
                     f"({','.join('?' * len(stkcods))})", stkcods)
        conn.commit()
    finally:
        conn.close()
    return tmp_db


def _product(conn, name, unit_type='ตัว', cost=10.0, base=20.0):
    return conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price, base_sell_price) "
        "VALUES (?,?,?,?)", (name, unit_type, cost, base)).lastrowid


def _uc(conn, pid, bsn_unit, ratio):
    return conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
        (pid, bsn_unit, ratio)).lastrowid


def _map_row(conn, pid, bsn_code, bsn_unit=''):
    """Force the mapping for this stock code onto the fixture's product. The
    live clone already maps most of the migration's real stock codes, and
    UNIQUE(bsn_code, bsn_unit) would refuse the insert -- force the state,
    never inherit it."""
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code = ? AND bsn_unit = ?",
                 (bsn_code, bsn_unit))
    return conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
        "VALUES (?,?,?,?)", (bsn_code, 'x', pid, bsn_unit)).lastrowid


def _sale(conn, doc_no, pid, unit, qty=1, price=10, code='C599', synced=1):
    return conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01',?,?,?,?,'x','cust','C1',?,?,?,0,'',?,?,?)",
        (doc_no, doc_no.rsplit('-', 1)[0], pid, code, qty, unit, price,
         qty * price, qty * price, synced)).lastrowid


def _units(conn, pid):
    return dict(conn.execute(
        "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)))


def _words(conn):
    return dict(conn.execute(
        "SELECT spelling, word FROM unit_map WHERE book='BSN5657' "
        "AND spelling IN ('กร','ถง','บล','หค','ตว','ถุ','ผง')"))


# A stock code the migration's embedded list really carries, per code. Read
# from the migration itself so a regenerated list cannot make these stale.
def _a_stkcod(code):
    rows = [b for c, b in _embedded('mig190 stkcod', '_mig190_stkcod', 'code, bsn_code')
            if c == code]
    assert rows, f'the embedded list has no stock code for {code}'
    return rows[0]


# ── the embedded evidence ───────────────────────────────────────────────────

def test_embedded_lists_have_the_shape_the_migration_needs():
    """SHAPE only — the stock-code list's content comes from a DBF this repo
    does not carry (see the module docstring)."""
    meanings = _embedded('mig190 meanings', '_mig190_meaning', 'code, old_word, new_word')
    assert {c: (o, n) for c, o, n in meanings} == MEANINGS

    stk = _embedded('mig190 stkcod', '_mig190_stkcod', 'code, bsn_code')
    assert {c for c, _ in stk} == set(MEANINGS), 'every code needs stock-card evidence'
    assert all(c.strip() and b.strip() for c, b in stk), 'a blank row would match nothing'
    assert len(stk) == len(set(stk)), 'duplicate (code, stock code) pairs'
    # control: the block really parsed, rather than returning an empty table
    assert len(stk) > 50, len(stk)


# ── the map ─────────────────────────────────────────────────────────────────

def test_map_reads_expresss_meaning_for_the_three_codes(pre190_db):
    before = _words(sqlite3.connect(pre190_db))
    assert (before['กร'], before['ถง'], before['บล']) == ('ตัว', 'ถุง', 'แผง')

    database.init_db()

    conn = database.get_connection()
    try:
        assert bsn_units.translate('กร', bsn_units.BOOK_BSN5657, conn=conn) == 'กุรุส'
        assert bsn_units.translate('ถง', bsn_units.BOOK_BSN5657, conn=conn) == 'ถัง'
        assert bsn_units.translate('บล', bsn_units.BOOK_BSN5657, conn=conn) == 'บล็อก'
        assert bsn_units.normalize_unit('กร', conn=conn) == 'กุรุส'
        # CONTROL: the migration touched only these three. `ผง` also means แผง
        # and must NOT have followed `บล`.
        w = _words(conn)
        assert (w['หค'], w['ตว'], w['ถุ'], w['ผง']) == ('โหลคู่', 'ตัว', 'ถุง', 'แผง')
        # the new words survive a second normalize, or _unit_same flags every
        # line the importer writes with them
        for word in ('กุรุส', 'ถัง', 'บล็อก'):
            assert bsn_units.normalize_unit(word, conn=conn) == word
        # EQUAL, not two hard-coded lists: what the migration calls the new
        # word must be exactly what the importer now produces for that code
        # (erp-engineering-discipline.md, "a migration that normalises stored
        # values must write exactly what the IMPORTER would write").
        for code, _old, new_word in _embedded(
                'mig190 meanings', '_mig190_meaning', 'code, old_word, new_word'):
            assert bsn_units.normalize_unit(code, conn=conn) == new_word
    finally:
        conn.close()


def test_the_other_express_book_is_untouched(pre190_db):
    """Meaning is per book (ADR 0018): an xp5 row for the same spelling keeps
    its own word."""
    conn = _open(pre190_db)
    conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('xp5','กร','ตัว')")
    conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('*','กร','ตัว')")
    conn.commit(); conn.close()

    database.init_db()

    conn = database.get_connection()
    try:
        assert bsn_units.translate('กร', bsn_units.BOOK_XP5, conn=conn) == 'ตัว'
        assert bsn_units.translate('กร', bsn_units.BOOK_ANY, conn=conn) == 'ตัว'
        # control: the BSN5657 row DID move in this same run
        assert bsn_units.translate('กร', bsn_units.BOOK_BSN5657, conn=conn) == 'กุรุส'
    finally:
        conn.close()


# ── the conversions ─────────────────────────────────────────────────────────

def test_rebased_product_gets_the_code_rows_ratio(pre190_db):
    """1050's shape: the base unit is the piece, and `กร` already carries 144.
    AC: 1 กุรุส -> 144 on a rebased product."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 rebased', unit_type='แผ่น')
    _uc(conn, pid, 'กร', 144.0)
    _uc(conn, pid, 'ตัว', 144.0)
    _uc(conn, pid, 'แผ่น', 1.0)
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert _units(conn, pid) == {'กร': 144.0, 'ตัว': 144.0, 'แผ่น': 1.0, 'กุรุส': 144.0}
        assert models._get_base_qty(conn, pid, 'แผ่น', 'กุรุส', 2) == 288.0
    finally:
        conn.close()


def test_non_rebased_product_gets_ratio_one(pre190_db):
    """The other ten: `กร` = 1.0 against a `ตัว` base. AC: 1 กุรุส -> 1 there.
    Express's own factor on their lines is 144 — #581/#603 rebase them one at a
    time; copying anything but 1.0 here would move stock."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 not rebased', unit_type='ตัว')
    _uc(conn, pid, 'กร', 1.0)
    _uc(conn, pid, 'ตัว', 1.0)
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert _units(conn, pid) == {'กร': 1.0, 'ตัว': 1.0, 'กุรุส': 1.0}
        assert models._get_base_qty(conn, pid, 'ตัว', 'กุรุส', 5) == 5.0
    finally:
        conn.close()


def test_product_billed_in_the_code_takes_the_unit_type_short_circuit(pre190_db):
    """No conversion under the raw code at all — the product is only known to
    Express. The old word IS its unit_type, so `_get_base_qty` resolved it at 1
    and the new word must too. This is the ถง/บล arm.

    No `แผง` conversion row, on purpose: with one present the migration could
    take 1.0 from that row, and this test would stay green with the
    unit_type short circuit deleted (review of #628, NIT 4)."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 billed in บล', unit_type='แผง')
    _map_row(conn, pid, _a_stkcod('บล'))
    conn.commit()
    assert _units(conn, pid) == {}, 'control: only the short circuit can answer'
    conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert _units(conn, pid) == {'บล็อก': 1.0}
    finally:
        conn.close()


def test_product_whose_old_reading_never_resolved_gets_nothing(pre190_db):
    """456's shape: billed in `ถง`, but neither `ถง` nor `ถุง` resolves and the
    base unit is something else. Its lines are unsynced today and must stay
    unsynced — inventing a ratio here is exactly the guess #595 forbids."""
    conn = _open(pre190_db)
    dead = _product(conn, 'mig190 unresolvable', unit_type='ดอก')
    _uc(conn, dead, 'กล่อง', 1000.0)
    _map_row(conn, dead, _a_stkcod('ถง'))
    # CONTROL: a sibling on the SAME code that DOES resolve, so an empty result
    # cannot come from the ถง arm never running at all.
    live = _product(conn, 'mig190 resolvable sibling', unit_type='ถุง')
    _map_row(conn, live, _a_stkcod('ถง'), bsn_unit='x')
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert _units(conn, dead) == {'กล่อง': 1000.0}
        assert models._get_base_qty(conn, dead, 'ดอก', 'ถัง', 3) is None
        assert _units(conn, live) == {'ถัง': 1.0}
    finally:
        conn.close()


def test_an_existing_new_word_row_is_not_duplicated_or_overwritten(pre190_db):
    """1050/1320 on prod already carry กุรุส = 144 (added by #584)."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 already has the word', unit_type='แท่ง')
    _uc(conn, pid, 'กร', 144.0)
    _uc(conn, pid, 'ตัว', 144.0)
    _uc(conn, pid, 'กุรุส', 144.0)
    _uc(conn, pid, 'แท่ง', 1.0)
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert _units(conn, pid) == {'กร': 144.0, 'ตัว': 144.0, 'กุรุส': 144.0, 'แท่ง': 1.0}
        assert conn.execute(
            "SELECT COUNT(*) FROM unit_conversions WHERE product_id=? AND bsn_unit='กุรุส'",
            (pid,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_every_raw_code_conversion_ends_with_its_word_conversion(pre190_db):
    """The ticket's AC, asserted over the WHOLE table, one seeded product per
    code. The fixture removes the clone's own raw-code rows, so the same
    property over the REAL rows is proved by the prod-copy rehearsal and by the
    migration's own postcondition, not here."""
    conn = _open(pre190_db)
    for code, ratio in (('กร', 144.0), ('ถง', 1.0), ('บล', 1.0)):
        pid = _product(conn, f'mig190 AC {code}', unit_type='ชิ้น')
        _uc(conn, pid, code, ratio)
    conn.commit()
    seeded = conn.execute("""
        SELECT COUNT(*) FROM unit_conversions c
         WHERE c.bsn_unit IN ('กร','ถง','บล')""").fetchone()[0]
    conn.close()
    assert seeded == 3, 'control: exactly the three seeded raw-code rows'

    database.init_db()

    conn = _open(pre190_db)
    try:
        for code, (_old, new) in MEANINGS.items():
            left = conn.execute("""
                SELECT c.product_id FROM unit_conversions c
                 WHERE c.bsn_unit = ?
                   AND NOT EXISTS (SELECT 1 FROM unit_conversions w
                                    WHERE w.product_id = c.product_id AND w.bsn_unit = ?)
            """, (code, new)).fetchall()
            assert left == [], f'{code} rows without a {new} row: {left}'
    finally:
        conn.close()


# ── nothing moves ───────────────────────────────────────────────────────────

def test_stock_and_ledger_do_not_move(pre190_db):
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 stock neutral', unit_type='ตัว')
    _uc(conn, pid, 'กร', 1.0)
    _uc(conn, pid, 'ตัว', 1.0)
    _sale(conn, 'IV190STOCK-1', pid, 'ตัว', qty=7, synced=0)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=[pid])
    conn.commit()

    def state():
        return (
            conn.execute("SELECT printf('%.18f', quantity) FROM stock_levels "
                         "WHERE product_id=?", (pid,)).fetchone(),
            conn.execute("SELECT COUNT(*), printf('%.18f', COALESCE(SUM(quantity_change),0))"
                         " FROM transactions WHERE product_id=?", (pid,)).fetchone()[:],
            conn.execute("SELECT printf('%.18f', COALESCE(cost_price,0)) FROM products "
                         "WHERE id=?", (pid,)).fetchone(),
        )
    before = state()
    assert before[1][0] > 0, 'control: the fixture posted no ledger row to protect'
    conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert state() == before
    finally:
        conn.close()


def test_cogs_for_a_gross_sale_uses_144_not_the_one_fallback(pre190_db):
    """AC: COGS for a กุรุส sale on a 1050-shaped product uses 144.
    `sales_filters.base_qty_sql` falls back to 1.0 for a unit with no
    conversion — that fallback is the whole reason the conversions ship in this
    migration."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 cogs', unit_type='แผ่น', cost=5.0)
    _uc(conn, pid, 'กร', 144.0)
    _uc(conn, pid, 'ตัว', 144.0)
    _uc(conn, pid, 'แผ่น', 1.0)
    _sale(conn, 'IV190COGS-1', pid, 'กุรุส', qty=2)
    conn.commit()

    sql = ("SELECT " + sales_filters.base_qty_sql() + ", " +
           sales_filters.unratioed_line_sql() +
           " FROM sales_transactions st JOIN products p ON p.id = st.product_id"
           " LEFT JOIN unit_conversions uc ON uc.product_id = st.product_id"
           "   AND uc.bsn_unit = st.unit"
           " WHERE st.doc_no = 'IV190COGS-1'")
    # BEFORE: no กุรุส conversion -> the 1.0 fallback, and the line is disclosed
    assert conn.execute(sql).fetchone()[:] == (2.0, 1)
    conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        assert conn.execute(sql).fetchone()[:] == (288.0, 0)
    finally:
        conn.close()


@pytest.mark.parametrize('unit_type, ratio, expected', [('แผ่น', 144.0, 144.0),
                                                        ('ตัว', 1.0, 1.0)])
def test_reimporting_a_raw_code_line_relabels_it_without_moving_stock(
        pre190_db, unit_type, ratio, expected):
    """THE TRAP. A line stored under the OLD word, re-imported with the RAW
    Express code Express keeps sending: post-190 the importer normalises that
    code to `กุรุส`, so `bsn_line._unit_same` reads the line as CHANGED, rewrites
    it, and pass 2 re-posts its ledger through the NEW conversion key. Quantity
    must be identical either way."""
    conn = _open(pre190_db)
    pid = _product(conn, f'mig190 reimport {unit_type}', unit_type=unit_type)
    _uc(conn, pid, 'กร', ratio)
    _uc(conn, pid, 'ตัว', ratio)
    if unit_type != 'ตัว':
        _uc(conn, pid, unit_type, 1.0)
    _map_row(conn, pid, 'C599')
    _sale(conn, 'IV190TRAP-1', pid, 'ตัว', qty=3, synced=0)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=[pid])
    conn.commit()
    stock_before = conn.execute(
        "SELECT printf('%.18f', quantity) FROM stock_levels WHERE product_id=?",
        (pid,)).fetchone()[0]
    assert float(stock_before) == -3.0 * expected, stock_before
    conn.close()

    database.init_db()

    entry = {'date_iso': '2026-01-01', 'doc_no': 'IV190TRAP-1', 'line_seq': 1,
             'qty': 3, 'unit': 'กร', 'unit_price': 10, 'vat_type': 0,
             'discount': '', 'total': 30, 'net': 30, 'product_name_raw': 'x',
             'product_code_raw': 'C599', 'party': 'cust', 'party_code': 'C1'}
    res = models.import_weekly([entry], 'sales', 'test-599', apply_removals=False)
    assert res['overwritten'] == 1, res      # control: the line really was rewritten

    conn = _open(pre190_db)
    try:
        row = conn.execute("SELECT unit, synced_to_stock FROM sales_transactions "
                           "WHERE doc_no='IV190TRAP-1'").fetchone()
        assert row[0] == 'กุรุส'
        assert row[1] == 1, 'the relabelled line must still be synced'
        assert conn.execute(
            "SELECT printf('%.18f', quantity) FROM stock_levels WHERE product_id=?",
            (pid,)).fetchone()[0] == stock_before
    finally:
        conn.close()


# ── preconditions ───────────────────────────────────────────────────────────

def _state(conn):
    """Everything migration 190 can write, so "untouched" is the WHOLE table
    rather than a count a fixture can satisfy by accident."""
    return (
        {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT book, spelling, word FROM unit_map")},
        [tuple(r) for r in conn.execute(
            "SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id")],
    )


def _expect_abort(db_path, fragment):
    conn = _open(db_path)
    before = _state(conn)
    conn.close()

    with pytest.raises(sqlite3.Error) as exc:
        database.init_db()
    assert fragment in str(exc.value), str(exc.value)

    conn = _open(db_path)
    try:
        applied = {r[0] for r in conn.execute("SELECT filename FROM applied_migrations")}
        assert MIG not in applied, 'a failed precondition must not stamp the migration'
        assert _state(conn) == before, 'the DB must be untouched'
    finally:
        conn.close()


def test_precondition_code_vs_old_aborts(pre190_db):
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 disagreeing rows', unit_type='แผ่น')
    _uc(conn, pid, 'กร', 144.0)
    _uc(conn, pid, 'ตัว', 12.0)          # the two readings disagree
    conn.commit(); conn.close()
    _expect_abort(pre190_db, 'code_vs_old')


def test_precondition_new_conflict_aborts(pre190_db):
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 new word conflict', unit_type='แผ่น')
    _uc(conn, pid, 'กร', 144.0)
    _uc(conn, pid, 'กุรุส', 12.0)         # a human already set a different ratio
    conn.commit(); conn.close()
    _expect_abort(pre190_db, 'new_conflict')


def test_precondition_new_is_base_aborts(pre190_db):
    """The new word IS the product's unit_type while the target is not 1: a
    line in that unit short-circuits to ratio 1 in `_get_base_qty`, so the
    conversion would never be read and the next rebuild would post a different
    quantity (mig 186's unit_type_ratio guard, same hazard)."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 new word is the base', unit_type='กุรุส')
    _uc(conn, pid, 'กร', 144.0)
    conn.commit(); conn.close()
    _expect_abort(pre190_db, 'new_is_base')


def test_the_postcondition_catches_a_conversion_that_was_not_written(pre190_db):
    """Break-it-once, owned by the test rather than by a hand edit: run the
    migration's own SQL with its INSERT removed and require the postcondition
    to ABORT. Without this the postcondition is a clause nothing ever exercises
    -- it cannot be reached from data alone, because the INSERT covers every
    planned row by construction."""
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 postcondition', unit_type='ตัว')
    _uc(conn, pid, 'กร', 1.0)
    conn.commit()

    sql = _read(MIG_190)
    insert = ("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)\n"
              "SELECT product_id, new_word, COALESCE(code_ratio, old_ratio)\n"
              "  FROM _mig190_plan\n"
              " WHERE new_ratio IS NULL AND COALESCE(code_ratio, old_ratio) IS NOT NULL;")
    assert insert in sql, 'the INSERT this test removes has been reworded'
    broken = sql.replace(insert, '-- removed by the test')
    assert insert not in broken, 'the mutation did not land'

    with pytest.raises(sqlite3.Error) as exc:
        conn.executescript(broken)
    assert 'postcondition FAILED' in str(exc.value), str(exc.value)

    # RAISE(ABORT) cancels its own STATEMENT and leaves the transaction open,
    # so the caller is what makes the failure atomic. The migration RUNNER
    # rolls back (proved by the precondition tests above, which read a FRESH
    # connection); here the test is the caller, so it rolls back itself.
    # NOT conn.executescript('ROLLBACK') -- executescript COMMITS first, which
    # would keep the half-applied migration.
    conn.execute('ROLLBACK')
    assert _words(conn)['กร'] == 'ตัว'
    assert _units(conn, pid) == {'กร': 1.0}
    # CONTROL: the untouched SQL runs clean on the same DB, so the abort came
    # from the missing INSERT and not from the fixture or the script itself.
    conn.executescript(sql)
    try:
        assert _units(conn, pid) == {'กร': 1.0, 'กุรุส': 1.0}
    finally:
        conn.close()


# ── rollback, re-runnability ────────────────────────────────────────────────

def test_rollback_restores_the_map_and_removes_only_its_own_rows(pre190_db):
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 rollback', unit_type='ตัว')
    _uc(conn, pid, 'กร', 1.0)
    keeper = _product(conn, 'mig190 rollback keeper', unit_type='ชิ้น')
    _uc(conn, keeper, 'กุรุส', 999.0)      # nothing to do with this migration
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    assert _units(conn, pid) == {'กร': 1.0, 'กุรุส': 1.0}
    conn.executescript(_read(ROLLBACK_190))
    conn.commit()
    try:
        assert _words(conn)['กร'] == 'ตัว'
        assert _words(conn)['ถง'] == 'ถุง'
        assert _words(conn)['บล'] == 'แผง'
        assert _units(conn, pid) == {'กร': 1.0}
        assert _units(conn, keeper) == {'กุรุส': 999.0}, 'rollback took an unrelated row'
        # Only THIS test's products: a prod-shaped clone legitimately holds
        # other new-word rows (#584 gave 1050/1320 กุรุส = 144) that a
        # whole-table count would read as rollback leftovers.
        assert conn.execute(
            "SELECT COUNT(*) FROM unit_conversions WHERE bsn_unit IN ('กุรุส','ถัง','บล็อก') "
            "AND product_id IN (?, ?)", (pid, keeper)).fetchone()[0] == 1
    finally:
        conn.close()


def test_rollback_refuses_a_row_someone_edited_afterwards(pre190_db):
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 edited after', unit_type='ตัว')
    _uc(conn, pid, 'กร', 1.0)
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    try:
        conn.execute("UPDATE unit_conversions SET ratio = 144.0 "
                     "WHERE product_id=? AND bsn_unit='กุรุส'", (pid,))
        conn.commit()
        with pytest.raises(sqlite3.Error) as exc:
            conn.executescript(_read(ROLLBACK_190))
        assert 'rollback REFUSED' in str(exc.value), str(exc.value)
        assert _units(conn, pid) == {'กร': 1.0, 'กุรุส': 144.0}
        assert _words(conn)['กร'] == 'กุรุส', 'a refused rollback must change nothing'
    finally:
        conn.close()


def test_a_second_forward_run_changes_nothing(pre190_db):
    conn = _open(pre190_db)
    pid = _product(conn, 'mig190 rerun', unit_type='ตัว')
    _uc(conn, pid, 'กร', 1.0)
    conn.commit(); conn.close()

    database.init_db()

    conn = _open(pre190_db)
    first = (dict(conn.execute("SELECT spelling, word FROM unit_map WHERE book='BSN5657'")),
             [tuple(r) for r in conn.execute(
                 "SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id")])
    conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
    conn.commit(); conn.close()

    database.init_db()          # the runner sees it as pending again

    conn = _open(pre190_db)
    try:
        second = (dict(conn.execute("SELECT spelling, word FROM unit_map WHERE book='BSN5657'")),
                  [tuple(r) for r in conn.execute(
                      "SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id")])
        assert second == first
    finally:
        conn.close()
