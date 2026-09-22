"""TDD for #600: relabel the bill lines the pre-#599 unit map mis-read.

scripts/derive_600_relabel_history.py builds the list from the BSN5657 stock
card; scripts/2026_09_22_relabel_history_600.py applies it. The fixture carries
the eight #603 hasp sales lines (1187/1188, stored ตัว, Express กร) with their
raw-`กร` purchases, a gross-based sandpaper at ratio 144, one ถง and one บล line,
and bystanders the list must NOT take: a real `ตว` line on the same invoice, a
line already stored as กุรุส, and a line dated after the stock card.
"""
import copy
import datetime
import importlib.util
import json
import pathlib
import sqlite3
import types

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / 'scripts'
_DERIVE = _SCRIPTS / 'derive_600_relabel_history.py'
_APPLY = _SCRIPTS / '2026_09_22_relabel_history_600.py'
ACTOR = 'script:2026_09_22_relabel_history_600'


def _load(path, name, src=None):
    if src is None:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType(name + '_mutant')
    mod.__file__ = str(path)
    exec(compile(src, str(path), 'exec'), mod.__dict__)
    return mod


# (pid, name, unit_type, cost, conversions, stock code)
PRODUCTS = [
    (1187, 'ขอสับ 6 สีโครเมียม (CR)', 'ตัว', 684.0, {'กร': 1.0, 'ตัว': 1.0, 'กุรุส': 1.0}, '900ข5160'),
    (1188, 'ขอสับ 6 สีรมดำ (AC)', 'ตัว', 684.0, {'กร': 1.0, 'ตัว': 1.0, 'กุรุส': 1.0}, '900ข5170'),
    (1050, 'กระดาษทรายขัดไม้ จระเข้ #3', 'แผ่น', 4.26, {'กร': 144.0, 'ตัว': 144.0, 'กุรุส': 144.0},
     '605ก2013'),
    (1303, 'จารบี ถัง', 'ตัว', 900.0, {'ถุง': 1.0, 'ถัง': 1.0}, '600ส7320'),
    (467, 'บล็อก 467', 'แผง', 10.0, {'บล็อก': 1.0}, '026ต2510-1'),
    (9001, 'ผู้ยืนดู', 'ตัว', 5.0, {}, '111ก1111'),
]

# (table, date, doc_no, qty, stored unit, pid, Express seq, Express code, price)
BILLS = [
    # the eight #603 hasp sales (issue #600 comment) + their raw-กร purchases
    ('sales', '2024-05-08', 'IV6701146-6', 1.0, 'ตัว', 1187, 6, 'กร', 960.0),
    ('sales', '2024-09-27', 'IV6702591-3', 4.0, 'ตัว', 1187, 3, 'กร', 960.0),
    ('sales', '2024-10-26', 'IV6702875-4', 1.0, 'ตัว', 1187, 4, 'กร', 960.0),
    ('sales', '2025-07-04', 'IV6801764-11', 1.0, 'ตัว', 1187, 11, 'กร', 960.0),
    ('sales', '2024-01-23', 'IV6700203-5', 1.0, 'ตัว', 1188, 5, 'กร', 960.0),
    ('sales', '2024-10-26', 'IV6702875-3', 1.0, 'ตัว', 1188, 3, 'กร', 960.0),
    ('sales', '2025-08-16', 'IV6802085-1', 1.0, 'ตัว', 1188, 1, 'กร', 960.0),
    ('sales', '2025-11-21', 'IV6802850-5', 1.0, 'ตัว', 1188, 5, 'กร', 960.0),
    ('purchase', '2024-04-01', 'RR6700174', 1.0, 'กร', 1187, 2, 'กร', 684.0),
    ('purchase', '2024-09-01', 'RR6700379', 4.0, 'กร', 1187, 1, 'กร', 684.0),
    ('purchase', '2024-10-01', 'RR6700415', 1.0, 'กร', 1187, 3, 'กร', 684.0),
    ('purchase', '2025-06-01', 'RR6800316', 1.0, 'กร', 1187, 1, 'กร', 684.0),
    ('purchase', '2024-01-10', 'RR6700038', 1.0, 'กร', 1188, 1, 'กร', 684.0),
    ('purchase', '2024-10-01', 'RR6700415', 1.0, 'กร', 1188, 4, 'กร', 684.0),
    ('purchase', '2025-08-01', 'RR6800360', 1.0, 'กร', 1188, 1, 'กร', 684.0),
    ('purchase', '2025-11-01', 'RR6800515', 1.0, 'กร', 1188, 1, 'กร', 684.0),
    # a gross-based product: ตัว and กุรุส both 144
    ('purchase', '2024-02-01', 'RR6700050', 2.0, 'กร', 1050, 1, 'กร', 613.44),
    ('sales', '2024-03-01', 'IV6700300-2', 1.0, 'ตัว', 1050, 2, 'กร', 930.0),
    # ถง / บล
    ('sales', '2024-04-02', 'IV6700400-1', 1.0, 'ถุง', 1303, 1, 'ถง', 1200.0),
    ('sales', '2024-04-03', 'IV6700401-3', 1.0, 'แผง', 467, 3, 'บล', 15.0),
    # bystanders
    ('sales', '2024-05-08', 'IV6701146-2', 3.0, 'ตัว', 9001, 2, 'ตว', 7.0),       # a real ตว
    ('sales', '2026-09-10', 'IV6900200-1', 1.0, 'กุรุส', 1050, 1, 'กร', 930.0),   # already right
    ('sales', '2026-09-15', 'IV6901700-1', 1.0, 'ตัว', 1050, None, None, 930.0),  # after the card
    ('sales', '2026-09-15', 'IV6901701-1', 2.0, 'ตัว', 9001, None, None, 7.0),    # after, not suspect
]

# The rows the list must take: 8 hasp sales + 8 hasp purchases + 1050's two + ถง + บล.
EXPECTED = sorted(
    [('sales_transactions', b[2]) for b in BILLS[:8]]
    + [('purchase_transactions', b[2] + '|' + str(b[5])) for b in BILLS[8:16]]
    + [('purchase_transactions', 'RR6700050|1050'), ('sales_transactions', 'IV6700300-2'),
       ('sales_transactions', 'IV6700400-1'), ('sales_transactions', 'IV6700401-3')])

_CODE = {p[0]: p[5] for p in PRODUCTS}


def _stcrd(bills=BILLS):
    out = []
    for kind, date, doc, qty, _u, pid, seq, code, price in bills:
        if seq is None:
            continue
        out.append({'DOCNUM': doc.split('-')[0], 'SEQNUM': str(seq), 'STKCOD': _CODE[pid],
                    'TQUCOD': code, 'TRNQTY': qty,
                    'TFACTOR': {'กร': 144.0, 'ถง': 25.0, 'บล': 1.0}.get(code, 1.0),
                    'DOCDAT': datetime.date.fromisoformat(date), 'UNITPR': price,
                    'TRNVAL': round(qty * price, 2), 'NETVAL': round(qty * price, 2),
                    'DISC': '', 'STKDES': 'x'})
    return out


def _seed(conn, bills=BILLS):
    from models import bsn_sync, wacc
    for spelling, word in (('กร', 'กุรุส'), ('ถง', 'ถัง'), ('บล', 'บล็อก'), ('ตว', 'ตัว')):
        conn.execute("INSERT OR REPLACE INTO unit_map (book, spelling, word) VALUES ('BSN5657',?,?)",
                     (spelling, word))
    for pid, name, unit, cost, uc, code in PRODUCTS:
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
            " opening_cost, low_stock_threshold, is_active) VALUES (?,?,?,0,0,0,10,1)",
            (pid, name, unit))
        for u, r in uc.items():
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                         (pid, u, r))
        conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)"
                     " VALUES (?,?,?)", (code, name, pid))
    seqs = {}
    for kind, date, doc, qty, unit, pid, _seq, _c, price in bills:
        if kind == 'purchase':
            n = seqs[(doc, pid)] = seqs.get((doc, pid), 0) + 1
            conn.execute(
                "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
                " qty, unit, unit_price, net, total, vat_type, synced_to_stock, line_seq, supplier_code)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,0,?,'S1')",
                (date, doc, doc, pid, _CODE[pid], qty, unit, price, round(qty * price, 2),
                 round(qty * price, 2), n))
        else:
            conn.execute(
                "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
                " qty, unit, unit_price, net, total, vat_type, synced_to_stock, customer_code)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,0,'C1')",
                (date, doc, doc.split('-')[0], pid, _CODE[pid], qty, unit, price,
                 round(qty * price, 2), round(qty * price, 2)))
    for pid, *_ in PRODUCTS:
        bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
        bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
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


def _q(path, sql, *args):
    c = _conn(path)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _exec(path, sql, *args):
    import actor
    c = actor.install(sqlite3.connect(path))
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _derive(path, stcrd=None):
    mod = _load(_DERIVE, 'derive_600')
    c = _conn(path)
    try:
        return mod.derive(c, _stcrd() if stcrd is None else stcrd)
    finally:
        c.close()


def _plan_file(path, tmp_path, plan=None):
    plan = plan if plan is not None else _derive(path)
    out = tmp_path / 'plan.json'
    out.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    return str(out)


def _run(path, plan_path, *extra, mode='rehearse', mod=None):
    args = ['--db', path, '--plan', plan_path, '--mode', mode, '--operator', 'pytest',
            '--reason', 'test run of the #600 relabel', *extra]
    return (mod or _load(_APPLY, 'apply_600')).main(args)


def _units(path):
    return {(t, r[0]): r[1] for t in ('sales_transactions', 'purchase_transactions')
            for r in _q(path, f"SELECT id, unit FROM {t}")}


def _content(path):
    """Every column of both bill tables except the mig-173 provenance four."""
    out = {}
    for t in ('sales_transactions', 'purchase_transactions'):
        c = _conn(path)
        try:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")
                    if not r[1].startswith('change_')]
            for r in c.execute(f"SELECT {', '.join(cols)} FROM {t}"):
                out[(t, r['id'])] = tuple(r)
        finally:
            c.close()
    return out


def _money_and_stock(path):
    return [
        _q(path, "SELECT id, unit_type, printf('%.17g', cost_price), printf('%.17g', opening_cost)"
                 " FROM products ORDER BY id"),
        _q(path, "SELECT product_id, quantity FROM stock_levels ORDER BY 1"),
        _q(path, "SELECT * FROM transactions ORDER BY id"),
        _q(path, "SELECT * FROM product_cost_ledger ORDER BY id"),
        _q(path, "SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY 1, 2"),
        _q(path, "SELECT id, synced_to_stock FROM sales_transactions ORDER BY id"),
        _q(path, "SELECT id, synced_to_stock FROM purchase_transactions ORDER BY id"),
    ]


def _identity(t, doc, pid):
    return (t, doc if t == 'sales_transactions' else '%s|%s' % (doc, pid))


# ── the derivation ──────────────────────────────────────────────────────────

def test_derive_takes_exactly_the_misread_lines(db):
    plan = _derive(db)
    got = sorted(_identity(e['table'], e['doc_no'], e['product_id']) for e in plan['relabel'])
    assert got == EXPECTED
    by = {(e['table'], e['doc_no'], e['bsn_code']): e for e in plan['relabel']}
    assert by[('sales_transactions', 'IV6701146-6', '900ข5160')]['new'] == 'กุรุส'
    assert by[('sales_transactions', 'IV6700400-1', '600ส7320')]['new'] == 'ถัง'
    assert by[('sales_transactions', 'IV6700401-3', '026ต2510-1')]['new'] == 'บล็อก'
    e = by[('sales_transactions', 'IV6701146-6', '900ข5160')]
    assert (e['stored'], e['express_doc'], e['express_seq'], e['express_code']) == \
        ('ตัว', 'IV6701146', 6, 'กร')


def test_derive_leaves_bystanders_and_lists_what_it_cannot_decide(db):
    plan = _derive(db)
    docs = {e['doc_no'] for e in plan['relabel']}
    assert 'IV6701146-2' not in docs, 'a real ตว line on the same invoice'
    assert 'IV6900200-1' not in docs and plan['counts']['already_new_word'] == {'กร': 1}
    assert [(e['doc_no'], e['stored']) for e in plan['unrecoverable']] == [('IV6901700-1', 'ตัว')]
    assert 'not on the stock card' in plan['unrecoverable'][0]['reason']
    # the post-card line on a stock code never billed in กร is counted, not listed
    assert sum(plan['counts']['unmatched_not_suspect'].values()) == 1


def test_derive_takes_the_word_from_the_map_not_from_itself(db):
    _exec(db, "UPDATE unit_map SET word='ถังใหญ่' WHERE book='BSN5657' AND spelling='ถง'")
    plan = _derive(db)
    assert [e['new'] for e in plan['relabel'] if e['express_code'] == 'ถง'] == ['ถังใหญ่']


def test_derive_refuses_before_599(db):
    mod = _load(_DERIVE, 'derive_600')
    _exec(db, "UPDATE unit_map SET word='ตัว' WHERE book='BSN5657' AND spelling='กร'")
    c = _conn(db)
    try:
        with pytest.raises(mod.DeriveRefused, match='migration 190'):
            mod.derive(c, _stcrd())
    finally:
        c.close()


def test_derive_never_guesses(db):
    """qty edited at source, a line number that now holds another stock code, two
    stock-card lines disagreeing, two Sendy rows on one identity, a stored word the
    old map never wrote for that code: each is listed, none is taken."""
    stcrd = _stcrd()
    by = {(r['DOCNUM'], r['SEQNUM']): r for r in stcrd}
    by[('IV6702591', '3')]['TRNQTY'] = 5.0                       # qty differs
    by[('IV6702875', '4')]['STKCOD'] = '999ก9999'                # line 4 is another product
    stcrd.append(dict(by[('IV6700203', '5')], TQUCOD='ตว'))      # line 5 twice, two codes
    by[('IV6700401', '3')]['TQUCOD'] = 'กร'                      # stored แผง, Express กร
    _exec(db, "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
              " qty, unit, unit_price, net, vat_type, synced_to_stock)"
              " VALUES ('2024-04-02','IV6700400-1','IV6700400',1303,'600ส7320',1,'ถุง',1,1,1,1)")
    plan = _derive(db, stcrd)
    reasons = {e['doc_no']: e['reason'] for e in plan['unrecoverable']}
    taken = {e['doc_no'] for e in plan['relabel']}
    for doc, why in (('IV6702591-3', 'qty'), ('IV6702875-4', 'no stock-card line'),
                     ('IV6700203-5', 'stock-card lines answer'), ('IV6700400-1', 'rows share'),
                     ('IV6700401-3', "stored 'แผง'")):
        assert doc not in taken, doc
        assert why in reasons.get(doc, ''), (doc, reasons.get(doc))
    assert 'IV6701146-6' in taken, 'control: an untouched line is still taken'


# ── the apply script ────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    before = (_content(db), _money_and_stock(db), _q(db, "SELECT COUNT(*) FROM audit_log"))
    assert _run(db, plan, mode='dry-run') == 0
    out = capsys.readouterr().out
    assert 'DRY RUN' in out and '20 row(s)' in out, out
    assert (_content(db), _money_and_stock(db), _q(db, "SELECT COUNT(*) FROM audit_log")) == before


def test_rehearse_relabels_exactly_the_list_and_nothing_else(db, tmp_path):
    plan_path = _plan_file(db, tmp_path)
    plan = json.loads(open(plan_path, encoding='utf-8').read())
    before = _content(db)
    assert _run(db, plan_path) == 0
    after = _content(db)
    changed = {k for k in before if before[k] != after[k]}
    assert len(changed) == 20
    ids = {(e['table'], e['row_id']): e for e in plan['relabel']}
    assert changed == set(ids)
    units = _units(db)
    for k, e in ids.items():
        assert units[k] == e['new'], (k, units[k])
        # only `unit` moved on the row
        c = _conn(db)
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({k[0]})") if not r[1].startswith('change_')]
        c.close()
        diff = [col for col, a, b in zip(cols, before[k], after[k]) if a != b]
        assert diff == ['unit'], (k, diff)


def test_stock_cost_ledger_and_sync_do_not_move(db, tmp_path):
    before = _money_and_stock(db)
    assert before[1], 'control: the fixture has stock rows'
    assert _run(db, _plan_file(db, tmp_path)) == 0
    assert _money_and_stock(db) == before


def test_each_rewrite_is_declared_with_its_own_token(db, tmp_path):
    assert _run(db, _plan_file(db, tmp_path)) == 0
    rows = _q(db, "SELECT doc_no, change_source, change_actor, change_reason, change_token"
                  " FROM sales_transactions WHERE change_actor=?"
                  " UNION ALL SELECT doc_no, change_source, change_actor, change_reason, change_token"
                  " FROM purchase_transactions WHERE change_actor=?", ACTOR, ACTOR)
    assert len(rows) == 20
    assert {r[1] for r in rows} == {'manual'}
    assert len({r[4] for r in rows}) == 20, 'one fresh token per rewrite'
    for doc, _s, _a, reason, _t in rows:
        assert doc.split('-')[0] in reason and 'pytest' in reason, reason
    audit = _q(db, "SELECT changed_fields FROM audit_log WHERE action='UPDATE' AND user=?", ACTOR)
    assert len(audit) == 20
    assert {tuple(json.loads(a[0])) for a in audit} == {('unit',)}


def test_a_second_run_is_refused(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    assert _run(db, plan) == 0
    state = (_content(db), _q(db, "SELECT COUNT(*) FROM audit_log"))
    capsys.readouterr()
    assert _run(db, plan) == 2
    assert 'already' in capsys.readouterr().out
    assert (_content(db), _q(db, "SELECT COUNT(*) FROM audit_log")) == state


def test_undo_restores_exactly_and_a_forward_run_may_follow(db, tmp_path):
    plan = _plan_file(db, tmp_path)
    before, money = _content(db), _money_and_stock(db)
    assert _run(db, plan) == 0
    assert _content(db) != before
    assert _run(db, plan, '--undo') == 0
    assert _content(db) == before
    assert _money_and_stock(db) == money
    assert len(_q(db, "SELECT id FROM audit_log WHERE user=?", ACTOR + ':undo')) == 20
    assert _run(db, plan) == 0, 'after an undo the relabel may run again'


def test_undo_with_nothing_to_undo_is_refused(db, tmp_path, capsys):
    before = _content(db)
    assert _run(db, _plan_file(db, tmp_path), '--undo') == 2
    assert 'nothing' in capsys.readouterr().out
    assert _content(db) == before


def test_undo_refuses_when_a_relabelled_row_was_changed_since(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    assert _run(db, plan) == 0
    # an import stamps the row before replacing it (models/imports.py)
    _exec(db, "UPDATE sales_transactions SET change_source='import', change_actor='express_dbf:x',"
              " change_reason=NULL WHERE doc_no='IV6702591-3'")
    state = _content(db)
    capsys.readouterr()
    assert _run(db, plan, '--undo') == 2
    assert 'IV6702591-3' in capsys.readouterr().out
    assert _content(db) == state


def test_refuses_while_the_map_still_reads_the_old_word(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    _exec(db, "UPDATE unit_map SET word='ตัว' WHERE book='BSN5657' AND spelling='กร'")
    before = _content(db)
    assert _run(db, plan) == 2
    assert 'unit map' in capsys.readouterr().out
    assert _content(db) == before


def test_refuses_a_relabel_that_would_move_stock(db, tmp_path, capsys):
    """1050's กุรุส at 1.0 against ตัว at 144: relabelling would post 1 where the
    ledger holds 144. Nothing is written, and the product is named."""
    plan = _plan_file(db, tmp_path)
    _exec(db, "UPDATE unit_conversions SET ratio=1.0 WHERE product_id=1050 AND bsn_unit='กุรุส'")
    before = _content(db)
    assert _run(db, plan) == 2
    out = capsys.readouterr().out
    assert '1050' in out and 'ratio' in out, out
    assert _content(db) == before


@pytest.mark.parametrize('where', ['plan', 'db'])
def test_each_of_the_eight_hasp_lines_is_required(db, tmp_path, capsys, where):
    plan = _derive(db)
    if where == 'plan':
        plan['relabel'] = [e for e in plan['relabel'] if e['doc_no'] != 'IV6802850-5']
    else:
        _exec(db, "UPDATE sales_transactions SET change_source='manual', change_actor='t',"
                  " change_reason='moved in a test fixture', change_token='x1',"
                  " doc_no='IV6802850-9' WHERE doc_no='IV6802850-5'")
    before = _content(db)
    assert _run(db, _plan_file(db, tmp_path, plan)) == 2
    out = capsys.readouterr().out
    assert 'IV6802850-5' in out and '#603' in out, out
    assert _content(db) == before


def test_a_row_changed_since_the_derivation_is_refused(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    _exec(db, "UPDATE sales_transactions SET qty=2, change_source='manual', change_actor='t',"
              " change_reason='edited in a test fixture', change_token='x2'"
              " WHERE doc_no='IV6700300-2'")
    before = _content(db)
    assert _run(db, plan) == 2
    assert 'IV6700300-2' in capsys.readouterr().out
    assert _content(db) == before


def test_a_row_the_importer_already_rewrote_is_skipped_not_refused(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    _exec(db, "UPDATE sales_transactions SET unit='กุรุส', change_source='import',"
              " change_actor='express_dbf:x', change_token='x3' WHERE doc_no='IV6802085-1'")
    assert _run(db, plan) == 0
    assert '19 row(s)' in capsys.readouterr().out
    assert _q(db, "SELECT change_actor FROM sales_transactions WHERE doc_no='IV6802085-1'") == \
        [('express_dbf:x',)]


def test_rehearse_refuses_the_app_db_and_live_needs_confirmation(db, tmp_path, capsys):
    plan = _plan_file(db, tmp_path)
    app_db = tmp_path / 'inventory.db'
    sqlite3.connect(db).backup(sqlite3.connect(str(app_db)))
    assert _run(str(app_db), plan) == 2
    assert _run(db, plan, mode='live') == 2
    assert 'confirm' in capsys.readouterr().out
    assert _units(db) == _units(str(app_db))


# ── the importer reads the relabelled rows as unchanged ─────────────────────

def _reimport(path, monkeypatch, file_type):
    import config
    import database
    import express_dbf_source as eds
    from models import imports
    monkeypatch.setattr(config, 'DATABASE_PATH', path)
    monkeypatch.setattr(database, 'DATABASE_PATH', path)
    stcrd = _stcrd()
    heads = [{'DOCNUM': r['DOCNUM'], 'RECTYP': '3', 'DOCDAT': r['DOCDAT'], 'FLGVAT': 1,
              'CUSCOD': 'C1', 'SUPCOD': 'S1'} for r in stcrd]
    if file_type == 'sales':
        entries = eds.build_sales_entries(heads, stcrd, [{'CUSCOD': 'C1', 'CUSNAM': 'c'}])
        entries = [e for e in entries if e['doc_no'].startswith('IV')]
    else:
        entries = eds.build_purchase_entries(heads, stcrd, [{'SUPCOD': 'S1', 'SUPNAM': 's'}])
        entries = [e for e in entries if e['doc_no'].startswith('RR')]
        # the history CSV these lines came through numbers line_seq per
        # (document, stock code), not by Express SEQNUM (parse_weekly.py)
        seen = {}
        for e in entries:
            key = (e['doc_no'], e['product_code_raw'])
            e['line_seq'] = seen[key] = seen.get(key, 0) + 1
    return imports.import_weekly(entries, file_type, 'test-600-reimport')


@pytest.mark.parametrize('file_type', ['sales', 'purchase'])
def test_reimporting_the_express_lines_after_the_relabel_is_a_no_op(db, tmp_path, monkeypatch,
                                                                    file_type):
    assert _run(db, _plan_file(db, tmp_path)) == 0
    before = _money_and_stock(db)
    stats = _reimport(db, monkeypatch, file_type)
    assert stats['overwritten'] == 0 and stats['imported'] == 0, stats
    assert stats['unchanged'] > 0
    assert _money_and_stock(db)[:4] == before[:4]


def test_control_without_the_relabel_the_importer_rewrites_them(db, monkeypatch):
    """The no-op above is only evidence if the same re-import does see the
    unrelabelled lines as changed."""
    stats = _reimport(db, monkeypatch, 'sales')
    assert stats['overwritten'] == 11, stats
