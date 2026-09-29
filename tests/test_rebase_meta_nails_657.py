"""Guards for scripts/2026_09_29_rebase_meta_nails_657.py — #657, ตะปูคอนกรีต META.

The fixture is family 413 cut down to the four products the script touches plus
one untouched sibling (664) as the control, each shaped the way prod held it on
2026-09-29 (migration 196). The run's own evidence is a rehearsal against a full
prod snapshot; these are the checks that keep meaning something after that.
"""
import importlib.util
import pathlib
import sqlite3

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_29_rebase_meta_nails_657.py"

FAMILY = 413
# pid: name, unit_type, cost, base, stock, conversions
PRODUCTS = {
    664: ('ตะปูคอนกรีต META 18x2.0mm สีดำ', 'กล่องเล็ก', 7.45, 13.0, 240, {'กล่อง': 1.0}),
    678: ('ตะปูคอนกรีต META 45x2.5mm สีขาว', 'กล่องเล็ก', 19.47, 34.0, 80, {}),
    680: ('ตะปูคอนกรีต META 60x2.5mm สีขาว', 'กล่องเล็ก', 25.2, 44.0, 90, {}),
    970: ('ตะปูคอนกรีต META 45x2.0mm สีขาว', 'ตัว', 0.0, 0.0, 0, {'กล่อง': 1.0}),
    973: ('ตะปูคอนกรีต META 18x1.5mm สีดำ', 'ตัว', 0.0, 11.5, 0, {'กล่อง': 1.0, 'กล่องเล็ก': 1.0}),
}
# pid, date, doc, qty (every bill these two ever had is keyed กล่อง)
SALES = [
    (973, '2024-12-18', 'IV6712001', 2.0),
    (973, '2025-05-09', 'IV6805002', 8.0),
    (970, '2024-09-17', 'IV6709003', 10.0),
]


def _load():
    spec = importlib.util.spec_from_file_location("rebase_meta_nails_657", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(conn):
    from models import bsn_sync
    conn.execute("INSERT INTO product_families (id, family_code, display_name) "
                 "VALUES (?, 'META-SCAC434F', 'ตะปูคอรีต')", (FAMILY,))
    conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('*', 'กล.เล็ก', 'กล่องเล็ก')")
    for pid, (name, unit, cost, base, stock, conv) in PRODUCTS.items():
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
            " opening_cost, low_stock_threshold, is_active, family_id)"
            " VALUES (?,?,?,?,?,?,10,1,?)", (pid, name, unit, cost, base, cost, FAMILY))
        for u, r in conv.items():
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                         (pid, u, r))
    conn.execute("INSERT INTO customers (code, name) VALUES ('53ส01', 'ร้านทดสอบ')")
    for pid, date, doc, qty in SALES:
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
            " product_name_raw, customer, customer_code, qty, unit, unit_price, vat_type,"
            " discount, total, net, synced_to_stock)"
            " VALUES (?,?,?,?,'532ต',  'raw','ร้านทดสอบ','53ส01',?,'กล่อง',11.5,1,'',?,?,0)",
            (date, doc, doc, pid, qty, qty * 11.5, qty * 11.5))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=tuple(PRODUCTS))
    for pid, (*_, stock, _conv) in PRODUCTS.items():
        sold = sum(q for p, _d, _n, q in SALES if p == pid)
        conn.execute(
            "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note,"
            " created_at) VALUES (?, 'ADJUST', ?, 'unit', 'ยอดยกมา', '2024-01-03 00:00:00')",
            (pid, stock + sold))
    conn.commit()


@pytest.fixture()
def db(empty_db, empty_db_conn):
    _seed(empty_db_conn)
    return str(empty_db)


def _run(path, *extra, mode='rehearse'):
    return _load().main(['--db', path, '--mode', mode,
                         '--operator', 'pytest', '--reason', 'test #657', *extra])


def _state(path):
    c = sqlite3.connect(path)
    try:
        out = {}
        for pid in PRODUCTS:
            unit, cost, oc, base = c.execute(
                "SELECT unit_type, cost_price, opening_cost, base_sell_price FROM products WHERE id=?",
                (pid,)).fetchone()
            stock = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]
            led = c.execute("SELECT COALESCE(SUM(quantity_change),0) FROM transactions WHERE product_id=?",
                            (pid,)).fetchone()[0]
            conv = dict(c.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                  (pid,)).fetchall())
            out[pid] = dict(unit=unit, cost=cost, oc=oc, base=base, stock=stock, led=led, conv=conv)
        return out
    finally:
        c.close()


def test_fixture_reproduces_prod(db):
    s = _state(db)
    assert s[973]['unit'] == s[970]['unit'] == 'ตัว'
    assert s[973]['stock'] == s[970]['stock'] == 0 == s[973]['led'] == s[970]['led']
    assert s[678]['conv'] == s[680]['conv'] == {}
    assert s[664]['stock'] == 240 == s[664]['led']


def test_family_lands_on_one_base_and_nothing_else_moves(db):
    before = _state(db)
    assert _run(db) == 0
    after = _state(db)
    for pid in PRODUCTS:
        assert after[pid]['unit'] == 'กล่องเล็ก', pid
        assert after[pid]['conv'] == {'กล่อง': 1.0}, pid
        for k in ('cost', 'oc', 'base', 'stock', 'led'):
            assert after[pid][k] == before[pid][k], (pid, k)


def test_every_bill_still_posts_one_box_per_box(db):
    from models import bsn_sync
    assert _run(db) == 0
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        for pid, _d, doc, qty in SALES:
            unit_type = c.execute("SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()[0]
            assert bsn_sync._get_base_qty(c, pid, unit_type, 'กล่อง', qty) == qty, doc
            posted = c.execute("SELECT SUM(-quantity_change) FROM transactions WHERE product_id=? "
                               "AND txn_type='OUT' AND reference_no=?", (pid, doc)).fetchone()[0]
            assert posted == qty, doc
    finally:
        c.close()


def test_973_answers_the_words_its_siblings_answer(db):
    import price_lookup
    assert _run(db) == 0
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        for ask in (None, 'กล่อง', 'กล่องเล็ก', 'กล.เล็ก'):
            sib = price_lookup.resolve_price(c, product_id=664, unit=ask)
            got = price_lookup.resolve_price(c, product_id=973, unit=ask)
            assert sib['answer']['price_per_unit'] == 13.0, ask
            assert got['answer']['price_per_unit'] == 11.5, ask
    finally:
        c.close()


def test_drifted_pre_state_refuses_and_writes_nothing(db, capsys):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note,"
              " created_at) VALUES (970, 'ADJUST', 5, 'unit', 'นับจริง', '2026-09-29 10:00:00')")
    c.commit()
    c.close()
    before = _state(db)
    assert _run(db) == 2
    assert 'REFUSED' in capsys.readouterr().out
    assert _state(db) == before


def test_second_run_refuses(db, capsys):
    assert _run(db) == 0
    once = _state(db)
    assert _run(db) == 2
    assert 'REFUSED' in capsys.readouterr().out
    assert _state(db) == once


def test_rehearse_refuses_a_file_the_app_opens(tmp_path, capsys):
    p = tmp_path / 'inventory.db'
    p.write_bytes(b'')
    assert _run(str(p)) == 2
    assert 'REFUSED' in capsys.readouterr().out


def test_live_needs_confirmation(db, capsys):
    assert _run(db, mode='live') == 2
    assert 'confirm-live' in capsys.readouterr().out
