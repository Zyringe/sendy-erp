"""TDD for scripts/2026_09_21_fix_rr6700253_unit_1658.py (#586 tail).

The fixture reproduces pid 1658's prod state (read 2026-09-21): the 26in saw's
purchase line on RR6700253 posted as 0.5 ปื้น, its one sale of 6 ปื้น, and the
+5.5 back-solved opening that hides the difference; WACC lands on ฿712.50 from
those rows exactly as on prod. The 24in saw on the same bill is the bystander.
The expected cost after the fix is typed from the bill (356.25 / 6), not from
the script's own arithmetic.
"""
import importlib.util
import pathlib
import sqlite3
import types

import pytest

import actor

_SCRIPT =(pathlib.Path(__file__).resolve().parents[1] / "scripts"
           / "2026_09_21_fix_rr6700253_unit_1658.py")

OPENING_NOTE = 'ยอดยกมา (back-solved)'
CUTOFF = '2026-03-03 23:59:59'
COST_AFTER = 356.25 / 6


def _load(src=None):
    if src is None:
        spec = importlib.util.spec_from_file_location("fix_rr6700253", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType("fix_rr6700253_mutant")
    mod.__file__ = str(_SCRIPT)
    exec(compile(src, str(_SCRIPT), "exec"), mod.__dict__)
    return mod


def _product(conn, pid, name, uc, bills, opening):
    from models import bsn_sync, wacc
    conn.execute(
        "INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
        " opening_cost, low_stock_threshold, is_active) VALUES (?,?,'ตัว',0,0,0,10,1)", (pid, name))
    for u, r in uc.items():
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                     (pid, u, r))
    for kind, date, doc, seq, qty, unit, price, net, code in bills:
        if kind == 'purchase':
            conn.execute(
                "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
                " qty, unit, unit_price, net, synced_to_stock, line_seq)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?)", (date, doc, doc, pid, code, qty, unit, price, net, seq))
        else:
            conn.execute(
                "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
                " qty, unit, unit_price, net, vat_type, synced_to_stock)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,0)",
                (date, doc, doc.split('-')[0], pid, code, qty, unit, price, net))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
    if opening:
        conn.execute(
            "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note, created_at)"
            " VALUES (?, 'ADJUST', ?, 'unit', ?, '2024-01-03 00:00:00')", (pid, opening, OPENING_NOTE))
    wacc.recalculate_product_wacc(pid, conn)


def _seed(conn):
    conn.execute("INSERT OR REPLACE INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'หล', 'โหล')")
    _product(conn, 1658, 'เลื่อยลันดา Eagle One 26in', {'ปื้น': 1.0, 'โหล': 12.0}, [
        ('purchase', '2024-06-19', 'RR6700253', 3, 0.5, 'ปื้น', 1000.0, 356.25, '633ล5026'),
        ('sales', '2024-06-21', 'IV6701603-3', None, 6.0, 'ปื้น', 83.34, 500.04, '633ล5026'),
    ], 5.5)
    _product(conn, 1009, 'เลื่อยลันดา Eagle One 24in', {'โหล': 12.0}, [
        ('purchase', '2024-06-19', 'RR6700253', 2, 0.5, 'โหล', 950.0, 338.44, '633ล5024'),
        ('sales', '2024-07-01', 'IV6799009-1', None, 6.0, 'ตัว', 80.0, 480.0, '633ล5024'),
    ], 0)
    conn.commit()


@pytest.fixture()
def db(empty_db, empty_db_conn):
    _seed(empty_db_conn)
    return str(empty_db)


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _q(path, sql, *args):
    c = _conn(path)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _one(path, sql, *args):
    rows = _q(path, sql, *args)
    return rows[0][0] if rows else None


def _run(path, *extra, mod=None):
    return (mod or _load()).main(['--db', path, *extra])


def _state(path):
    return [
        _q(path, "SELECT id, unit_type, cost_price, opening_cost FROM products ORDER BY id"),
        _q(path, "SELECT product_id, quantity FROM stock_levels ORDER BY 1"),
        _q(path, "SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY 1, 2"),
        _q(path, "SELECT id, doc_no, product_id, qty, unit, net, change_source FROM purchase_transactions ORDER BY id"),
        _q(path, "SELECT id, product_id, txn_type, quantity_change, reference_no, note, created_at"
                 " FROM transactions ORDER BY id"),
        _q(path, "SELECT id, product_id, qty_change, unit_cost, stock_after, wacc_after"
                 " FROM product_cost_ledger ORDER BY id"),
        _q(path, "SELECT COUNT(*) FROM product_price_history"),
    ]


def _leg(path, doc, pid=1658):
    return _one(path, "SELECT quantity_change FROM transactions WHERE product_id=? AND reference_no=?"
                      " AND note LIKE 'BSN%'", pid, doc)


def _cost(path, pid=1658):
    return _one(path, "SELECT cost_price FROM products WHERE id=?", pid)


def _stock(path, pid=1658):
    return _one(path, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)


def _pinned(path, pid=1658):
    return _one(path, "SELECT COALESCE(SUM(quantity_change),0) FROM transactions WHERE product_id=?"
                      " AND created_at <= ?", pid, CUTOFF)


# ── control: the fixture carries prod's bug ─────────────────────────────────

def test_fixture_reproduces_prod(db):
    assert _leg(db, 'RR6700253') == 0.5, "0.5 โหล posted as 0.5 ปื้น"
    assert _leg(db, 'IV6701603-3') == -6
    assert _stock(db) == 0 and _pinned(db) == 0
    assert _q(db, "SELECT quantity_change FROM transactions WHERE product_id=1658 AND note=?",
              OPENING_NOTE) == [(5.5,)]
    assert abs(_cost(db) - 712.5) < 1e-9, _cost(db)


# ── the fix ─────────────────────────────────────────────────────────────────

def test_rehearsal_writes_nothing(db, capsys):
    before = _state(db)
    assert _run(db) == 0
    assert 'REHEARSAL' in capsys.readouterr().out
    assert _state(db) == before


def test_line_becomes_a_dozen_and_posts_six(db):
    assert _run(db, '--apply') == 0
    assert _q(db, "SELECT qty, unit, unit_price, net FROM purchase_transactions WHERE doc_no='RR6700253'"
                  " AND bsn_code='633ล5026'") == [(0.5, 'โหล', 1000.0, 356.25)]
    assert _leg(db, 'RR6700253') == 6
    assert _leg(db, 'IV6701603-3') == -6


def test_the_count_is_kept_and_the_plug_is_gone(db):
    assert _run(db, '--apply') == 0
    assert _stock(db) == 0
    assert _pinned(db) == 0
    assert _one(db, "SELECT SUM(quantity_change) FROM transactions WHERE product_id=1658") == 0
    assert _q(db, "SELECT * FROM transactions WHERE product_id=1658 AND note=?", OPENING_NOTE) == []


def test_cost_lands_on_the_bill_price_per_piece(db):
    hist_before = _q(db, "SELECT field_name FROM product_price_history WHERE product_id=1658")
    assert _run(db, '--apply') == 0
    assert abs(_cost(db) - COST_AFTER) < 1e-9, _cost(db)
    new = _q(db, "SELECT field_name, old_value, new_value FROM product_price_history"
                 " WHERE product_id=1658")[len(hist_before):]
    assert len(new) == 1 and new[0][0] == 'cost_price', new
    assert abs(new[0][1] - 712.5) < 1e-9 and abs(new[0][2] - COST_AFTER) < 1e-9


def test_the_edit_is_declared(db):
    assert _run(db, '--apply') == 0
    src, actor, reason, token = _q(db, "SELECT change_source, change_actor, change_reason, change_token"
                                       " FROM purchase_transactions WHERE doc_no='RR6700253'"
                                       " AND bsn_code='633ล5026'")[0]
    assert src == 'manual' and actor == 'script:2026_09_21_fix_rr6700253_unit_1658'
    assert 'RR6700253' in reason and token


def test_the_24in_line_on_the_same_bill_is_untouched(db):
    before = (_q(db, "SELECT * FROM purchase_transactions WHERE product_id=1009"),
              _leg(db, 'RR6700253', pid=1009), _cost(db, 1009), _stock(db, 1009))
    assert _run(db, '--apply') == 0
    after = (_q(db, "SELECT * FROM purchase_transactions WHERE product_id=1009"),
             _leg(db, 'RR6700253', pid=1009), _cost(db, 1009), _stock(db, 1009))
    assert after == before and before[1] == 6


def test_a_second_run_is_refused(db, capsys):
    assert _run(db, '--apply') == 0
    after = _state(db)
    capsys.readouterr()
    assert _run(db, '--apply') == 2
    assert 'already' in capsys.readouterr().out
    assert _state(db) == after


def test_committed_run_reports_ok_on_a_fresh_connection(db, capsys):
    assert _run(db, '--apply') == 0
    out = capsys.readouterr().out
    assert 'COMMITTED' in out and out.count('OK ') == 1 and 'BAD' not in out, out


# ── the unit comes from the map, not from the script ────────────────────────

def test_the_word_written_is_the_maps_word(db, capsys):
    """Change what the map says `หล` means: the script must follow the map, so it
    now looks for a conversion row under THAT word and refuses."""
    c = sqlite3.connect(db)
    c.execute("UPDATE unit_map SET word='โหลทดสอบ' WHERE book='BSN5657' AND spelling='หล'")
    c.commit()
    c.close()
    before = _state(db)
    assert _run(db, '--apply') == 2
    out = capsys.readouterr().out
    assert 'unit_conversions[โหลทดสอบ]' in out, out
    assert _state(db) == before


def test_refuses_when_the_map_cannot_translate(db, capsys):
    c = sqlite3.connect(db)
    c.execute("DELETE FROM unit_map WHERE spelling='หล'")
    c.commit()
    c.close()
    before = _state(db)
    assert _run(db, '--apply') == 2
    assert 'does not translate' in capsys.readouterr().out
    assert _state(db) == before


# ── guards: each refuses before any write, and each is load-bearing ─────────

def _sql(*stmts):
    def apply(path):
        c = actor.install(sqlite3.connect(path))   # #590: the cost drift needs an actor
        for s in stmts:
            c.execute(s)
        c.commit()
        c.close()
    return apply


def _unsynced_sale(path):
    c = sqlite3.connect(path)
    c.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code, qty,"
              " unit, unit_price, net, vat_type, synced_to_stock) VALUES ('2026-09-01', 'IV6999100-1',"
              " 'IV6999100', 1658, '633ล5026', 1, 'ปื้น', 90, 90, 1, 0)")
    c.commit()
    c.close()


_LINE = "doc_no='RR6700253' AND bsn_code='633ล5026'"
_DECLARE = "change_source='manual', change_actor='test', change_reason='test drift for a guard', change_token=?"

# (id, drift, refusal fragment, guard mutation, rc with the guard off, what
#  the guard-off run must print) — the last two measured, naming each backstop
GUARDS = [
    ('already', lambda p: _declared(p, "unit='โหล'"),
     'already โหล', ("if unit == new_unit:", "if False:"), 2, "expected 'ปื้น'"),
    ('line_drift', lambda p: _declared(p, "net=300.0"),
     'expected 0.5 @ 1000 net 356.25', ("if (qty, price, net) != (QTY, UNIT_PRICE, NET):", "if False:"),
     1, 'qty/price/net moved'),
    ('opening', _sql("UPDATE transactions SET quantity_change=5.0 WHERE product_id=1658 AND note='%s'"
                     % OPENING_NOTE),
     'expected one row of 5.5', ("if len(openings) != 1 or abs(openings[0][0] - EXPECT_OPENING) > 1e-9:", "if False:"),
     1, 'opening plug still there'),
    ('unsynced', _unsynced_sale,
     'unsynced bill row', ("if n_unsynced:", "if False:"), 1, 'stock moved'),
    ('ratio', _sql("UPDATE unit_conversions SET ratio=10.0 WHERE product_id=1658 AND bsn_unit='โหล'"),
     'unit_conversions[โหล] is 10.0', ("if ratios.get(new_unit) != EXPECT_RATIO:", "if False:"),
     1, 'posted 5'),
    # a plan-drift guard: WACC recomputes cost from the bills, so without it the
    # fix still lands correctly and commits
    ('cost', _sql("UPDATE products SET cost_price=700.0 WHERE id=1658"),
     'cost_price is 700.0', ("if abs((prod[1] or 0) - EXPECT_COST_BEFORE) > 1e-9:", "if False:"),
     0, 'COMMITTED'),
]


def _declared(path, set_clause):
    c = sqlite3.connect(path)
    c.execute("UPDATE purchase_transactions SET %s, %s WHERE %s" % (set_clause, _DECLARE, _LINE),
              ('drift-%s' % set_clause,))
    c.commit()
    c.close()


def _src():
    return _SCRIPT.read_text(encoding='utf-8')


def _mutant(old, new):
    src = _src()
    assert src.count(old) == 1, "mutation target must exist exactly once: %r" % old
    mutant = src.replace(old, new)
    assert mutant != src and mutant.count(old) == 0, "the mutation did not land"
    return _load(mutant)


@pytest.mark.parametrize('gid,drift,fragment,mutation,rc_off,backstop', GUARDS,
                         ids=[g[0] for g in GUARDS])
def test_guard_refuses_before_any_write(db, capsys, gid, drift, fragment, mutation, rc_off, backstop):
    drift(db)
    before = _state(db)
    assert _run(db, '--apply') == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and fragment in out, out
    assert _state(db) == before


@pytest.mark.parametrize('gid,drift,fragment,mutation,rc_off,backstop', GUARDS,
                         ids=[g[0] for g in GUARDS])
def test_break_it_once_guard_off_behaves_as_measured(db, capsys, gid, drift, fragment, mutation,
                                                      rc_off, backstop):
    """The guard-off run must COMPLETE (nothing swallowed) and end in its measured
    outcome, so a mutant that dies early or a missing backstop turns this red."""
    mod = _mutant(*mutation)
    drift(db)
    before = _state(db)
    rc = _run(db, '--apply', mod=mod)
    out = capsys.readouterr().out
    assert rc == rc_off, (rc, out)
    assert fragment not in out and backstop in out, out
    if rc_off:
        assert _state(db) == before, "a refused or rolled-back run wrote something"


# ── invariants: a wrong fix must not commit ─────────────────────────────────

INVARIANT_MUTATIONS = [
    # without the plug change the count is broken and 5.5 phantom saws appear
    ('plug_untouched', ("    delta = pinned - _ledger_sum(conn, CUTOFF)\n", "    delta = 0\n"),
     1, 'count pin broken'),
    ('no_wacc', ("    wacc.recalculate_product_wacc(PID, conn)\n", ""), 1, 'expected 59.375'),
    # writing Express's raw code instead of the map's word
    ('raw_code', ("(new_unit, ACTOR, REASON,", "(EXPRESS_UNIT, ACTOR, REASON,"), None, None),
]


@pytest.mark.parametrize('iid,mutation,rc_want,fragment', INVARIANT_MUTATIONS,
                         ids=[m[0] for m in INVARIANT_MUTATIONS])
def test_break_it_once_invariants_catch_a_wrong_fix(db, capsys, iid, mutation, rc_want, fragment):
    mod = _mutant(*mutation)
    before = _state(db)
    rc = _run(db, '--apply', mod=mod)
    out = capsys.readouterr().out
    if rc_want is None:
        assert rc in (1, 2), (rc, out)
    else:
        assert rc == rc_want and 'ROLLED BACK' in out and fragment in out, out
    assert _state(db) == before


def test_break_it_once_wacc_must_run_after_the_plug_is_gone(db, capsys):
    """Moving WACC above the plug change still lands cost on 59.375 (the
    first-purchase branch) but costs the purchase over 11.5 in stock. Only the
    cost-ledger invariant can see that."""
    src = _src()
    call = "    wacc.recalculate_product_wacc(PID, conn)\n    return new_unit, delta\n"
    anchor = "    delta = pinned - _ledger_sum(conn, CUTOFF)\n"
    assert src.count(call) == 1 and src.count(anchor) == 1
    mutant = src.replace(call, "    return new_unit, delta\n").replace(
        anchor, "    wacc.recalculate_product_wacc(PID, conn)\n" + anchor)
    assert mutant.index("wacc.recalculate_product_wacc") < mutant.index(anchor), "the move did not land"
    before = _state(db)
    rc = _run(db, '--apply', mod=_load(mutant))
    out = capsys.readouterr().out
    assert rc == 1 and 'ROLLED BACK' in out and 'cost-ledger purchase row' in out, out
    assert '11.5' in out, out
    assert _state(db) == before
