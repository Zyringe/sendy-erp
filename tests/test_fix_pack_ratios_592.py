"""TDD for scripts/2026_09_19_fix_pack_ratios_592.py (#592).

Written BEFORE the script: it mutates `transactions` / `stock_levels`.

The fixture reproduces each product's prod pre-state (snapshot
prod-2026-09-19T1103Z-post-mig187): unit_type, conversion rows, the exact bills
in the unit being fixed (doc, date, qty), the opening plug and every other
non-BSN ledger row with its timestamp. The ordinary bills are collapsed into one
or two per product that net to the same totals, so the ledger sum up to the
cutoff and today's stock are prod's numbers. The ledger is posted by the app's
own sync from the bills, never typed in.

The expected stock after the fix is the evidence report's table
(Operations/05_analysis-reports/data-quality/unit_ratio_suspects_592_2026-09-19.md),
typed in as an oracle independent of the script's own arithmetic.
"""
import importlib.util
import pathlib
import sqlite3
import types

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_19_fix_pack_ratios_592.py"

OPENING_NOTE = 'ยอดยกมา (back-solved)'
CUTOFF = '2026-03-03 23:59:59'

# pid -> stock after the fix, from the evidence report (count pin kept)
EXPECTED_STOCK = {304: 4100, 623: 52, 575: 494, 578: 275, 577: 272, 576: 65, 574: 20, 926: 0}
# pid -> the compensation that keeps the 2026-02-23 count: (ratio-1) x absorbed qty
EXPECTED_PIN = {304: 0, 623: 9, 575: 8, 578: 44, 577: 4, 576: 4, 574: 4, 926: 8}
RATIO = {304: ('ซอง', 12), 623: ('ซอง', 10), 575: ('ชุด', 5), 578: ('ชุด', 5),
         577: ('แพ็ค', 5), 576: ('ชุด', 5), 574: ('ชุด', 5), 926: ('ชุด', 5)}


def _load(src=None):
    if src is None:
        spec = importlib.util.spec_from_file_location("fix_pack_ratios_592", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType("fix_pack_ratios_592_mutant")
    mod.__file__ = str(_SCRIPT)
    exec(compile(src, str(_SCRIPT), "exec"), mod.__dict__)
    return mod


# ── fixture ─────────────────────────────────────────────────────────────────

# (table, date, doc, qty, unit) — unit prices are irrelevant to stock
PRODUCTS = {
    304: dict(name='ดจ.สแตนเลสซอง Golden Lion 9/64in', unit_type='ดอก', cost=4.0,
              uc={'ซอง': 1.0, 'โหล': 12.0}, opening=4452, code='017ด5115',
              bills=[('sales', '2024-06-01', 'IV6799001-1', 20.0, 'โหล'),
                     ('sales', '2026-05-02', 'IV6900611-1', 3.0, 'ซอง'),
                     ('sales', '2026-06-12', 'IV6900915-4', 3.0, 'ดอก'),
                     ('sales', '2026-08-15', 'IV6901335-1', 6.0, 'ซอง'),
                     ('sales', '2026-08-27', 'IV6901449-2', 1.0, 'ดอก')]),
    623: dict(name='ดจ.สแตนเลส SUNFLOWER ซองแดง 7/32in', unit_type='ดอก', cost=14.5,
              uc={'ช5': 5.0, 'ชุด5': 5.0, 'ซอง': 1.0, 'ดอก': 1.0}, opening=111, code='564ด5114',
              extra=[('2026-06-09 14:14:48', 100, 'นับสต็อกสลับ')],
              bills=[('sales', '2024-08-01', 'IV6799002-1', 110.0, 'ดอก'),
                     ('sales', '2025-12-02', 'IV6802932-1', 1.0, 'ซอง'),
                     ('sales', '2026-06-20', 'IV6900981-1', 1.0, 'ช5'),
                     ('sales', '2026-08-01', 'IV6901231-1', 2.0, 'ซอง'),
                     ('sales', '2026-08-14', 'IV6901331-1', 1.0, 'ซอง'),
                     ('sales', '2026-09-14', 'IV6901557-1', 3.0, 'ดอก'),
                     ('sales', '2026-09-15', 'IV6901579-1', 1.0, 'ซอง')]),
    575: dict(name='กระดาษทรายกลมตีนตุ๊กแก #60-4in', unit_type='แผ่น', cost=1.6,
              uc={'ชุด': 1.0, 'แผ่น': 1.0}, opening=1092, code='999ก9013',
              bills=[('purchase', '2025-06-01', 'HP6799575', 100.0, 'แผ่น'),
                     ('sales', '2025-07-01', 'IV6799575-1', 696.0, 'แผ่น'),
                     ('sales', '2025-12-19', 'IV6803061-1', 2.0, 'ชุด')]),
    578: dict(name='กระดาษทรายกลมตีนตุ๊กแก #120-4in', unit_type='แผ่น', cost=1.6,
              uc={'ชุด': 1.0, 'อัน': 5.0, 'แผ่น': 1.0}, opening=1446, code='999ก9016',
              extra=[('2026-07-03 00:00:00', -1260, 'ล้าง orphan ledger 2026-07-03')],
              bills=[('purchase', '2024-05-01', 'HP6799578', 300.0, 'แผ่น'),
                     ('sales', '2024-10-04', 'IV6702662-2', 10.0, 'ชุด'),
                     ('sales', '2025-07-01', 'IV6799578-1', 200.0, 'แผ่น'),
                     ('sales', '2026-01-05', 'IV6900002-1', 1.0, 'ชุด')]),
    577: dict(name='กระดาษทรายกลมตีนตุ๊กแก #100-4in', unit_type='แผ่น', cost=1.6,
              uc={'แผ่น': 1.0, 'แพ็ค': 1.0}, opening=889, code='999ก9015',
              bills=[('sales', '2025-07-01', 'IV6799577-1', 616.0, 'แผ่น'),
                     ('sales', '2026-02-07', 'IV6900242-1', 1.0, 'แพ็ค')]),
    576: dict(name='กระดาษทรายกลมตีนตุ๊กแก #80-4in', unit_type='แผ่น', cost=1.6,
              uc={'ชุด': 1.0, 'อัน': 1.0, 'แผ่น': 1.0}, opening=432, code='999ก9014',
              extra=[('2026-07-03 00:00:00', -36, 'ล้าง orphan ledger 2026-07-03')],
              bills=[('sales', '2025-07-01', 'IV6799576-1', 225.0, 'แผ่น'),
                     ('sales', '2025-12-15', 'IV6803018-1', 1.0, 'ชุด'),
                     ('sales', '2026-04-01', 'IV6799576-2', 100.0, 'แผ่น'),
                     ('sales', '2026-05-28', 'IV6900784-1', 1.0, 'ชุด')]),
    574: dict(name='กระดาษทรายกลมตีนตุ๊กแก #40-4in', unit_type='แผ่น', cost=1.6,
              uc={'ชุด': 1.0, 'แผ่น': 1.0}, opening=122, code='999ก9012',
              bills=[('sales', '2025-07-01', 'IV6799574-1', 101.0, 'แผ่น'),
                     ('sales', '2025-08-19', 'IV6802105-1', 1.0, 'ชุด')]),
    926: dict(name='กระดาษทรายกลม เกือกม้า #100-4in', unit_type='ตัว', cost=0.0,
              uc={'ชุด': 1.0, 'แผ่น': 1.0}, opening=127, code='900ก1100',
              bills=[('sales', '2024-06-01', 'IV6799926-1', 125.0, 'แผ่น'),
                     ('sales', '2024-12-14', 'IV6703400-1', 2.0, 'ชุด')]),
    # a bystander carrying the same bug shape: NOT in Put's ruling, must not move
    999: dict(name='ดจ.สแตนเลสซอง bystander', unit_type='ดอก', cost=4.0,
              uc={'ซอง': 1.0}, opening=50, code='017ด9999',
              bills=[('sales', '2026-05-05', 'IV6799999-1', 2.0, 'ซอง')]),
}
PROD_STOCK = {304: 4199, 623: 88, 575: 494, 578: 275, 577: 272, 576: 69, 574: 20, 926: 0, 999: 48}
PROD_SUM_TO_CUTOFF = {304: 4212, 623: 0, 575: 494, 578: 1535, 577: 272, 576: 206, 574: 20, 926: 0}


def _bill(conn, table, pid, date, doc, qty, unit, code):
    if table == 'sales':
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty, unit,"
            " unit_price, net, vat_type, synced_to_stock, bsn_code) VALUES (?,?,?,?,?,?,1,?,1,0,?)",
            (date, doc, doc.split('-')[0], pid, qty, unit, qty, code))
    else:
        conn.execute(
            "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, qty, unit,"
            " unit_price, net, synced_to_stock, bsn_code, line_seq) VALUES (?,?,?,?,?,?,1.6,?,0,?,1)",
            (date, doc, doc, pid, qty, unit, qty * 1.6, code))


def _seed(conn):
    from models import bsn_sync, wacc
    for pid, p in PRODUCTS.items():
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
            " opening_cost, low_stock_threshold, is_active) VALUES (?,?,?,?,10,?,10,1)",
            (pid, p['name'], p['unit_type'], p['cost'], p['cost']))
        for u, r in p['uc'].items():
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                         (pid, u, r))
        for table, date, doc, qty, unit in p['bills']:
            _bill(conn, table, pid, date, doc, qty, unit, p['code'])
        bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
        bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
        conn.execute(
            "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note, created_at)"
            " VALUES (?, 'ADJUST', ?, 'unit', ?, '2024-01-03 00:00:00')", (pid, p['opening'], OPENING_NOTE))
        for created_at, qty, note in p.get('extra', ()):
            conn.execute(
                "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note, created_at)"
                " VALUES (?, 'ADJUST', ?, 'unit', ?, ?)", (pid, qty, note, created_at))
        wacc.recalculate_product_wacc(pid, conn)
    conn.commit()


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


def _q(path, sql, *args):
    c = _conn(path)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _stock(path, pid):
    rows = _q(path, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)
    return rows[0][0] if rows else 0


def _sum_to(path, pid, ts=CUTOFF):
    return _q(path, "SELECT COALESCE(SUM(quantity_change),0) FROM transactions"
                    " WHERE product_id=? AND created_at<=?", pid, ts)[0][0]


def _legs(path, pid):
    return dict(_q(path, "SELECT reference_no, quantity_change FROM transactions"
                         " WHERE product_id=? AND note LIKE 'BSN%'", pid))


def _state(path):
    return [
        _q(path, "SELECT id, unit_type, cost_price, opening_cost, base_sell_price FROM products ORDER BY id"),
        _q(path, "SELECT product_id, quantity FROM stock_levels ORDER BY 1"),
        _q(path, "SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY 1, 2"),
        _q(path, "SELECT id, product_id, txn_type, quantity_change, reference_no, note, created_at"
                 " FROM transactions ORDER BY id"),
        _q(path, "SELECT id, product_id, qty_change, unit_cost, stock_after, wacc_after"
                 " FROM product_cost_ledger ORDER BY id"),
        _q(path, "SELECT COUNT(*) FROM product_price_history"),
    ]


# ── control: the fixture carries prod's bug and prod's numbers ──────────────

def test_fixture_reproduces_prod(db):
    for pid, stock in PROD_STOCK.items():
        assert _stock(db, pid) == stock, pid
    for pid, s in PROD_SUM_TO_CUTOFF.items():
        assert _sum_to(db, pid) == s, pid
    assert _legs(db, 304)['IV6901335-1'] == -6, "6 ซอง posted as 6 ดอก"
    assert _legs(db, 623)['IV6802932-1'] == -1
    assert _legs(db, 578)['IV6702662-2'] == -10


# ── the fix ─────────────────────────────────────────────────────────────────

def test_rehearsal_writes_nothing(db, capsys):
    before = _state(db)
    assert _run(db) == 0
    assert 'REHEARSAL' in capsys.readouterr().out
    assert _state(db) == before


def test_stock_after_matches_the_evidence_report(db):
    assert _run(db, '--apply') == 0
    got = {pid: _stock(db, pid) for pid in EXPECTED_STOCK}
    assert got == EXPECTED_STOCK
    for pid in EXPECTED_STOCK:
        led = _q(db, "SELECT SUM(quantity_change) FROM transactions WHERE product_id=?", pid)[0][0]
        assert led == got[pid], pid


def test_every_bill_in_the_unit_posts_qty_times_ratio_and_nothing_else_moves(db):
    before = {pid: _legs(db, pid) for pid in RATIO}
    assert _run(db, '--apply') == 0
    for pid, (unit, r) in RATIO.items():
        bills = dict(_q(db, "SELECT doc_no, qty FROM sales_transactions WHERE product_id=? AND unit=?",
                        pid, unit))
        assert bills, pid
        after = _legs(db, pid)
        assert set(after) == set(before[pid]), pid
        for doc, change in after.items():
            want = -bills[doc] * r if doc in bills else before[pid][doc]
            assert change == want, (pid, doc, change, want)
        ratios = dict(_q(db, "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", pid))
        assert ratios == {**PRODUCTS[pid]['uc'], unit: float(r)}, pid


def test_the_2026_02_23_count_is_kept(db):
    assert _run(db, '--apply') == 0
    for pid, s in PROD_SUM_TO_CUTOFF.items():
        assert _sum_to(db, pid) == s, pid
    # 623's later count (the swap moved onto it on 06-09) is kept too
    assert _sum_to(db, 623, '2026-06-09 14:14:48') == 100


def test_one_compensation_row_per_product_strictly_before_the_head(db):
    heads = {pid: _q(db, "SELECT MIN(created_at) FROM transactions WHERE product_id=?", pid)[0][0]
             for pid in RATIO}
    assert _run(db, '--apply') == 0
    for pid, delta in EXPECTED_PIN.items():
        rows = _q(db, "SELECT quantity_change, created_at, reference_no, note FROM transactions"
                      " WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'", pid)
        if not delta:
            assert rows == [], pid
            continue
        assert len(rows) == 1, (pid, rows)
        qty, at, ref, note = rows[0]
        assert qty == delta, (pid, qty)
        assert at < heads[pid], (pid, at, heads[pid])
        assert ref is None, "reconcile._ledger_check must never see it"
        assert '#592' in note and not note.startswith(('BSN', 'ประวัติขาย'))


def test_cost_is_unchanged_and_no_price_history_is_written(db):
    cost = dict(_q(db, "SELECT id, cost_price FROM products WHERE id IN (%s)"
                       % ','.join(map(str, RATIO))))
    n_hist = _q(db, "SELECT COUNT(*) FROM product_price_history")[0][0]
    assert _run(db, '--apply') == 0
    assert dict(_q(db, "SELECT id, cost_price FROM products WHERE id IN (%s)"
                       % ','.join(map(str, RATIO)))) == cost
    assert _q(db, "SELECT COUNT(*) FROM product_price_history")[0][0] == n_hist


def test_the_bystander_is_untouched(db):
    before = (_legs(db, 999), _stock(db, 999),
              _q(db, "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=999"))
    assert _run(db, '--apply') == 0
    assert (_legs(db, 999), _stock(db, 999),
            _q(db, "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=999")) == before
    assert before[0]['IV6799999-1'] == -2


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
    assert out.count('OK ') == len(RATIO) and 'BAD' not in out, out


# ── guards: each refuses before any write, and each is load-bearing ─────────

def _sql(*stmts):
    def apply(path):
        c = sqlite3.connect(path)
        for s in stmts:
            c.execute(s)
        c.commit()
        c.close()
    return apply


GUARDS = [
    ('twin', _sql("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (578, 'ชด', 1.0)"),
     'normalises to', ("if twins:", "if False:")),
    ('absorbed', _sql("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty,"
                      " unit, unit_price, net, vat_type, synced_to_stock, bsn_code)"
                      " VALUES ('2025-10-01', 'IV6899001-1', 'IV6899001', 575, 1, 'ชุด', 39, 39, 1, 1,"
                      " '999ก9013')"),
     'absorbed bills', ("if absorbed != plan['absorbed']:", "if False:")),
    ('unsynced', _sql("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty,"
                      " unit, unit_price, net, vat_type, synced_to_stock, bsn_code)"
                      " VALUES ('2026-09-18', 'IV6999001-1', 'IV6999001', 574, 3, 'แผ่น', 6, 18, 1, 0,"
                      " '999ก9012')"),
     'unsynced', ("if n_unsynced:", "if False:")),
    ('between_counts', _sql("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty,"
                            " unit, unit_price, net, vat_type, synced_to_stock, bsn_code)"
                            " VALUES ('2026-05-01', 'IV6999002-1', 'IV6999002', 623, 1, 'ซอง', 220, 220,"
                            " 1, 1, '564ด5114')"),
     'between the cutoff and the later count', ("if between:", "if False:")),
    ('purchase_in_unit', _sql("INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id,"
                              " qty, unit, unit_price, net, synced_to_stock, bsn_code, line_seq)"
                              " VALUES ('2026-09-01', 'HP6999001', 'HP6999001', 304, 1, 'ซอง', 30, 30, 1,"
                              " '017ด5115', 1)"),
     'bought in the unit', ("if n_purch:", "if False:")),
    ('credit_note', _sql("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty,"
                         " unit, unit_price, net, vat_type, synced_to_stock, bsn_code)"
                         " VALUES ('2026-09-01', 'SR6999001-1', 'SR6999001', 577, 1, 'แพ็ค', 39, 39, 1, 1,"
                         " '999ก9015')"),
     'credit note', ("if n_sr:", "if False:")),
    ('ratio_not_one', _sql("UPDATE unit_conversions SET ratio=5.0 WHERE product_id=574 AND bsn_unit='ชุด'"),
     'already', ("if ratios.get(plan['unit']) != 1.0:", "if False:")),
    ('unmapped_unit', _sql("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, qty,"
                           " unit, unit_price, net, vat_type, synced_to_stock, bsn_code)"
                           " VALUES ('2026-09-01', 'IV6999003-1', 'IV6999003', 576, 1, 'กล่อง', 30, 30, 1, 1,"
                           " '999ก9014')"),
     'no unit_conversions row', ("if unmapped:", "if False:")),
]


def _src():
    return _SCRIPT.read_text(encoding='utf-8')


@pytest.mark.parametrize('gid,drift,fragment,mutation', GUARDS, ids=[g[0] for g in GUARDS])
def test_guard_refuses_before_any_write(db, capsys, gid, drift, fragment, mutation):
    drift(db)
    before = _state(db)
    assert _run(db, '--apply') == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and fragment in out, out
    assert _state(db) == before


@pytest.mark.parametrize('gid,drift,fragment,mutation', GUARDS, ids=[g[0] for g in GUARDS])
def test_break_it_once_guard_is_load_bearing(db, capsys, gid, drift, fragment, mutation):
    old, new = mutation
    src = _src()
    assert src.count(old) == 1, "mutation target must exist exactly once: %r" % old
    mutant = src.replace(old, new)
    assert mutant != src and mutant.count(old) == 0, "the mutation did not land"
    drift(db)
    try:
        _run(db, '--apply', mod=_load(mutant))
        err = ''
    except Exception as exc:        # a later step may trip over the drift instead
        err = str(exc)
    assert fragment not in capsys.readouterr().out + err, "guard %s is not load-bearing" % gid


# ── invariants: a wrong fix must roll back ──────────────────────────────────

INVARIANT_MUTATIONS = [
    # without the compensation the absorbed bills are subtracted a second time
    # (578 would land on 231, 926 on -8): exactly what /unit-conversions does
    ('no_compensation', ("if delta:\n", "if False:\n"), 'count pin'),
    ('wrong_ratio', ("(plan['ratio'], pid, plan['unit'])", "(plan['ratio'] + 1, pid, plan['unit'])"),
     'posted'),
    ('compensation_at_today', ("datetime(MIN(created_at), '-1 second')", "datetime('now', '-1 second')"),
     'count pin'),
]


@pytest.mark.parametrize('iid,mutation,fragment', INVARIANT_MUTATIONS,
                         ids=[m[0] for m in INVARIANT_MUTATIONS])
def test_break_it_once_invariants_catch_a_wrong_fix(db, capsys, iid, mutation, fragment):
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


def test_break_it_once_the_oracle_alone_catches_the_ui_path_result(db):
    """With the compensation AND the invariant gate both removed, the script does
    what /unit-conversions does. The typed-in evidence table must still reject it,
    so it is an oracle independent of the script's own invariants."""
    src = _src()
    mutant = src.replace("    if delta:\n", "    if False:\n").replace("    if bad:\n", "    if False:\n")
    assert "    if delta:\n" not in mutant and "    if bad:\n" not in mutant, "the mutation did not land"
    assert _run(db, '--apply', mod=_load(mutant)) in (0, 1)
    got = {pid: _stock(db, pid) for pid in EXPECTED_STOCK}
    assert got != EXPECTED_STOCK
    assert (got[578], got[926], got[623]) == (231, -8, 43), got
