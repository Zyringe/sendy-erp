"""TDD for scripts/2026_09_19_rebase_689_767.py — #586, pid 689 + 767.

Written BEFORE the script (erp-engineering-discipline: anything mutating
`transactions` / `stock_levels` starts from a failing test).

Each fixture reproduces the product's REAL pre-state on prod (read from
prod-2026-09-19T0820Z-post-uc4.db): unit_type, every ratio at 1.0, cost, base,
opening plug, stock, tier, and the exact bug rows. The ordinary bills are fewer
than on prod but net to the same totals, so the recomputed opening is the same
number prod gives: 689 -> -1 คู่ at the tail, 767 -> +243 ม้วน at the head. The
ledger is posted by the app's own sync from those bills, never typed in.

The script is driven through main() against the DB FILE, because the two things
the 1050/1320 rehearsal caught and its unit tests could not (sys.path derived
from __file__, row_factory=Row) live in main().

The last block is break-it-once, executable: each guard is deleted from a copy
of the script's source, the mutation is asserted to have landed, and the
mutant must then FAIL to refuse the fixture the original refuses.
"""
import importlib.util
import pathlib
import sqlite3
import types

import pytest
from tests._pre_mig590 import sign_raw_connections  # noqa: E402

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_19_rebase_689_767.py"
_ENGINE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_19_gross_to_piece.py"

SOURCE = 'script:2026_09_19_rebase_689_767'
OLD_SOURCE = 'script:2026_09_19_gross_to_piece'
OPENING_NOTE = 'ยอดยกมา (back-solved)'
RECONCILE_NOTE = 'ปรับยอดคงเหลือ (แปลงหน่วย 2026-09-19)'


def _load(src=None):
    """The script as a module. `src` replaces its text (break-it-once)."""
    if src is None:
        spec = importlib.util.spec_from_file_location("rebase_689_767", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType("rebase_689_767_mutant")
    mod.__file__ = str(_SCRIPT)          # engine discovery keys on it
    exec(compile(src, str(_SCRIPT), "exec"), mod.__dict__)
    return mod


def _engine():
    spec = importlib.util.spec_from_file_location("gross_to_piece", _ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fixture ─────────────────────────────────────────────────────────────────

def _bill(conn, table, pid, date, doc, qty, unit, price, code):
    if table == 'sales':
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty, unit,"
            " unit_price, net, vat_type, synced_to_stock, bsn_code)"
            " VALUES (?,?,?,?,?,?,?,?,1,0,?)",
            (date, doc, doc.split('-')[0], pid, qty, unit, price, round(qty * price, 2), code))
    else:
        conn.execute(
            "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, qty, unit,"
            " unit_price, net, synced_to_stock, bsn_code, line_seq)"
            " VALUES (?,?,?,?,?,?,?,?,0,?,1)",
            (date, doc, doc, pid, qty, unit, price, round(qty * price, 2), code))


def _product(conn, pid, name, unit, cost, base, units, bills, opening, code):
    conn.execute(
        "INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
        " opening_cost, low_stock_threshold, is_active) VALUES (?,?,?,?,?,?,10,1)",
        (pid, name, unit, cost, base, cost))
    for u in units:
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,1.0)",
                     (pid, u))
    for b in bills:
        _bill(conn, *b[:1], pid, *b[1:], code)
    # post the buggy ledger through the app's own engine, then the back-solved plug
    from models import bsn_sync, wacc
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note, created_at)"
        " VALUES (?, 'ADJUST', ?, 'unit', ?, '2024-01-03 00:00:00')", (pid, opening, OPENING_NOTE))
    wacc.recalculate_product_wacc(pid, conn)


GLOVE_BILLS = [
    # the bug: 11 คู่ at ฿24.17 (x12 = ฿290.04, the dozen price) posted as -11 โหล
    ('sales', '2024-08-07', 'IV6702067-4', 11.0, 'คู่', 24.17),
    ('purchase', '2024-08-09', 'HP6700106', 12.0, 'หล', 180.0),
    ('sales', '2024-12-24', 'IV6703509-7', 6.0, 'โหล', 285.0),
    ('purchase', '2025-09-03', 'HP6800106', 12.0, 'หล', 180.0),
    ('sales', '2025-09-24', 'IV6802402-8', 10.0, 'โหล', 266.36),
]
TAPE_BILLS = [
    ('purchase', '2024-02-13', 'RR6700064', 100.0, 'แพ', 29.2125),
    ('sales', '2024-02-14', 'IV6700390-1', 33.0, 'แพ็ค', 40.0),
    # the bug: 150 ม้วน at ฿13.33 (x3 = ฿39.99, the pack price) posted as -150 แพ็ค, twice
    ('sales', '2025-01-09', 'IV6800087-9', 150.0, 'ม้วน', 13.33),
    ('sales', '2026-01-06', 'IV6900023-8', 150.0, 'ม้วน', 13.33),
]


def _seed(conn):
    _product(conn, 689, 'ถุงมือยาง S สีส้ม', 'โหล', 181.9, 290.0, ('คู่', 'หล', 'โหล'),
             GLOVE_BILLS, 10, '900ถ2120')
    _product(conn, 767, 'ผ้ายิปซั่ม Eagle One', 'แพ็ค', 29.21, 40.0, ('ม้วน', 'แพ', 'แพ็ค'),
             TAPE_BILLS, 281, '555ผ5000')
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (767, '1 แพค', 40.0)")
    # a sibling in the same family shape (690 M): Put ruled it is NOT touched
    _product(conn, 690, 'ถุงมือยาง M สีส้ม', 'โหล', 181.9, 290.0, ('คู่', 'หล', 'โหล'),
             [('sales', '2024-09-01', 'IV6799999-1', 5.0, 'คู่', 24.17),
              ('purchase', '2024-08-20', 'HP6799999', 12.0, 'หล', 180.0)], 3, '900ถ2121')
    conn.commit()



@pytest.fixture(autouse=True)
def _pre_mig590_world(monkeypatch):
    """A dated one-off script's raw connections, in the world it ran in (#590).
    See tests/_pre_mig590.py."""
    sign_raw_connections(monkeypatch)

@pytest.fixture()
def db(empty_db, empty_db_conn):
    _seed(empty_db_conn)
    return str(empty_db)


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _run(path, *extra, mod=None):
    return (mod or _load()).main(['--db', path, *extra])


def _state(path):
    """Everything the script may touch, as one comparable value."""
    c = _conn(path)
    try:
        return [
            [tuple(r) for r in c.execute(
                "SELECT id, unit_type, cost_price, opening_cost, base_sell_price, low_stock_threshold"
                " FROM products ORDER BY id")],
            [tuple(r) for r in c.execute("SELECT product_id, quantity FROM stock_levels ORDER BY 1")],
            [tuple(r) for r in c.execute(
                "SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY 1, 2")],
            [tuple(r) for r in c.execute(
                "SELECT id, product_id, qty_label, price FROM product_price_tiers ORDER BY 1")],
            [tuple(r) for r in c.execute(
                "SELECT id, product_id, txn_type, quantity_change, reference_no, note, created_at"
                " FROM transactions ORDER BY id")],
            [tuple(r) for r in c.execute(
                "SELECT id, product_id, qty_change, unit_cost, stock_after, wacc_after, note"
                " FROM product_cost_ledger ORDER BY id")],
        ]
    finally:
        c.close()


def _legs(path, pid):
    c = _conn(path)
    try:
        return {r['reference_no']: r['quantity_change'] for r in c.execute(
            "SELECT reference_no, quantity_change FROM transactions"
            " WHERE product_id=? AND note LIKE 'BSN%'", (pid,))}
    finally:
        c.close()


def _one(path, sql, *args):
    c = _conn(path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


# ── the fixture reproduces prod's pre-state ─────────────────────────────────

def test_fixture_reproduces_the_prod_bug_shape(db):
    """CONTROL for everything below: the bug is present before the script runs."""
    assert _legs(db, 689)['IV6702067-4'] == -11, "11 คู่ posted as -11 โหล"
    assert _legs(db, 767)['IV6800087-9'] == -150
    assert _one(db, "SELECT quantity FROM stock_levels WHERE product_id=689")[0] == 7
    assert _one(db, "SELECT quantity FROM stock_levels WHERE product_id=767")[0] == 48
    assert _one(db, "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id IN (689, 767)")[0] >= 4


# ── the conversion ──────────────────────────────────────────────────────────

def test_689_every_bill_rederives_from_its_own_unit(db):
    assert _run(db, '--apply') == 0
    legs = _legs(db, 689)
    assert len(legs) == 5, legs
    assert legs['IV6702067-4'] == -11, "a คู่ bill is now correct exactly as written"
    assert legs['IV6703509-7'] == -72 and legs['IV6802402-8'] == -120, "โหล sales x12"
    assert legs['HP6700106'] == 144 and legs['HP6800106'] == 144, "หล purchases x12"


def test_767_every_bill_rederives_from_its_own_unit(db):
    assert _run(db, '--apply') == 0
    legs = _legs(db, 767)
    assert len(legs) == 4, legs
    assert legs['IV6800087-9'] == -150 and legs['IV6900023-8'] == -150, "ม้วน bills stand"
    assert legs['IV6700390-1'] == -99, "แพ็ค sales x3"
    assert legs['RR6700064'] == 300, "แพ purchases x3"


def test_stock_keeps_its_physical_quantity(db):
    """Put: 7 โหล -> 84 คู่, 48 แพ็ค -> 144 ม้วน. Not the same NUMBER, the same goods."""
    assert _run(db, '--apply') == 0
    for pid, want in ((689, 84), (767, 144)):
        assert _one(db, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)[0] == want
        assert _one(db, "SELECT SUM(quantity_change) FROM transactions WHERE product_id=?", pid)[0] == want


def test_689_negative_opening_goes_to_a_labelled_tail_row(db):
    assert _run(db, '--apply') == 0
    assert _one(db, "SELECT COUNT(*) FROM transactions WHERE product_id=689 AND note=?", OPENING_NOTE)[0] == 0
    rows = _conn(db).execute(
        "SELECT quantity_change, created_at FROM transactions WHERE product_id=689 AND note=?",
        (RECONCILE_NOTE,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == -1, rows
    tail = _one(db, "SELECT MAX(created_at) FROM transactions WHERE product_id=689 AND note<>?",
                RECONCILE_NOTE)[0]
    assert rows[0][1] == tail, "the reconcile sits AT the tail, after every movement"


def test_767_positive_opening_sits_strictly_before_the_head(db):
    assert _run(db, '--apply') == 0
    rows = _conn(db).execute(
        "SELECT quantity_change, created_at FROM transactions WHERE product_id=767 AND note=?",
        (OPENING_NOTE,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == 243, rows
    head = _one(db, "SELECT MIN(created_at) FROM transactions WHERE product_id=767 AND note<>?",
                OPENING_NOTE)[0]
    assert rows[0][1] < head, "WACC sorts IN first on a tie, so a tie is not before the head"
    assert _one(db, "SELECT COUNT(*) FROM transactions WHERE product_id=767 AND note=?",
                RECONCILE_NOTE)[0] == 0


def test_prices_and_ratios(db):
    assert _run(db, '--apply') == 0
    p = _one(db, "SELECT unit_type, cost_price, opening_cost, base_sell_price FROM products WHERE id=689")
    assert p[0] == 'คู่'
    assert p[1] == pytest.approx(181.9 / 12, abs=1e-12) and p[2] == pytest.approx(181.9 / 12, abs=1e-12)
    assert p[3] == 24.17, "290/12 = 24.1666 rounds UP"
    p = _one(db, "SELECT unit_type, cost_price, opening_cost, base_sell_price FROM products WHERE id=767")
    assert p[0] == 'ม้วน'
    assert p[1] == pytest.approx(29.21 / 3, abs=1e-12) and p[2] == pytest.approx(29.21 / 3, abs=1e-12)
    assert p[3] == 13.34, "40/3 = 13.333 rounds UP"
    c = _conn(db)
    assert dict(c.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=689")) == \
        {'คู่': 1.0, 'หล': 12.0, 'โหล': 12.0}
    assert dict(c.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=767")) == \
        {'ม้วน': 1.0, 'แพ': 3.0, 'แพ็ค': 3.0}


def test_stock_value_is_conserved(db):
    assert _run(db, '--apply') == 0
    for pid, before in ((689, 7 * 181.9), (767, 48 * 29.21)):
        q, cost = _one(db, "SELECT s.quantity, p.cost_price FROM products p"
                           " JOIN stock_levels s ON s.product_id=p.id WHERE p.id=?", pid)
        assert q * cost == pytest.approx(before, abs=0.005)


def test_tiers_689_gains_a_dozen_767_is_relabelled_in_place(db):
    before_id = _one(db, "SELECT id FROM product_price_tiers WHERE product_id=767")[0]
    assert _run(db, '--apply') == 0
    c = _conn(db)
    assert [tuple(r) for r in c.execute(
        "SELECT qty_label, price FROM product_price_tiers WHERE product_id=689")] == [('1 โหล', 290.0)]
    rows = [tuple(r) for r in c.execute(
        "SELECT id, qty_label, price FROM product_price_tiers WHERE product_id=767")]
    assert rows == [(before_id, '1 แพ็ค', 40.0)], "relabelled, not deleted and re-inserted"


def test_low_stock_threshold_is_kept(db):
    assert _run(db, '--apply') == 0
    for pid in (689, 767):
        assert _one(db, "SELECT low_stock_threshold FROM products WHERE id=?", pid)[0] == 10


def test_cost_ledger_rows_name_THIS_script(db):
    """The engine's note used to hard-code the 1050/1320 script's name."""
    assert _run(db, '--apply') == 0
    notes = [r[0] for r in _conn(db).execute(
        "SELECT note FROM product_cost_ledger WHERE product_id IN (689, 767)")]
    assert len(notes) >= 4, notes
    assert all(n.endswith('(%s)' % SOURCE) for n in notes), notes
    assert not any(OLD_SOURCE in n for n in notes), notes


def test_engine_default_source_is_unchanged(empty_db_conn):
    """The applied script calls rebase() without `source`: its note must not move."""
    eng = _engine()
    assert eng.SOURCE == OLD_SOURCE
    _product(empty_db_conn, 1320, 'ดินสอช่างไม้พระจันทร์แท้', 'ตัว', 989.4, 1250.0,
             ('กร', 'ตัว', 'แท่ง'), [('purchase', '2025-02-12', 'RR6800080', 0.5, 'กร', 989.4)],
             0, '555ด1000')
    eng.rebase(empty_db_conn, 1320, 'แท่ง', 144, 8.69, 72)
    notes = [r[0] for r in empty_db_conn.execute(
        "SELECT note FROM product_cost_ledger WHERE product_id=1320")]
    assert len(notes) >= 1, notes
    assert all(n.endswith('(%s)' % OLD_SOURCE) for n in notes), notes


def test_siblings_and_everything_else_are_untouched(db):
    before = _state(db)
    assert _run(db, '--apply') == 0
    after = _state(db)
    keep = lambda st: [[r for r in part if 689 not in r[:2] and 767 not in r[:2]] for part in st]
    assert keep(before) == keep(after)
    assert any(r[0] == 690 for r in keep(after)[0]), "control: the sibling is in the comparison"


def test_rehearsal_writes_nothing(db):
    before = _state(db)
    assert _run(db) == 0
    assert _state(db) == before


def test_refuses_to_convert_twice(db):
    assert _run(db, '--apply') == 0
    once = _state(db)
    assert _run(db, '--apply') == 2
    assert _state(db) == once


def test_invariant_failure_rolls_back(db, capsys):
    """Invariants are asserted INSIDE the transaction: a wrong expectation writes nothing."""
    mod = _load()
    mod.PLAN[689]['expect_opening'] = -2
    before = _state(db)
    assert _run(db, '--apply', mod=mod) == 1
    out = capsys.readouterr().out
    assert 'ROLLED BACK' in out and 'opening recomputed to -1, expected -2' in out, out
    assert _state(db) == before


# ── preconditions: each refuses BEFORE the first write ──────────────────────

def _tweak_sql(sql):
    def apply(path):
        c = sqlite3.connect(path)
        c.executescript(sql)
        c.commit()
        c.close()
    return apply


# (id, fixture drift, fragment of the refusal, mutation that deletes the guard)
GUARDS = [
    ('unit_type', "UPDATE products SET unit_type='ตัว' WHERE id=689",
     "unit_type is 'ตัว', expected 'โหล'", ("if unit_type != plan['old_unit']:", "if False:")),
    ('money', "UPDATE products SET cost_price=182.5 WHERE id=767",
     'cost/base/opening_cost moved', ("if money != plan['money']:", "if False:")),
    ('stock', "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note,"
              " created_at) VALUES (689, 'ADJUST', 1, 'unit', 'นับจริง', '2026-09-01 00:00:00')",
     'stock is 8, expected 7', ("if stock != plan['stock']:", "if False:")),
    ('ratios', "UPDATE unit_conversions SET ratio=12 WHERE product_id=689 AND bsn_unit='หล'",
     'unit_conversions', ("if ratios != {u: 1.0 for u in plan['units']}:", "if False:")),
    ('tiers', "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (689, '1 โหล', 290)",
     'tiers are', ("if tiers != plan['tiers_before']:", "if False:")),
    ('opening', "UPDATE transactions SET quantity_change=282 WHERE product_id=767 AND note='%s'"
                % OPENING_NOTE,
     'opening plug', ("if openings != [plan['opening']]:", "if False:")),
    ('bug_row', "UPDATE sales_transactions SET qty=12, change_source='manual', change_actor='test',"
                " change_token='t-586-bug-row', change_reason='ทดสอบว่าสคริปต์ปฏิเสธเมื่อบิลต้นทางเปลี่ยน'"
                " WHERE doc_no='IV6702067-4'",
     'IV6702067-4', ("if not ok:", "if False:")),
    ('unsynced', "UPDATE sales_transactions SET synced_to_stock=0 WHERE doc_no='IV6703509-7'",
     'unsynced', ("if n_unsynced:", "if False:")),
    ('bill_units', "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty,"
                   " unit, unit_price, net, vat_type, synced_to_stock) VALUES ('2025-01-01',"
                   " 'IV6899999-1', 'IV6899999', 689, 1, 'กล่อง', 100, 100, 1, 1)",
     'no unit_conversions row', ("if bill_units - plan['units']:", "if False:")),
    ('foreign', "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,"
                " date_start, is_active) VALUES (767, 'ทดสอบ', 'percent', 10, '2026-01-01', 1)",
     'promotions', ("if n and table not in HANDLED:", "if False:")),
]


def _src():
    return _SCRIPT.read_text(encoding='utf-8')


@pytest.mark.parametrize('gid,drift,fragment,mutation', GUARDS, ids=[g[0] for g in GUARDS])
def test_guard_refuses_before_any_write(db, capsys, gid, drift, fragment, mutation):
    _tweak_sql(drift)(db)
    before = _state(db)
    assert _run(db, '--apply') == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and fragment in out, out
    assert _state(db) == before


@pytest.mark.parametrize('gid,drift,fragment,mutation', GUARDS, ids=[g[0] for g in GUARDS])
def test_break_it_once_guard_is_load_bearing(db, capsys, gid, drift, fragment, mutation):
    """Delete the guard; the refusal naming it must disappear."""
    old, new = mutation
    src = _src()
    assert src.count(old) == 1, "mutation target must exist exactly once: %r" % old
    mutant = src.replace(old, new)
    assert mutant != src and mutant.count(old) == 0, "the mutation did not land"
    _tweak_sql(drift)(db)
    try:
        _run(db, '--apply', mod=_load(mutant))
        err = ''
    except Exception as exc:        # a later step may trip over the drift instead
        err = str(exc)
    assert fragment not in capsys.readouterr().out + err, "guard %s is not load-bearing" % gid


def test_plan_base_must_be_base_over_ratio_rounded_up(db, capsys):
    mod = _load()
    mod.PLAN[767]['new_base'] = 13.33            # rounded DOWN: loose would undercut the pack
    before = _state(db)
    assert _run(db, '--apply', mod=mod) == 2
    assert 'not 40/3 rounded UP' in capsys.readouterr().out
    assert _state(db) == before


def test_break_it_once_plan_base_guard(db, capsys):
    old = "if plan['new_base'] != _ceil2(money[2] / plan['ratio']):"
    src = _src()
    assert src.count(old) == 1
    mod = _load(src.replace(old, "if False:"))
    mod.PLAN[767]['new_base'] = 13.33
    _run(db, '--apply', mod=mod)
    assert 'rounded UP' not in capsys.readouterr().out


INVARIANT_MUTATIONS = [
    # THE difference from 1050/1320: stock is non-zero, so it must be carried
    # into the new unit, not copied as a number
    ('preserve_stock', ("before[pid]['stock'] * plan['ratio']", "before[pid]['stock']"),
     'stock 7 != preserved 84'),
    # attribution: without it every cost-ledger note names the 1050/1320 script
    ('source', ("source=SOURCE)", ")"), 'does not name'),
    ('tiers', ("apply_tiers(conn)\n", "None\n"), 'tier set'),
    # 767's tier must keep its row (and its audit history): delete + insert is refused
    ('tier_in_place', (
        '''    conn.execute("UPDATE product_price_tiers SET qty_label='1 แพ็ค' "\n'''
        '''                 "WHERE product_id=767 AND qty_label='1 แพค'")\n''',
        '''    conn.execute("DELETE FROM product_price_tiers WHERE product_id=767")\n'''
        '''    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "\n'''
        '''                 "VALUES (767, '1 แพ็ค', 40.0)")\n'''),
     'relabelled in place'),
]


@pytest.mark.parametrize('iid,mutation,fragment', INVARIANT_MUTATIONS,
                         ids=[m[0] for m in INVARIANT_MUTATIONS])
def test_break_it_once_invariants_catch_a_wrong_conversion(db, capsys, iid, mutation, fragment):
    old, new = mutation
    src = _src()
    assert src.count(old) == 1, "mutation target must exist exactly once: %r" % old
    mutant = src.replace(old, new)
    assert mutant != src
    before = _state(db)
    assert _run(db, '--apply', mod=_load(mutant)) == 1
    out = capsys.readouterr().out
    assert 'ROLLED BACK' in out and fragment in out, out
    assert _state(db) == before
