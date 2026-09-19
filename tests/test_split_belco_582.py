"""TDD for scripts/2026_09_19_split_belco_582.py — issue #582.

Written BEFORE the script (erp-engineering-discipline: anything that re-points
source rows and rebuilds the stock ledger starts from a failing test).

Put ruled 2026-09-19 that BELCO is a different product from META. The two
BELCO lines (purchase HP6900041, sale IV6901138-7) sit on pid 1305
`ดอกลมหัวลูกบล็อก META 8mmx65mm` under the META bsn_code, and the BELCO
purchase dragged 1305's WACC from ฿24.50 to ฿7.00. The script creates a BELCO
product, moves exactly those two lines onto it, replays both ledgers and
recomputes both costs.

Fixture: `empty_db_conn` (the live SCHEMA, zero rows), so every row asserted on
is forced here rather than inherited from a prod clone. The pre-state is built
through the app's own sync + WACC engine, so 1305 lands at ฿7.00 the same way
it did on prod (#546 zero-stock branch), not by typing the number in.

A sibling product (1304) shares BOTH documents with 1305 — a second line on
HP6900041 and on IV6901138 — so keying on doc_no alone would grab it, and an
"every other product unchanged" check has something real to protect.

Every guard has a break-it-once test that first asserts its mutation LANDED
(the violating state is really in the DB, or the injected fault really ran),
then asserts the specific refusal/invariant message AND that nothing was
written (state, not just an exception).
"""
import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_19_split_belco_582.py"
_spec = importlib.util.spec_from_file_location("split_belco_582", _SCRIPT)
split_belco = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(split_belco)

META = 1305
SIBLING = 1304
CODE = '528ด8655'
SIB_CODE = '528ด8654'
BELCO_NAME = 'ดอกลมหัวลูกบล็อก BELCO 8mmx65mm'
PURCHASE_RAW = 'ดอกลมหัวลูกบล็อก 8mmx65mm  BELCO'   # two spaces, as on prod
SALE_RAW = 'ดอกลมหัวลูกบล็อก 8mmx65mm BELCO'


def _seed(conn):
    from models import bsn_sync, wacc
    conn.executescript("""
        INSERT INTO brands (id, code, name) VALUES (7, 'meta', 'META');
        INSERT INTO categories (id, code, name_th, short_code) VALUES (10, 'drill_bit', 'ดอกสว่าน', 'DRB');
        INSERT INTO products (id, product_name, unit_type, cost_price, opening_cost, base_sell_price,
                              brand_id, category_id, sub_category, size, is_active, sku_code, created_via)
        VALUES (1305, 'ดอกลมหัวลูกบล็อก META 8mmx65mm', 'ตัว', 0, 0, 0, 7, 10,
                'ดอกลมหัวลูกบล็อก', '8mmx65mm', 1, 'DRB-META-8mmx65mm', 'legacy'),
               (1304, 'ดอกลมหัวลูกบล็อก META 8mmx45mm', 'ตัว', 0, 0, 0, 7, 10,
                'ดอกลมหัวลูกบล็อก', '8mmx45mm', 1, 'DRB-META-8mmx45mm', 'legacy');
        INSERT INTO stock_levels (product_id, quantity) VALUES (1305, 0), (1304, 0);
        INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES
            (1305, 'อัน', 1.0), (1305, 'อน', 1.0), (1304, 'อัน', 1.0);
        INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit) VALUES
            ('528ด8655', 'ดอกลมหัวลูกบล็อก 8mmx65mm เมต้า', 1305, ''),
            ('528ด8654', 'ดอกลมหัวลูกบล็อก 8mmx45mm เมต้า', 1304, '');
        INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note, created_at)
        VALUES (1305, 'ADJUST', 0, 'unit', 'ยอดยกมา (back-solved)', '2024-01-03 00:00:00');
    """)
    conn.executemany(
        "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, line_seq, product_id, bsn_code,"
        " product_name_raw, supplier, qty, unit, unit_price, discount, net, vat_type, synced_to_stock)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)", [
            ('2024-11-04', 'RR6700438', 'RR6700438', 1, META, CODE,
             'ดอกลมหัวลูกบล็อก 8mmx65mm เมต้า', 'กิจนำอิมปอร์ต', 30.0, 'อน', 35.0, '30%', 735.0, 1),
            ('2026-07-17', 'HP6900041', 'HP6900041', 1, META, CODE,
             PURCHASE_RAW, 'ขจรศิลป์', 13.0, 'อัน', 7.0, '', 91.0, 0),
            ('2026-07-17', 'HP6900041', 'HP6900041', 2, SIBLING, SIB_CODE,
             'ดอกลมหัวลูกบล็อก 8mmx45mm เมต้า', 'ขจรศิลป์', 10.0, 'อัน', 21.0, '', 210.0, 0),
        ])
    conn.executemany(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
        " product_name_raw, customer, customer_code, qty, unit, unit_price, discount, total, net,"
        " vat_type, synced_to_stock) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)", [
            ('2024-11-05', 'IV6702971-2', 'IV6702971', META, CODE,
             'ดอกลมหัวลูกบล็อก 8mmx65mm เมต้า', 'วิชัยเคหะภัณฑ์', '14ว001', 30.0, 'อัน', 35.0, '10%',
             945.0, 945.0, 0),
            ('2026-07-18', 'IV6901138-7', 'IV6901138', META, CODE,
             SALE_RAW, 'ภาคภูมิ พาชัย (เล็ก)', '56ภ01', 13.0, 'อัน', 10.0, '', 130.0, 130.0, 1),
            ('2026-07-18', 'IV6901138-6', 'IV6901138', SIBLING, SIB_CODE,
             'ดอกลมหัวลูกบล็อก 8mmx45mm เมต้า', 'ภาคภูมิ พาชัย (เล็ก)', '56ภ01', 5.0, 'อัน', 30.0, '',
             150.0, 150.0, 1),
        ])
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    wacc.recalculate_product_wacc(META, conn)
    wacc.recalculate_product_wacc(SIBLING, conn)
    conn.commit()


@pytest.fixture()
def db(empty_db_conn):
    _seed(empty_db_conn)
    # The fixture must reproduce prod's pre-state, or every test below proves
    # nothing about the real run. Control, asserted once here for all of them.
    row = empty_db_conn.execute(
        "SELECT p.cost_price, s.quantity FROM products p JOIN stock_levels s ON s.product_id=p.id"
        " WHERE p.id=?", (META,)).fetchone()
    assert (row[0], row[1]) == (pytest.approx(7.0), 0), "fixture did not reach prod's pre-state"
    assert empty_db_conn.execute(
        "SELECT cost_price FROM products WHERE id=?", (SIBLING,)).fetchone()[0] == pytest.approx(21.0)
    return empty_db_conn


def _state(conn):
    """Everything the script may write, as one comparable value."""
    q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]
    return {
        'products': q("SELECT id, product_name, unit_type, cost_price, base_sell_price, brand_id,"
                      " family_id, is_active FROM products ORDER BY id"),
        'stock': q("SELECT product_id, quantity FROM stock_levels ORDER BY product_id"),
        'uc': q("SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id"),
        'sales': q("SELECT id, product_id, synced_to_stock, change_token FROM sales_transactions ORDER BY id"),
        'purchase': q("SELECT id, product_id, synced_to_stock, change_token FROM purchase_transactions ORDER BY id"),
        'ledger': q("SELECT id, product_id, quantity_change, reference_no, note FROM transactions ORDER BY id"),
        'cost_ledger': q("SELECT product_id, event_type, reference_no, unit_cost, wacc_after"
                         " FROM product_cost_ledger ORDER BY id"),
        'mapping': q("SELECT bsn_code, product_id, bsn_unit FROM product_code_mapping ORDER BY id"),
        'audit_n': conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
    }


def _cost(conn, pid):
    return conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]


def _stock(conn, pid):
    return conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]


def _belco_pid(conn):
    rows = conn.execute("SELECT id FROM products WHERE product_name=?", (BELCO_NAME,)).fetchall()
    assert len(rows) == 1, rows
    return rows[0][0]


# ── the happy path ──────────────────────────────────────────────────────────

def test_apply_restores_meta_cost_and_prices_belco_at_seven(db):
    code, report = split_belco.run(db, apply=True)
    assert code == 0, report['problems']
    belco = _belco_pid(db)
    assert _cost(db, META) == pytest.approx(24.50, abs=1e-9)
    assert _cost(db, belco) == pytest.approx(7.00, abs=1e-9)
    assert _cost(db, SIBLING) == pytest.approx(21.0), "a product outside the split moved"


def test_apply_moves_exactly_the_two_belco_lines(db):
    split_belco.run(db, apply=True)
    belco = _belco_pid(db)
    on = lambda table, pid: sorted(r[0] for r in db.execute(
        "SELECT doc_no FROM %s WHERE product_id=?" % table, (pid,)))
    assert on('purchase_transactions', belco) == ['HP6900041']
    assert on('sales_transactions', belco) == ['IV6901138-7']
    assert on('purchase_transactions', META) == ['RR6700438']
    assert on('sales_transactions', META) == ['IV6702971-2']
    # the sibling's own lines on the SAME two documents did not follow
    assert on('purchase_transactions', SIBLING) == ['HP6900041']
    assert on('sales_transactions', SIBLING) == ['IV6901138-6']


def test_moved_rows_keep_their_bsn_code_and_the_mapping_stays_on_meta(db):
    split_belco.run(db, apply=True)
    belco = _belco_pid(db)
    codes = {r[0] for r in db.execute(
        "SELECT bsn_code FROM sales_transactions WHERE product_id=? UNION "
        "SELECT bsn_code FROM purchase_transactions WHERE product_id=?", (belco, belco))}
    assert codes == {CODE}
    assert [r[0] for r in db.execute("SELECT product_id FROM product_code_mapping WHERE bsn_code=?",
                                     (CODE,))] == [META]
    assert db.execute("SELECT COUNT(*) FROM product_code_mapping WHERE product_id=?",
                      (belco,)).fetchone()[0] == 0


def test_new_product_takes_the_leads_defaults(db):
    split_belco.run(db, apply=True)
    belco = _belco_pid(db)
    p = db.execute("SELECT unit_type, base_sell_price, family_id, is_active, brand_id, category_id,"
                   " sub_category, size FROM products WHERE id=?", (belco,)).fetchone()
    assert tuple(p) == ('ตัว', 0.0, None, 1, None, 10, 'ดอกลมหัวลูกบล็อก', '8mmx65mm')
    ratios = dict(db.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (belco,)))
    assert ratios == {'อัน': 1.0, 'อน': 1.0}


def test_uses_an_existing_belco_brand_when_there_is_one(db):
    db.execute("INSERT INTO brands (id, code, name) VALUES (90, 'belco', 'BELCO')")
    db.commit()
    split_belco.run(db, apply=True)
    assert db.execute("SELECT brand_id FROM products WHERE id=?", (_belco_pid(db),)).fetchone()[0] == 90


def test_both_ledgers_replayed_stock_zero_and_ledger_equals_stock(db):
    split_belco.run(db, apply=True)
    belco = _belco_pid(db)
    legs = sorted(tuple(r) for r in db.execute(
        "SELECT reference_no, quantity_change FROM transactions WHERE product_id=?"
        " AND note LIKE 'BSN%'", (belco,)))
    assert legs == [('HP6900041', 13), ('IV6901138-7', -13)]
    for pid in (META, belco):
        assert _stock(db, pid) == 0
        led = db.execute("SELECT COALESCE(SUM(quantity_change),0) FROM transactions WHERE product_id=?",
                         (pid,)).fetchone()[0]
        assert led == _stock(db, pid)


def test_cost_ledgers_carry_one_purchase_each(db):
    split_belco.run(db, apply=True)
    belco = _belco_pid(db)
    rows = lambda pid: [tuple(r) for r in db.execute(
        "SELECT event_type, reference_no, unit_cost FROM product_cost_ledger WHERE product_id=?"
        " ORDER BY id", (pid,))]
    assert rows(META) == [('PURCHASE', 'RR6700438', 24.5)]
    assert rows(belco) == [('PURCHASE', 'HP6900041', 7.0)]


def test_the_repoint_is_declared_and_audited(db):
    split_belco.run(db, apply=True)
    for table, doc in (('purchase_transactions', 'HP6900041'), ('sales_transactions', 'IV6901138-7')):
        src, actor, reason = db.execute(
            "SELECT change_source, change_actor, change_reason FROM %s WHERE doc_no=? AND bsn_code=?"
            % table, (doc, CODE)).fetchone()
        assert src == 'manual' and actor == split_belco.ACTOR
        assert '#582' in reason
        n = db.execute("SELECT COUNT(*) FROM audit_log WHERE table_name=? AND action='UPDATE'"
                       " AND change_reason LIKE '%#582%'", (table,)).fetchone()[0]
        assert n == 1, "%s: expected exactly one audited #582 update, got %d" % (table, n)


def test_dry_run_writes_nothing_but_computes_the_after_state(db):
    before = _state(db)
    code, report = split_belco.run(db, apply=False)
    assert code == 0, report['problems']
    # it really did the work inside the transaction...
    assert report['after'][META]['cost'] == pytest.approx(24.50)
    assert report['after']['belco']['cost'] == pytest.approx(7.00)
    # ...and rolled all of it back
    assert _state(db) == before


def test_refuses_to_run_twice(db):
    split_belco.run(db, apply=True)
    after_first = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert code == 2
    assert _state(db) == after_first


# ── preconditions: break each one, assert it LANDED, assert refusal + no write ─

# (mutation SQL, landed-check SQL, landed value, message fragment)
PRECONDITION_BREAKS = {
    'meta product renamed': (
        "UPDATE products SET product_name='x' WHERE id=1305",
        "SELECT product_name FROM products WHERE id=1305", 'x', 'wrong DB'),
    'meta cost not 7.00': (
        "UPDATE products SET cost_price=24.5 WHERE id=1305",
        "SELECT cost_price FROM products WHERE id=1305", 24.5, 'cost_price'),
    'meta stock not 0': (
        "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note)"
        " VALUES (1305, 'ADJUST', 4, 'unit', 'นับจริง')",
        "SELECT quantity FROM stock_levels WHERE product_id=1305", 4, 'pid 1305 stock'),
    'purchase line gone': (
        "DELETE FROM purchase_transactions WHERE doc_no='HP6900041' AND bsn_code='528ด8655'",
        "SELECT COUNT(*) FROM purchase_transactions WHERE doc_no='HP6900041' AND bsn_code='528ด8655'",
        0, 'HP6900041: 0 row(s) match'),
    'sale line name differs': (
        "DELETE FROM sales_transactions WHERE doc_no='IV6901138-7';"
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
        " product_name_raw, qty, unit, unit_price, net, synced_to_stock)"
        " VALUES ('2026-07-18','IV6901138-7','IV6901138',1305,'528ด8655',"
        " 'ดอกลมหัวลูกบล็อก 8mmx65mm เมต้า',13,'อัน',10,130,1)",
        "SELECT product_name_raw FROM sales_transactions WHERE doc_no='IV6901138-7'",
        'ดอกลมหัวลูกบล็อก 8mmx65mm เมต้า', 'IV6901138-7: 0 row(s) match'),
    'sale line duplicated': (
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
        " product_name_raw, qty, unit, unit_price, net, synced_to_stock)"
        " VALUES ('2026-07-18','IV6901138-7','IV6901138',1305,'528ด8655',"
        " 'ดอกลมหัวลูกบล็อก 8mmx65mm BELCO',13,'อัน',10,130,0)",
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_no='IV6901138-7'", 2, 'IV6901138-7: 2 row(s) match'),
    'meta has an unexpected extra line': (
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
        " product_name_raw, qty, unit, unit_price, net, synced_to_stock)"
        " VALUES ('2026-09-01','IV6901500-1','IV6901500',1305,'528ด8655','x',1,'อัน',35,35,0)",
        "SELECT COUNT(*) FROM sales_transactions WHERE product_id=1305", 3, 'sales_transactions lines are'),
    'unsynced line on meta': (
        "UPDATE sales_transactions SET synced_to_stock=0 WHERE doc_no='IV6702971-2'",
        "SELECT synced_to_stock FROM sales_transactions WHERE doc_no='IV6702971-2'", 0, 'unsynced'),
    'meta unit ratio changed': (
        "UPDATE unit_conversions SET ratio=12 WHERE product_id=1305 AND bsn_unit='อน'",
        "SELECT ratio FROM unit_conversions WHERE product_id=1305 AND bsn_unit='อน'", 12.0,
        'unit_conversions'),
    'belco product already exists': (
        "INSERT INTO products (product_name) VALUES ('ดอกลมหัวลูกบล็อก BELCO 8mmx65mm')",
        "SELECT COUNT(*) FROM products WHERE product_name='ดอกลมหัวลูกบล็อก BELCO 8mmx65mm'",
        1, 'already exists'),
    'two belco brands': (
        "INSERT INTO brands (code, name) VALUES ('belco', 'BELCO'), ('belco2', 'Belco')",
        "SELECT COUNT(*) FROM brands WHERE upper(name)='BELCO'", 2, 'BELCO brand'),
}


@pytest.mark.parametrize('case', sorted(PRECONDITION_BREAKS))
def test_precondition_break_refuses_and_writes_nothing(db, case):
    mutate, landed_sql, landed, fragment = PRECONDITION_BREAKS[case]
    db.executescript(mutate)
    db.commit()
    assert db.execute(landed_sql).fetchone()[0] == landed, "the mutation did not land"
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert code == 2, report
    assert any(fragment in p for p in report['problems']), report['problems']
    assert _state(db) == before


# ── invariants: inject a fault mid-run, assert it RAN, assert rollback ───────

def _count_calls(monkeypatch, module, name, wrapper):
    calls = []
    real = getattr(module, name)

    def spy(*a, **kw):
        calls.append(1)
        return wrapper(real, *a, **kw)
    monkeypatch.setattr(module, name, spy)
    return calls


def _assert_rolled_back(db, before, code, report, fragment):
    assert code == 1, report
    assert any(fragment in p for p in report['problems']), report['problems']
    assert _state(db) == before


def test_invariant_cost_catches_a_skipped_wacc_recalc(db, monkeypatch):
    from models import wacc
    calls = _count_calls(monkeypatch, wacc, 'recalculate_product_wacc', lambda real, *a, **kw: None)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert calls, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'pid 1305 cost_price')
    assert any('expected 7.00' in p for p in report['problems']), report['problems']


def test_invariant_no_movement_lost_catches_a_skipped_purchase_replay(db, monkeypatch):
    from models import bsn_sync

    def skip_purchases(real, conn, table, file_type, **kw):
        if table != 'purchase_transactions':
            return real(conn, table, file_type, **kw)
    calls = _count_calls(monkeypatch, bsn_sync, '_sync_bsn_to_stock', skip_purchases)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert len(calls) == 2, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'replay lost')


def test_invariant_movement_set_catches_a_lost_zero_quantity_row(db, monkeypatch):
    """The one fault only the movement multiset can see: 1305's zero-quantity
    back-solved opening row vanishing moves no stock, no cost, no identity and
    strands no BSN leg — a DELETE pattern one notch too wide would do exactly this."""
    from models import bsn_sync

    def then_drop_plug(real, conn, table, file_type, **kw):
        real(conn, table, file_type, **kw)
        conn.execute("DELETE FROM transactions WHERE product_id=1305 AND note='ยอดยกมา (back-solved)'")
    calls = _count_calls(monkeypatch, bsn_sync, '_sync_bsn_to_stock', then_drop_plug)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert calls, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'ledger movement set changed')


def test_invariant_ledger_equals_stock_catches_a_stock_level_write(db, monkeypatch):
    from models import bsn_sync

    def then_corrupt(real, conn, table, file_type, **kw):
        real(conn, table, file_type, **kw)
        conn.execute("UPDATE stock_levels SET quantity = quantity + 3 WHERE product_id = 1305")
    calls = _count_calls(monkeypatch, bsn_sync, '_sync_bsn_to_stock', then_corrupt)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert calls, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'SUM(ledger)')


def test_invariant_orphan_ledger_catches_a_stranded_leg(db, monkeypatch):
    from models import bsn_sync

    def then_strand(real, conn, table, file_type, **kw):
        real(conn, table, file_type, **kw)
        if table == 'purchase_transactions':
            # a sale leg left behind on 1305 after its source row moved away,
            # balanced so stock and the ledger identity still look fine
            conn.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
                         " reference_no, note, created_at) VALUES (1305, 'OUT', -13, 'unit',"
                         " 'IV6901138-7', 'BSN ขาย', '2026-07-18 00:00:00')")
            conn.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
                         " note) VALUES (1305, 'ADJUST', 13, 'unit', 'balance')")
    calls = _count_calls(monkeypatch, bsn_sync, '_sync_bsn_to_stock', then_strand)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert calls, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'orphan')


def test_invariant_other_products_catches_a_neighbour_change(db, monkeypatch):
    from models import wacc

    def then_touch_sibling(real, pid, conn=None, **kw):
        out = real(pid, conn, **kw)
        conn.execute("UPDATE products SET cost_price = 99 WHERE id = 1304")
        return out
    calls = _count_calls(monkeypatch, wacc, 'recalculate_product_wacc', then_touch_sibling)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert calls, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'outside the split')


def test_invariant_exactly_two_rows_catches_a_third_repoint(db, monkeypatch):
    from models import _shared

    def and_one_more(real, conn, table, row_id, changes, **kw):
        real(conn, table, row_id, changes, **kw)
        if table == 'sales_transactions':
            other = conn.execute("SELECT id FROM sales_transactions WHERE doc_no='IV6702971-2'").fetchone()[0]
            real(conn, table, other, changes, **kw)
    calls = _count_calls(monkeypatch, _shared, 'declared_update', and_one_more)
    before = _state(db)
    code, report = split_belco.run(db, apply=True)
    assert calls, "the injected fault never ran"
    _assert_rolled_back(db, before, code, report, 'source row')
