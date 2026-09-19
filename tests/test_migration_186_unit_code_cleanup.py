"""Migration 186 — GH #597 (#595 · 2/7): translate stored Express unit codes
to the word THE IMPORTER ITSELF produces for them, and merge/rename duplicate
unit_conversions rows.

The invariant this file pins (the #609 review's blocker): a stored unit may
only become `bsn_units.normalize_unit(code)`. Anything else and the next import
sees the stored word differ from the raw code, rewrites the line, and loses its
conversion (reproduced on a prod copy: pid 436 stock +70).

Assert external behaviour only: the word stored on a row, what a re-import of
the raw line does, and stock before vs after. `tmp_db` clones the live dev DB
with its data, so every test seeds exactly the rows it needs.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import bsn_units
import database

MIG = '186_unit_code_cleanup.sql'
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MIG_186 = os.path.join(REPO, 'data', 'migrations', MIG)
ROLLBACK_186 = os.path.join(REPO, 'data', 'migrations', '186_unit_code_cleanup.rollback.sql')

# Express's own meaning of these differs from the unit map (#599/#600), and
# the `!` entries are not units. Neither is translated by 186.
EXCLUDED_CODES = {'กร', 'ถง', 'บล'}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _block(sql, name):
    """The text between `-- >>> <name>` and `-- <<< <name>` in the migration."""
    start = sql.index(f'-- >>> {name}')
    end = sql.index(f'-- <<< {name}')
    return sql[start:end]


def _embedded_map():
    """Execute the migration's own map block and read the rows back, so the
    test sees exactly what the migration will use (no regex over SQL)."""
    conn = sqlite3.connect(':memory:')
    try:
        conn.executescript(_block(_read(MIG_186), 'mig186 map'))
        return dict(conn.execute('SELECT code, word FROM _mig186_map'))
    finally:
        conn.close()


@pytest.fixture
def pre186_db(tmp_db):
    """Reconstruct the true pre-186 state: once an environment has booted
    after 186 merges, `tmp_db`'s clone of the live DB already carries it, and
    a bare `database.init_db()` would silently skip the SQL on disk."""
    conn = sqlite3.connect(tmp_db)
    try:
        applied = {r[0] for r in conn.execute("SELECT filename FROM applied_migrations")}
        if MIG in applied:
            conn.executescript(_read(ROLLBACK_186))
            conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
            conn.commit()
    finally:
        conn.close()
    return tmp_db


def _new_product(conn, name, unit_type='ตัว', cost=10.0, base=20.0):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price, base_sell_price) "
        "VALUES (?,?,?,?)", (name, unit_type, cost, base))
    return cur.lastrowid


def _uc(conn, product_id, bsn_unit, ratio):
    return conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
        (product_id, bsn_unit, ratio)).lastrowid


def _sale(conn, doc_no, pid, unit, qty=1, price=10, code='C', synced=1):
    return conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01',?,?,?,?,'x','cust','C1',?,?,?,0,'',?,?,?)",
        (doc_no, doc_no.rsplit('-', 1)[0], pid, code, qty, unit, price,
         qty * price, qty * price, synced)).lastrowid


def _purchase(conn, doc_no, pid, unit, qty=1, price=10, code='C', line_seq=1, synced=1):
    return conn.execute(
        "INSERT INTO purchase_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, supplier, supplier_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, line_seq, synced_to_stock) "
        "VALUES (1,'2026-01-01',?,?,?,?,'x','sup','S1',?,?,?,0,'',?,?,?,?)",
        (doc_no, doc_no, pid, code, qty, unit, price, qty * price, qty * price,
         line_seq, synced)).lastrowid


def _units(conn, pid):
    return dict(conn.execute(
        "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)))


# ── the map: exactly what the importer produces ─────────────────────────────

def test_embedded_map_equals_what_the_importer_produces(tmp_db):
    """Both directions: every pair in the migration is one the importer makes,
    and every code the importer translates (minus the exclusions) is in it.
    Derived through bsn_units' PUBLIC API so it holds when #596 moves the map
    into the DB."""
    derived = {}
    for code in bsn_units.load_unit_map():
        word = bsn_units.normalize_unit(code)
        if word != code and code not in EXCLUDED_CODES and not code.startswith('!'):
            derived[code] = word
    # control: the API really returned the map, not an empty one
    assert derived.get('หค') == 'โหลคู่', derived

    embedded = _embedded_map()
    assert sorted(set(embedded.items()) - set(derived.items())) == [], \
        "migration translates a code to a word the importer never produces"
    assert sorted(set(derived.items()) - set(embedded.items())) == [], \
        "importer translates a code the migration leaves behind"
    # a translated word must survive a second normalize, or _unit_same flags it
    assert {w for w in embedded.values() if bsn_units.normalize_unit(w) != w} == set()


# ── the #609 blocker, kept as a test: re-import is a no-op after 186 ────────

def _entry(row, file_type):
    e = {'date_iso': row['date_iso'], 'doc_no': row['doc_no'], 'qty': row['qty'],
         'unit': row['unit'], 'unit_price': row['unit_price'], 'vat_type': row['vat_type'],
         'discount': row['discount'], 'total': row['total'], 'net': row['net'],
         'product_name_raw': row['product_name_raw'], 'product_code_raw': row['bsn_code']}
    if file_type == 'purchase':
        e.update(line_seq=row['line_seq'], party=row['supplier'], party_code=row['supplier_code'])
    else:
        e.update(line_seq=1, party=row['customer'], party_code=row['customer_code'])
    return e


def _ledger(conn, pid):
    return (conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0],
            tuple(conn.execute("SELECT COUNT(*), SUM(quantity_change) FROM transactions "
                               "WHERE product_id=?", (pid,)).fetchone()))


# (file_type, raw unit as Express emits it, stored unit, conversions, 186 word)
# 1. RR6900079 line 1: purchase stored raw 'หค', code+word twin at 24 (merge)
# 2. RR6900079 line 2: purchase already stored as the word, same product shape
# 3. a sales 'หล' line whose only conversion is the code row (rename)
# 4. the reviewer's pid 436 shape: 'ช5' is NOT an importer code, so 186 must
#    leave the line and its conversion alone
REIMPORT_CASES = [
    ('rr1', 'purchase', 'หค', 'หค',     {'หค': 24.0, 'โหลคู่': 24.0}, 'โหลคู่'),
    ('rr2', 'purchase', 'หค', 'โหลคู่',   {'หค': 24.0, 'โหลคู่': 24.0}, 'โหลคู่'),
    ('hl',  'sales',    'หล', 'หล',     {'หล': 12.0},                'โหล'),
    ('c5',  'sales',    'ช5', 'ช5',     {'ช5': 5.0},                 'ช5'),
]


def _seed_reimport_case(conn, file_type, stored_unit, convs, tag):
    """A synced legacy line posted through the app's own sync, before 186."""
    from models.bsn_sync import _sync_bsn_to_stock
    code = f'T186-{tag}'
    pid = _new_product(conn, f'mig186 reimport {tag}')
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
                 "VALUES (?, 'x', ?)", (code, pid))
    for unit, ratio in convs.items():
        _uc(conn, pid, unit, ratio)
    table = 'purchase_transactions' if file_type == 'purchase' else 'sales_transactions'
    if file_type == 'purchase':
        _purchase(conn, f'RR186{tag}', pid, stored_unit, qty=25, price=240, code=code, synced=0)
    else:
        _sale(conn, f'IV186{tag}-1', pid, stored_unit, qty=14, price=50, code=code, synced=0)
    _sync_bsn_to_stock(conn, table, file_type, product_ids=[pid])
    conn.commit()
    return pid, table


@pytest.mark.parametrize('tag,file_type,raw_unit,stored_unit,convs,word', REIMPORT_CASES)
def test_reimport_of_raw_lines_is_unchanged_after_186(pre186_db, tag, file_type, raw_unit,
                                                       stored_unit, convs, word):
    import models
    conn = sqlite3.connect(pre186_db)
    conn.row_factory = sqlite3.Row
    pid, table = _seed_reimport_case(conn, file_type, stored_unit, convs, tag)
    before = _ledger(conn, pid)
    assert before[1][0] >= 1, "seed never posted to the ledger -- the test would be vacuous"
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table} WHERE product_id=? ORDER BY id", (pid,)).fetchall()
    # Express emits the RAW code again on the next upload
    entries = [_entry(r, file_type) for r in rows]
    entries[0]['unit'] = raw_unit
    conn.close()

    res = models.import_weekly(entries, file_type, 'mig186-reimport-test', apply_removals=False)

    conn = sqlite3.connect(pre186_db)
    try:
        assert _ledger(conn, pid) == before
        assert res['unchanged'] == len(entries), res
        assert res['overwritten'] == 0 and res['imported'] == 0, res
        assert conn.execute(f"SELECT id, unit, synced_to_stock FROM {table} WHERE product_id=?",
                            (pid,)).fetchall() == [(rows[0]['id'], word, 1)]
    finally:
        conn.close()


def test_reimport_check_goes_red_when_186_translates_a_non_importer_code(pre186_db, tmp_path):
    """Control for the test above: give a COPY of 186 the reviewer's bad pair
    (ช5 -> ชุด5) and the same pid-436-shaped line must move stock on re-import.
    If this ever passes quietly, the re-import test above has gone vacuous."""
    import models
    sql = _read(MIG_186)
    marker = "INSERT INTO _mig186_map (code, word) VALUES\n"
    assert sql.count(marker) == 1
    mutated = sql.replace(marker, marker + "    ('ช5', 'ชุด5'),\n")
    assert "('ช5', 'ชุด5')" in _block(mutated, 'mig186 map')

    conn = sqlite3.connect(pre186_db)
    conn.row_factory = sqlite3.Row
    pid, table = _seed_reimport_case(conn, 'sales', 'ช5', {'ช5': 5.0}, 'bad')
    before = _ledger(conn, pid)
    conn.executescript(mutated)
    row = conn.execute(f"SELECT * FROM {table} WHERE product_id=?", (pid,)).fetchone()
    assert row['unit'] == 'ชุด5'
    entry = _entry(row, 'sales')
    entry['unit'] = 'ช5'
    conn.close()

    res = models.import_weekly([entry], 'sales', 'mig186-bad-map', apply_removals=False)

    assert res['overwritten'] == 1, res
    conn = sqlite3.connect(pre186_db)
    try:
        after = _ledger(conn, pid)
    finally:
        conn.close()
    assert after[0] != before[0], "stock did not move -- the control cannot see the blocker"
    assert after[0] - before[0] == 14 * 5.0


# ── apply / idempotency / declared-change path ──────────────────────────────

def test_migration_applies_and_is_idempotent_via_runner(pre186_db):
    database.init_db()
    database.init_db()
    conn = sqlite3.connect(pre186_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM applied_migrations WHERE filename = ?",
                         (MIG,)).fetchone()[0]
        assert n == 1, "re-running the runner must not re-apply 186"
    finally:
        conn.close()


def test_hand_rerun_keeps_the_first_runs_snapshot(pre186_db):
    """CREATE TABLE IF NOT EXISTS, not drop-first: a second run by hand must
    neither fail nor erase the rows the rollback restores from."""
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 rerun product', unit_type='แผง')
    _uc(conn, pid, 'ตว', 1.0)
    conn.commit()
    conn.close()
    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        first = conn.execute("SELECT COUNT(*) FROM migration_186_snapshot").fetchone()[0]
        assert first >= 1
        conn.executescript(_read(MIG_186))
        assert conn.execute("SELECT COUNT(*) FROM migration_186_snapshot").fetchone()[0] == first
        assert _units(conn, pid) == {'ตัว': 1.0}
    finally:
        conn.close()


def test_ledger_lines_translated_via_declared_change_path(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 declared-change product', unit_type='โหล')
    sid = _sale(conn, 'IVTEST-1', pid, 'หล', price=100)
    pur = _purchase(conn, 'HPTEST-1', pid, 'หล', price=80)
    # a reason left over from an earlier human edit must not be carried into
    # the migration's own audit row
    conn.execute("UPDATE sales_transactions SET change_reason='old human reason here' WHERE id=?",
                 (sid,))
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        for table, rid in (('sales_transactions', sid), ('purchase_transactions', pur)):
            row = conn.execute(f"SELECT unit, change_source, change_actor, change_token, "
                               f"change_reason FROM {table} WHERE id=?", (rid,)).fetchone()
            assert row[:3] == ('โหล', 'import', 'mig186-unit-cleanup')
            assert row[3]
            assert row[4] is None
            audit = conn.execute(
                "SELECT user, change_source, change_reason FROM audit_log "
                "WHERE table_name=? AND row_id=? AND action='UPDATE' "
                "AND changed_fields LIKE '%\"unit\"%' ORDER BY id DESC LIMIT 1",
                (table, rid)).fetchone()
            assert audit == ('mig186-unit-cleanup', 'import', None)
    finally:
        conn.close()


# ── unit_conversions: merge / rename / abort ────────────────────────────────

def test_twin_merge_same_ratio_deletes_code_row(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 twin merge product')
    _uc(conn, pid, 'หล', 12.0)
    _uc(conn, pid, 'โหล', 12.0)
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert _units(conn, pid) == {'โหล': 12.0}
    finally:
        conn.close()


def test_rename_when_no_twin_exists_preserves_ratio(pre186_db):
    """pid 1393's shape on prod: a code-only 'ตว' conversion on a แผง product."""
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 rename product', unit_type='แผง', cost=12.5, base=75.0)
    _uc(conn, pid, 'ตว', 1.0)
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert _units(conn, pid) == {'ตัว': 1.0}
    finally:
        conn.close()


def test_two_codes_for_one_word_same_ratio_merge_deterministically(pre186_db):
    """แพ and แพค both mean แพ็ค. With no แพ็ค row and equal ratios the lower id
    survives (renamed); the other is deleted and snapshotted."""
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 two-codes product')
    keep = _uc(conn, pid, 'แพค', 6.0)
    gone = _uc(conn, pid, 'แพ', 6.0)
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert conn.execute("SELECT id, bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                            (pid,)).fetchall() == [(keep, 'แพ็ค', 6.0)]
        assert conn.execute("SELECT id, product_id, bsn_unit, ratio FROM migration_186_uc_deleted "
                            "WHERE product_id=?", (pid,)).fetchall() == [(gone, pid, 'แพ', 6.0)]
    finally:
        conn.close()


def _assert_aborts_unchanged(db, message):
    conn = sqlite3.connect(db)
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
              for t in ('unit_conversions', 'product_code_mapping', 'product_price_tiers',
                        'products', 'sales_transactions', 'purchase_transactions')}
    applied = conn.execute("SELECT filename FROM applied_migrations ORDER BY 1").fetchall()
    conn.close()

    with pytest.raises(Exception, match=message):
        database.init_db()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'migration_186%'"
                            ).fetchall() == []
        for t, rows in before.items():
            assert conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() == rows, t
        assert conn.execute("SELECT filename FROM applied_migrations ORDER BY 1").fetchall() == applied
    finally:
        conn.close()


def test_disagreeing_twin_aborts_leaving_db_unchanged(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 conflict product')
    _uc(conn, pid, 'ตว', 1.0)
    _uc(conn, pid, 'ตัว', 2.0)
    _sale(conn, 'IVABORT-1', pid, 'หล')      # proves nothing else ran either
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre186_db, 'mig 186 precondition FAILED: code and word conversion')


def test_two_codes_for_one_word_different_ratio_aborts(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 two-codes conflict product')
    _uc(conn, pid, 'แพ', 6.0)
    _uc(conn, pid, 'แพค', 12.0)
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre186_db, 'mig 186 precondition FAILED: two codes for one word')


def test_mapping_collision_aborts_in_the_precondition(pre186_db):
    """(bsn_code, 'หล') next to (bsn_code, 'โหล') cannot both become 'โหล'."""
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 mapping collision product')
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
                 "VALUES ('T186-PCM', 'x', ?, 'หล')", (pid,))
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) "
                 "VALUES ('T186-PCM', 'x', ?, 'โหล')", (pid,))
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre186_db, 'mig 186 precondition FAILED: product_code_mapping')


def test_tier_label_collision_aborts_in_the_precondition(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 tier collision product')
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?, '1 แพค', 40)",
                 (pid,))
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?, '1 แพ็ค', 45)",
                 (pid,))
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre186_db, 'mig 186 precondition FAILED: product_price_tiers')


def test_unit_type_landing_on_a_non_unit_ratio_aborts(pre186_db):
    """After 186 a line in the product's own unit short-circuits to ratio 1
    (_get_base_qty), so a conversion for that word with any other ratio would
    silently change what the next ledger rebuild posts."""
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 unit_type ratio product', unit_type='กก')
    _uc(conn, pid, 'กิโลกรัม', 1000.0)
    conn.commit()
    conn.close()
    _assert_aborts_unchanged(pre186_db, 'mig 186 precondition FAILED: unit_type')


# ── what 186 leaves alone ───────────────────────────────────────────────────

@pytest.mark.parametrize('code', ['กร', 'ถง', 'บล', '!หล', 'ช5', 'กิโล', 'คค'])
def test_codes_outside_the_importer_map_left_untouched(pre186_db, code):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, f'mig186 untouched-code product {code}')
    _sale(conn, f'IVX{code}-1', pid, code)
    _purchase(conn, f'HPX{code}', pid, code)
    _uc(conn, pid, code, 1.0)
    conn.execute("UPDATE products SET unit_type=? WHERE id=?", (code, pid))
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert conn.execute("SELECT unit FROM sales_transactions WHERE doc_no=?",
                            (f'IVX{code}-1',)).fetchone()[0] == code
        assert conn.execute("SELECT unit FROM purchase_transactions WHERE doc_no=?",
                            (f'HPX{code}',)).fetchone()[0] == code
        assert _units(conn, pid) == {code: 1.0}
        assert conn.execute("SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()[0] == code
    finally:
        conn.close()


def test_tables_rewritten_from_raw_codes_on_upload_are_left_alone(pre186_db):
    """express_sales_order_lines is replaced from raw TQUCOD on every DBF
    upload, express_credit_note_lines' DBF-direct path deletes and re-inserts
    raw units, and the supplier tables use the supplier's own words (ขด = coil)
    -- translating any of them only lasts until the next upload (#610)."""
    conn = sqlite3.connect(pre186_db)
    sid = conn.execute("INSERT INTO suppliers (name) VALUES ('mig186 supplier')").lastrowid
    soid = conn.execute(
        "INSERT INTO express_sales_order_lines (entity, so_no, line_seq, unit) "
        "VALUES ('BSN', 'SO186', 1, 'หล')").lastrowid
    cnid = conn.execute(
        "INSERT INTO express_credit_note_lines (credit_note_id, line_no, unit) "
        "VALUES (999999, 1, 'หล')").lastrowid
    sci = conn.execute(
        "INSERT INTO supplier_catalogue_items (supplier_id, name_raw, name_normalized, unit) "
        "VALUES (?, 'เชือก', 'เชือก', 'ขด')", (sid,)).lastrowid
    conn.execute(
        "INSERT INTO supplier_catalogue_price_history (item_id, version_id, unit) "
        "VALUES (?, 1, 'แพค')", (sci,))
    spm = conn.execute(
        "INSERT INTO supplier_product_mapping (supplier_id, catalogue_item_id, supplier_unit, erp_unit) "
        "VALUES (?, ?, 'แพ', 'หล')", (sid, sci)).lastrowid
    pid = _new_product(conn, 'mig186 control product')
    ctl = _sale(conn, 'IVCTL-1', pid, 'หล')     # control: a covered column DOES change
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert conn.execute("SELECT unit FROM sales_transactions WHERE id=?", (ctl,)).fetchone()[0] == 'โหล'
        assert conn.execute("SELECT unit FROM express_sales_order_lines WHERE id=?",
                            (soid,)).fetchone()[0] == 'หล'
        assert conn.execute("SELECT unit FROM express_credit_note_lines WHERE id=?",
                            (cnid,)).fetchone()[0] == 'หล'
        assert conn.execute("SELECT unit FROM supplier_catalogue_items WHERE id=?",
                            (sci,)).fetchone()[0] == 'ขด'
        assert conn.execute("SELECT unit FROM supplier_catalogue_price_history "
                            "WHERE item_id=?", (sci,)).fetchone()[0] == 'แพค'
        assert conn.execute("SELECT supplier_unit, erp_unit FROM supplier_product_mapping "
                            "WHERE id=?", (spm,)).fetchone() == ('แพ', 'หล')
    finally:
        conn.close()


# ── tier labels ─────────────────────────────────────────────────────────────

def test_tier_labels_keep_leading_count_and_spacing(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 tier label product')
    for label, price in (('1 แพค', 50.0),    # space form -> space kept
                         ('12หล', 100.0),    # no-space form -> word appended directly
                         ('1กิโล', 70.0),    # not an importer code -> untouched (#610)
                         ('1 โหล', 200.0),   # already a word -> untouched
                         ('ลัง', 300.0)):    # no leading count, a word -> untouched
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                     (pid, label, price))
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert dict(conn.execute("SELECT qty_label, price FROM product_price_tiers "
                                 "WHERE product_id=?", (pid,))) == {
            '1 แพ็ค': 50.0, '12โหล': 100.0, '1กิโล': 70.0, '1 โหล': 200.0, 'ลัง': 300.0}
    finally:
        conn.close()


# ── rollback ────────────────────────────────────────────────────────────────

def _state(conn):
    return {
        'sales': conn.execute("SELECT id, unit FROM sales_transactions ORDER BY id").fetchall(),
        'uc': conn.execute("SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id").fetchall(),
        'tiers': conn.execute("SELECT id, qty_label FROM product_price_tiers ORDER BY id").fetchall(),
        'products': conn.execute("SELECT id, unit_type FROM products ORDER BY id").fetchall(),
    }


def _seed_rollback_fixture(conn):
    pid_merge = _new_product(conn, 'mig186 rollback merge product')
    _uc(conn, pid_merge, 'หล', 12.0)
    _uc(conn, pid_merge, 'โหล', 12.0)
    pid_rename = _new_product(conn, 'mig186 rollback rename product', unit_type='ชด')
    _uc(conn, pid_rename, 'ตว', 1.0)
    _sale(conn, 'IVRB-1', pid_rename, 'หล')
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?, '1 แพค', 99)",
                 (pid_rename,))
    conn.commit()
    return pid_merge, pid_rename


def _run_rollback(conn):
    conn.executescript(_read(ROLLBACK_186))
    conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
    conn.commit()
    return conn.execute("SELECT table_name, row_id, reason FROM temp.mig186_rollback_skipped "
                        "ORDER BY 1, 2").fetchall()


def test_rollback_restores_data_exactly(pre186_db):
    conn = sqlite3.connect(pre186_db)
    _seed_rollback_fixture(conn)
    before = _state(conn)
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        assert _state(conn) != before, "migration made no change -- rollback check would be vacuous"
        assert _run_rollback(conn) == []
        assert _state(conn) == before
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'migration_186%'"
                            ).fetchall() == []
    finally:
        conn.close()


def test_rollback_leaves_later_edits_and_collisions_alone(pre186_db):
    """Rows edited or inserted AFTER the forward run are newer than the
    snapshot: the rollback must not clobber them, and must skip (and report)
    a re-insert or rename-back that would collide on UNIQUE(product_id, bsn_unit)."""
    conn = sqlite3.connect(pre186_db)
    pid_merge, pid_rename = _seed_rollback_fixture(conn)
    pid_edit = _new_product(conn, 'mig186 rollback edited product')
    _uc(conn, pid_edit, 'อน', 1.0)
    sid = _sale(conn, 'IVRB-2', pid_edit, 'หล')
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        # someone renames a converted row by hand ...
        conn.execute("UPDATE unit_conversions SET bsn_unit='ชิ้น' WHERE product_id=? AND bsn_unit='อัน'",
                     (pid_edit,))
        # ... re-creates the merged-away code row ...
        _uc(conn, pid_merge, 'หล', 12.0)
        # ... re-creates the renamed code row ...
        _uc(conn, pid_rename, 'ตว', 1.0)
        # ... and corrects a bill line's unit through the declared path
        conn.execute("UPDATE sales_transactions SET unit='แผง', change_source='manual', "
                     "change_actor='t', change_token='t-186', change_reason='team corrected the unit' "
                     "WHERE id=?", (sid,))
        conn.commit()

        skipped = _run_rollback(conn)

        assert _units(conn, pid_edit) == {'ชิ้น': 1.0}
        assert conn.execute("SELECT unit FROM sales_transactions WHERE id=?", (sid,)).fetchone()[0] == 'แผง'
        assert _units(conn, pid_merge) == {'หล': 12.0, 'โหล': 12.0}
        assert _units(conn, pid_rename) == {'ตว': 1.0, 'ตัว': 1.0}
        assert sorted((t, r) for t, _, r in skipped) == [
            ('sales_transactions', 'changed after 186, left as is'),
            ('unit_conversions', 'changed after 186, left as is'),
            ('unit_conversions', 'code row exists again, not restored'),
            ('unit_conversions', 'code row exists again, not restored'),
        ]
        # the untouched parts of the fixture still roll back
        assert conn.execute("SELECT unit FROM sales_transactions WHERE doc_no='IVRB-1'").fetchone()[0] == 'หล'
        assert conn.execute("SELECT unit_type FROM products WHERE id=?", (pid_rename,)).fetchone()[0] == 'ชด'
    finally:
        conn.close()


# ── stock untouched (label-only change) ─────────────────────────────────────

def test_stock_and_cost_price_unmoved_by_relabelling(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 stock-untouched product', cost=5.0)
    _sale(conn, 'IVSC-1', pid, 'หล', qty=3, price=50, synced=0)
    _uc(conn, pid, 'หล', 12.0)
    conn.commit()
    before = (dict(conn.execute("SELECT product_id, quantity FROM stock_levels")),
              dict(conn.execute("SELECT id, cost_price FROM products")),
              conn.execute("SELECT COUNT(*), SUM(quantity_change) FROM transactions").fetchone())
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        after = (dict(conn.execute("SELECT product_id, quantity FROM stock_levels")),
                 dict(conn.execute("SELECT id, cost_price FROM products")),
                 conn.execute("SELECT COUNT(*), SUM(quantity_change) FROM transactions").fetchone())
        assert after == before
        assert conn.execute("SELECT unit FROM sales_transactions WHERE doc_no='IVSC-1'").fetchone()[0] == 'โหล'
    finally:
        conn.close()
