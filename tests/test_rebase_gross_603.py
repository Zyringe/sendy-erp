"""TDD for scripts/2026_09_20_rebase_gross_603.py — #603, six "กร = 1.0" products.

Written BEFORE the script (erp-engineering-discipline: anything mutating
`transactions` / `stock_levels` starts from a failing test).

The fixture reproduces the six products EXACTLY as prod holds them (read
2026-09-19 17:07Z, identical to prod-2026-09-19T1536Z-pre-592.db): every bill,
every price, the opening plugs, 1052's orphan-cleanup ADJUST, the tiers and the
price-history rows the resolver's epoch reads. It is typed here from that dump
and never built from the script's PLAN, so a wrong PLAN cannot confirm itself.
The ledger is posted by the app's own sync, never typed in.

Plus one control the script must not touch: 1186 (ขอตัว C 11in), which Put
ruled is not a gross.

Three orders are exercised, because #599 (the unit map's กร -> กุรุส) and #600
(relabelling old rows) may land before or after this:
  * 603-first  : the map still says กร -> ตัว, no กุรุส conversion exists.
  * 599-first  : the map says กร -> กุรุส and #599 added กุรุส = 1.0.
  * 600-first  : as 599-first, and the old rows already read กุรุส.
The hasps (1187/1188 -> ตัว) may only run in the last two.
"""
import importlib.util
import os
import pathlib
import sqlite3
import types

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_20_rebase_gross_603.py"

SOURCE = 'script:2026_09_20_rebase_gross_603'
OPENING_NOTE = 'ยอดยกมา (back-solved)'
RECONCILE_NOTE = 'ปรับยอดคงเหลือ (แปลงหน่วย 2026-09-19)'
ORPHAN_NOTE = 'ล้าง orphan ledger 2026-07-03'
SANDPAPERS = (1047, 1048, 1049, 1052)
HASPS = (1187, 1188)
SANDPAPER_ARG = '1047,1048,1049,1052'
HASP_ARG = '1187,1188'


def _load(src=None):
    """The script as a module. `src` replaces its text (break-it-once)."""
    if src is None:
        spec = importlib.util.spec_from_file_location("rebase_gross_603", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType("rebase_gross_603_mutant")
    mod.__file__ = str(_SCRIPT)          # engine discovery keys on it
    exec(compile(src, str(_SCRIPT), "exec"), mod.__dict__)
    return mod


def _src():
    return _SCRIPT.read_text(encoding='utf-8')


# ── fixture: prod's rows, as read 2026-09-19 ────────────────────────────────

# (pid, name, cost, base, tier price, bsn_code, extra units)
PRODUCTS = [
    (1047, 'กระดาษทรายขัดไม้ จระเข้ #0', 614.02, 800.0, 800.0, '605ก2010', {}),
    (1048, 'กระดาษทรายขัดไม้ จระเข้ #1', 614.02, 800.0, 800.0, '605ก2011', {}),
    (1049, 'กระดาษทรายขัดไม้ จระเข้ #2', 664.48, 880.0, 880.0, '605ก2012', {}),
    (1052, 'กระดาษทรายขัดไม้ จระเข้ #4', 788.0142307692307, 980.0, 980.0, '605ก2014', {'โหล': 12.0}),
    (1187, 'ขอสับ 6 สีโครเมียม (CR)', 684.0, 960.0, 960.0, '900ข5160', {}),
    (1188, 'ขอสับ 6 สีรมดำ (AC)', 684.0, 960.0, 960.0, '900ข5170', {}),
]

# (pid, date, doc, qty, unit_price, net, vat_type, customer, customer_code)
SALES = [
    (1047, '2025-09-13', 'IV6802317-1', 1.0, 800.0, 800.0, 1, 'วิชัยเคหะภัณฑ์', '14ว001'),
    (1047, '2024-01-26', 'IV6700242-4', 1.0, 800.0, 800.0, 1, 'ฟูแสงวัสดุ', '33ฟ001'),
    (1047, '2024-08-19', 'IV6702197-3', 1.0, 800.0, 800.0, 1, 'ฟ้าเหลียงพาณิชย์(Vศุภกร)', '34ฟ01'),
    (1047, '2024-05-21', 'IV6701274-6', 1.0, 800.0, 800.0, 2, 'เอ็มเอสยู มายโฮม(แม่สรวย)', '34ม06'),
    (1047, '2024-07-13', 'IV6701822-7', 1.0, 800.0, 784.0, 1, 'เจริญทรัพย์การค้า', '38จ01'),
    (1047, '2025-02-14', 'IV6800465-1', 1.0, 800.0, 800.0, 1, 'จิรวัฒน์พบพระ การค้า', '39จ04'),
    (1047, '2024-11-26', 'IV6703189-5', 1.0, 800.0, 800.0, 1, 'ทรัพย์ทวี', '43ท013'),
    (1047, '2025-12-13', 'IV6802995-5', 1.0, 800.0, 800.0, 1, 'สมุย เอ็นจิเนียริ่ง', '68ส005'),
    (1048, '2025-09-13', 'IV6802317-2', 1.0, 800.0, 800.0, 1, 'วิชัยเคหะภัณฑ์', '14ว001'),
    (1048, '2025-06-07', 'IV6801500-8', 2.0, 747.68, 1495.36, 2, 'โชคชัยสุขภัณฑ์(V)', '23ช05'),
    (1049, '2025-12-13', 'IV6802995-6', 1.0, 880.0, 880.0, 1, 'สมุย เอ็นจิเนียริ่ง', '68ส005'),
    (1052, '2025-06-07', 'IV6801500-10', 2.0, 915.9, 1831.8, 2, 'โชคชัยสุขภัณฑ์(V)', '23ช05'),
    (1052, '2024-01-26', 'IV6700242-5', 1.0, 980.0, 980.0, 1, 'ฟูแสงวัสดุ', '33ฟ001'),
    (1052, '2025-01-07', 'IV6800068-10', 1.0, 980.0, 980.0, 1, 'ฟ้าเหลียงพาณิชย์(Vศุภกร)', '34ฟ01'),
    (1052, '2024-05-21', 'IV6701274-8', 1.0, 980.0, 980.0, 2, 'เอ็มเอสยู มายโฮม(แม่สรวย)', '34ม06'),
    (1052, '2024-11-26', 'IV6703189-6', 1.0, 980.0, 980.0, 1, 'ทรัพย์ทวี', '43ท013'),
    (1052, '2024-10-28', 'IV6702896-3', 1.0, 980.0, 980.0, 1, 'วรากรวัสดุV(S.W.J.J)', '43ว02'),
    (1052, '2025-01-13', 'IV6800126-1', 1.0, 980.0, 980.0, 1, 'สามารถวิศวะ 1990', '58ส002'),
    (1187, '2024-05-08', 'IV6701146-6', 1.0, 690.0, 690.0, 1, 'เจริญพรพานิช', '19จ001'),
    (1187, '2024-09-27', 'IV6702591-3', 4.0, 864.0, 3456.0, 2, 'ศรีจอมทอง ทริปเพล็ทส์(ไม้ล้านนา)', '27ม04'),
    (1187, '2024-10-26', 'IV6702875-4', 1.0, 960.0, 912.0, 2, 'รุ่งเรืองพัฒนา', '29ร001'),
    (1187, '2025-07-04', 'IV6801764-11', 1.0, 1060.0, 1060.0, 1, 'น.แหม๋ง ขายเครื่องก่อสร้าง', 'L1301ห02'),
    (1188, '2025-08-16', 'IV6802085-1', 1.0, 960.0, 960.0, 2, 'ปิยะภัณฑ์ฮอด', '27ป007'),
    (1188, '2024-10-26', 'IV6702875-3', 1.0, 960.0, 912.0, 2, 'รุ่งเรืองพัฒนา', '29ร001'),
    (1188, '2024-01-23', 'IV6700203-5', 1.0, 960.0, 960.0, 1, 'รวมทรัพย์วัสดุ', '34ร10'),
    (1188, '2025-11-21', 'IV6802850-5', 1.0, 960.0, 960.0, 1, 'รวมทรัพย์วัสดุ', '34ร10'),
]

# (pid, date, doc, qty, unit_price, net, vat_type)
PURCHASES = [
    (1047, '2024-01-24', 'RR6700041', 1.0, 730.0, 657.0, 1),
    (1047, '2024-05-20', 'RR6700200', 1.0, 730.0, 657.0, 2),
    (1047, '2024-07-12', 'RR6700289', 1.0, 730.0, 657.0, 1),
    (1047, '2024-08-16', 'RR6700333', 1.0, 730.0, 657.0, 1),
    (1047, '2024-11-25', 'RR6700481', 1.0, 730.0, 657.0, 1),
    (1047, '2025-02-13', 'RR6800085', 1.0, 730.0, 614.02, 1),
    (1047, '2025-09-12', 'RR6800407', 1.0, 730.0, 614.02, 1),
    (1047, '2025-12-12', 'RR6800561', 1.0, 730.0, 614.02, 1),
    (1048, '2025-06-06', 'RR6800265', 2.0, 730.0, 1228.04, 1),
    (1048, '2025-09-12', 'RR6800407', 1.0, 730.0, 614.02, 1),
    (1049, '2025-12-12', 'RR6800561', 1.0, 790.0, 664.48, 1),
    (1052, '2024-01-24', 'RR6700041', 1.0, 880.0, 792.0, 1),
    (1052, '2024-05-20', 'RR6700200', 1.0, 880.0, 792.0, 2),
    (1052, '2024-10-26', 'RR6700424', 1.0, 880.0, 792.0, 1),
    (1052, '2024-11-25', 'RR6700481', 1.0, 880.0, 792.0, 1),
    (1052, '2025-01-06', 'RR6800014', 2.0, 880.0, 1584.0, 0),
    (1052, '2025-06-06', 'RR6800265', 2.0, 880.0, 1480.37, 1),
    (1187, '2024-05-09', 'RR6700174', 1.0, 960.0, 684.0, 1),
    (1187, '2024-09-26', 'RR6700379', 4.0, 960.0, 2736.0, 1),
    (1187, '2024-10-25', 'RR6700415', 1.0, 960.0, 684.0, 1),
    (1187, '2025-07-03', 'RR6800316', 1.0, 960.0, 684.0, 0),
    (1188, '2024-01-22', 'RR6700038', 1.0, 960.0, 684.0, 1),
    (1188, '2024-10-25', 'RR6700415', 1.0, 960.0, 684.0, 1),
    (1188, '2025-08-15', 'RR6800360', 1.0, 960.0, 684.0, 0),
    (1188, '2025-11-20', 'RR6800515', 1.0, 960.0, 684.0, 0),
]

# (pid, opening plug) — 1052's plug is balanced by the 2026-07-03 orphan cleanup
OPENINGS = {1047: 0, 1048: 0, 1049: 0, 1052: 24, 1187: 0, 1188: 0}


def _seed(conn, order='603-first'):
    """Prod's pre-state. `order` simulates #599 / #600 having landed first."""
    from models import bsn_sync, wacc
    gross = 'กุรุส'
    conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'กร', ?)",
                 ('ตัว' if order == '603-first' else gross,))
    conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'ตว', 'ตัว')")
    codes = {}
    for pid, name, cost, base, tier, code, extra in PRODUCTS + [
            (1186, 'ขอตัว C 11in', 92.62, 0.0, None, '900ข5150', {})]:
        codes[pid] = code
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, cost_price, base_sell_price,"
            " opening_cost, low_stock_threshold, is_active) VALUES (?,?,'ตัว',?,?,0.0,10,1)",
            (pid, name, cost, base))
        conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)"
                     " VALUES (?, ?, ?, '')", (code, name, pid))
        units = {'กร': 1.0, 'ตัว': 1.0, **extra}
        if order != '603-first':
            units[gross] = 1.0            # #599 copies the กร row's ratio
        for u, r in units.items():
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                         (pid, u, r))
        if tier is not None:
            conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price)"
                         " VALUES (?, '1 กุรุส', ?)", (pid, tier))
            conn.execute("INSERT INTO product_price_history (product_id, field_name, old_value,"
                         " new_value, changed_at) VALUES (?, 'base_sell_price', 0.0, ?,"
                         " '2026-08-28 04:29:04')", (pid, base))
    sale_unit = gross if order == '600-first' else 'ตัว'
    buy_unit = gross if order == '600-first' else 'กร'
    for pid, date, doc, qty, price, net, vat, cust, ccode in SALES + [
            (1186, '2024-10-24', 'IV6702851-1', 1.0, 130.0, 123.5, 1, 'สายัณห์ก่อสร้าง(2018)', '53ส01')]:
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?) ON CONFLICT(code) DO NOTHING",
                     (ccode, cust))
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
            " product_name_raw, customer, customer_code, qty, unit, unit_price, vat_type,"
            " discount, total, net, synced_to_stock) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,0)",
            (date, doc, doc.split('-')[0], pid, codes[pid], 'raw', cust, ccode, qty,
             sale_unit if pid != 1186 else 'ตัว', price, vat, net, net))
    for pid, date, doc, qty, price, net, vat in PURCHASES + [
            (1186, '2024-10-23', 'RR6700411', 1.0, 92.62, 92.62, 1)]:
        conn.execute(
            "INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, product_id, bsn_code,"
            " product_name_raw, supplier, qty, unit, unit_price, vat_type, total, net,"
            " line_seq, synced_to_stock) VALUES (?,?,?,?,?,'raw','ศรีไทยเจริญโลหะกิจ',?,?,?,?,?,?,1,0)",
            (date, doc, doc, pid, codes[pid], qty, buy_unit if pid != 1186 else 'กร',
             price, vat, net, net))
    ids = tuple(OPENINGS) + (1186,)
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=ids)
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=ids)
    for pid in ids:
        conn.execute(
            "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note,"
            " created_at) VALUES (?, 'ADJUST', ?, 'unit', ?, '2024-01-03 00:00:00')",
            (pid, OPENINGS.get(pid, 0), OPENING_NOTE))
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note,"
        " created_at) VALUES (1052, 'ADJUST', -24, 'unit', ?, '2026-07-03 00:00:00')", (ORPHAN_NOTE,))
    for pid in ids:
        wacc.recalculate_product_wacc(pid, conn)
    conn.commit()


def _make(empty_db, empty_db_conn, order):
    _seed(empty_db_conn, order)
    return str(empty_db)


@pytest.fixture()
def db(empty_db, empty_db_conn):
    return _make(empty_db, empty_db_conn, '603-first')


@pytest.fixture()
def db599(empty_db, empty_db_conn):
    return _make(empty_db, empty_db_conn, '599-first')


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _one(path, sql, *args):
    c = _conn(path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def _all(path, sql, *args):
    c = _conn(path)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _run(path, pids, *extra, mod=None, mode='rehearse'):
    return (mod or _load()).main(['--db', path, '--pids', pids, '--mode', mode,
                                  '--operator', 'test-operator', *extra])


def _state(path):
    """Everything the script may touch, as one comparable value."""
    q = lambda sql: _all(path, sql)
    return [
        q("SELECT id, unit_type, cost_price, opening_cost, base_sell_price, low_stock_threshold"
          " FROM products ORDER BY id"),
        q("SELECT product_id, quantity FROM stock_levels ORDER BY 1"),
        q("SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY 1, 2"),
        q("SELECT id, product_id, qty_label, price FROM product_price_tiers ORDER BY 1"),
        q("SELECT id, product_id, txn_type, quantity_change, reference_no, note, created_at"
          " FROM transactions ORDER BY id"),
        q("SELECT id, product_id, qty_change, unit_cost, stock_after, wacc_after, note"
          " FROM product_cost_ledger ORDER BY id"),
        q("SELECT id, product_id, unit, qty, synced_to_stock FROM sales_transactions ORDER BY id"),
        q("SELECT id, product_id, unit, qty, synced_to_stock FROM purchase_transactions ORDER BY id"),
        q("SELECT product_id, field_name, old_value, new_value, source FROM product_price_history"
          " ORDER BY id"),
    ]


def _legs(path, pid):
    return dict(_all(path, "SELECT reference_no, quantity_change FROM transactions"
                           " WHERE product_id=? AND note LIKE 'BSN%'", pid))


def _cogs(path, pid):
    """The app's own COGS expression (/accounting), over the product's whole history."""
    import sales_filters
    return _one(path, "SELECT COALESCE(SUM({q} * p.cost_price), 0) FROM sales_transactions st"
                      " JOIN products p ON p.id = st.product_id {j} WHERE st.product_id = ?"
                .format(q=sales_filters.base_qty_sql(), j=sales_filters.unit_conversion_join()),
                pid)[0]


# ── the fixture reproduces prod's pre-state ─────────────────────────────────

def test_fixture_reproduces_prod(db):
    """CONTROL for everything below."""
    for pid, *_ in PRODUCTS:
        assert _one(db, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)[0] == 0
    assert _legs(db, 1187)['IV6702591-3'] == -4, "4 ตัว (a gross each) posts -4 in the old base"
    assert _legs(db, 1052)['RR6800014'] == 2
    cost = dict(_all(db, "SELECT id, cost_price FROM products"))
    for pid, _n, want, *_ in PRODUCTS:
        assert cost[pid] == pytest.approx(want, abs=1e-9), (pid, cost[pid], want)
    assert _one(db, "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id IN"
                    " (1047,1048,1049,1052,1187,1188)")[0] == 25


# ── sandpapers: runnable today ──────────────────────────────────────────────

TODAY = '2026-09-20'     # pinned: the bills' 24-month window depends on it
NEW_BASE = {1047: 5.56, 1048: 5.56, 1049: 6.12, 1052: 6.81, 1187: 6.67, 1188: 6.67}


def _assert_rebased_sandpapers(path):
    for pid, _n, cost, *_ in PRODUCTS[:4]:
        p = _one(path, "SELECT unit_type, cost_price, opening_cost, base_sell_price,"
                       " low_stock_threshold FROM products WHERE id=?", pid)
        assert p[0] == 'แผ่น'
        assert p[1] == pytest.approx(cost / 144, abs=1e-12), "cost divides EXACTLY"
        assert p[2] == pytest.approx(cost / 144, abs=1e-12), "opening_cost 0 falls back to cost"
        assert p[3] == NEW_BASE[pid], "base/144 rounded UP"
        assert p[4] == 10, "Put 08-17: the threshold is kept"
        assert _one(path, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)[0] == 0
        assert _one(path, "SELECT SUM(quantity_change) FROM transactions WHERE product_id=?", pid)[0] == 0
        assert _all(path, "SELECT qty_label, price FROM product_price_tiers WHERE product_id=?", pid) \
            == [('1 กุรุส', {1047: 800.0, 1048: 800.0, 1049: 880.0, 1052: 980.0}[pid])]
    ratios = lambda pid: dict(_all(path, "SELECT bsn_unit, ratio FROM unit_conversions"
                                         " WHERE product_id=?", pid))
    for pid in (1047, 1048, 1049):
        assert ratios(pid) == {'กร': 144.0, 'ตัว': 144.0, 'กุรุส': 144.0, 'แผ่น': 1.0}
    assert ratios(1052) == {'กร': 144.0, 'ตัว': 144.0, 'กุรุส': 144.0, 'แผ่น': 1.0, 'โหล': 12.0}, \
        "1 โหล = 12 แผ่น — not the engine's blanket 144"


def test_sandpapers_rebase_to_sheets(db):
    assert _run(db, SANDPAPER_ARG) == 0
    _assert_rebased_sandpapers(db)


def test_every_sandpaper_bill_posts_a_gross(db):
    assert _run(db, SANDPAPER_ARG) == 0
    legs = _legs(db, 1047)
    assert len(legs) == 16, legs
    assert set(v for k, v in legs.items() if k.startswith('IV')) == {-144}
    assert set(v for k, v in legs.items() if k.startswith('RR')) == {144}
    assert _legs(db, 1048)['IV6801500-8'] == -288 and _legs(db, 1048)['RR6800265'] == 288


def test_1052_orphan_cleanup_is_rescaled_and_opening_recomputed(db):
    """The -24 ADJUST was written in the old base (24 gross). Left alone it would
    silently become 24 sheets; the opening would follow it to +24."""
    assert _run(db, SANDPAPER_ARG) == 0
    assert _all(db, "SELECT quantity_change FROM transactions WHERE product_id=1052 AND note=?",
                ORPHAN_NOTE) == [(-3456.0,)]
    rows = _all(db, "SELECT quantity_change, created_at FROM transactions"
                    " WHERE product_id=1052 AND note IN (?,?)", OPENING_NOTE, RECONCILE_NOTE)
    assert len(rows) == 1 and rows[0][0] == 3456, rows
    head = _one(db, "SELECT MIN(created_at) FROM transactions WHERE product_id=1052 AND note<>?",
                OPENING_NOTE)[0]
    assert rows[0][1] < head, "strictly before the head: WACC sorts IN first on a tie"


def test_zero_openings_vanish(db):
    assert _run(db, SANDPAPER_ARG) == 0
    for pid in (1047, 1048, 1049):
        assert _one(db, "SELECT COUNT(*) FROM transactions WHERE product_id=? AND note IN (?,?)",
                    pid, OPENING_NOTE, RECONCILE_NOTE)[0] == 0


def test_cogs_and_cost_ledger_value_unchanged(db):
    before = {pid: _cogs(db, pid) for pid in SANDPAPERS}
    value = lambda pid: _one(db, "SELECT SUM(qty_change * unit_cost) FROM product_cost_ledger"
                                 " WHERE product_id=?", pid)[0]
    vbefore = {pid: value(pid) for pid in SANDPAPERS}
    assert all(before.values()), before
    assert _run(db, SANDPAPER_ARG) == 0
    for pid in SANDPAPERS:
        assert _cogs(db, pid) == pytest.approx(before[pid], abs=0.005), pid
        assert value(pid) == pytest.approx(vbefore[pid], abs=0.005), pid
    notes = _all(db, "SELECT note FROM product_cost_ledger WHERE product_id IN (1047,1048,1049,1052)")
    assert len(notes) == 17 and all(n[0].endswith('(%s)' % SOURCE) for n in notes), notes


def test_price_history_is_attributed_to_this_script(db):
    assert _run(db, SANDPAPER_ARG) == 0
    rows = _all(db, "SELECT product_id, old_value, new_value, source FROM product_price_history"
                    " WHERE field_name='base_sell_price' AND old_value <> 0 ORDER BY product_id")
    assert rows == [(1047, 800.0, 5.56, SOURCE), (1048, 800.0, 5.56, SOURCE),
                    (1049, 880.0, 6.12, SOURCE), (1052, 980.0, 6.81, SOURCE)]


def test_script_source_is_registered_with_the_resolver():
    """#613: a rebase SOURCE missing from this tuple resets every repeat customer to list."""
    import price_lookup
    assert SOURCE in price_lookup._UNIT_REBASE_SOURCES
    assert _load().SOURCE == SOURCE


def _answers(path, pid):
    """{customer_code: (basis, price)} asking in the unit of that customer's own latest bill."""
    import price_lookup
    c = _conn(path)
    try:
        out = {}
        for code, unit in c.execute(
                "SELECT customer_code, unit FROM sales_transactions WHERE product_id=?"
                " ORDER BY date_iso, id", (pid,)):
            out[code] = unit                      # the latest bill wins
        res = {}
        for code, unit in out.items():
            a = price_lookup.resolve_price(c, product_id=pid, customer_code=code, unit=unit,
                                           today=TODAY)['answer']
            res[code] = (a['basis'], a['price_per_unit'])
        return res
    finally:
        c.close()


def test_repeat_customer_keeps_last_paid_price(db):
    """The #613 failure, end to end: a repeat customer is quoted what they last paid."""
    before = {pid: _answers(db, pid) for pid in SANDPAPERS}
    last_paid = [(pid, c) for pid in SANDPAPERS for c, (b, _p) in before[pid].items() if b == 'last_paid']
    assert len(last_paid) >= 3, "control: the fixture must contain repeat customers in window"
    assert _run(db, SANDPAPER_ARG) == 0
    after = {pid: _answers(db, pid) for pid in SANDPAPERS}
    for pid, c in last_paid:
        assert after[pid][c] == before[pid][c], (pid, c, before[pid][c], after[pid][c])


def test_break_it_once_unregistered_source_rolls_back(db, capsys, monkeypatch):
    """Take SOURCE out of the resolver's tuple: the in-transaction check must see
    the base-price epoch move to today, and roll back."""
    import price_lookup
    monkeypatch.setattr(price_lookup, '_UNIT_REBASE_SOURCES',
                        tuple(s for s in price_lookup._UNIT_REBASE_SOURCES if s != SOURCE))
    before = _state(db)
    assert _run(db, SANDPAPER_ARG) == 1
    out = capsys.readouterr().out
    assert 'ROLLED BACK' in out and 'epoch' in out, out
    assert _state(db) == before


# ── hasps: only after #599 ──────────────────────────────────────────────────

def test_hasps_refused_while_the_map_reads_kr_as_tua(db, capsys):
    before = _state(db)
    assert _run(db, HASP_ARG) == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and '#599' in out, out
    assert _state(db) == before


def test_hasps_refused_even_mixed_with_sandpapers(db, capsys):
    before = _state(db)
    assert _run(db, SANDPAPER_ARG + ',' + HASP_ARG) == 2
    assert '#599' in capsys.readouterr().out
    assert _state(db) == before


def _assert_rebased_hasps(path):
    for pid in HASPS:
        p = _one(path, "SELECT unit_type, cost_price, opening_cost, base_sell_price FROM products"
                       " WHERE id=?", pid)
        assert p[0] == 'ตัว'
        assert p[1] == pytest.approx(684.0 / 144, abs=1e-12) and p[2] == pytest.approx(4.75, abs=1e-12)
        assert p[3] == 6.67
        assert dict(_all(path, "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", pid)) \
            == {'กร': 144.0, 'กุรุส': 144.0, 'ตัว': 1.0}
        assert _one(path, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)[0] == 0
        assert _all(path, "SELECT qty_label, price FROM product_price_tiers WHERE product_id=?", pid) \
            == [('1 กุรุส', 960.0)]
        assert _one(path, "SELECT COUNT(*) FROM sales_transactions WHERE product_id=? AND unit='ตัว'",
                    pid)[0] == 0, "no bill may still say ตัว: that word is now a single hasp"


def test_hasps_rebase_after_599(db599):
    assert _run(db599, HASP_ARG) == 0
    _assert_rebased_hasps(db599)
    legs = _legs(db599, 1187)
    assert len(legs) == 8, legs
    assert legs['IV6702591-3'] == -576 and legs['RR6700379'] == 576, "4 gross = 576 hasps"


def test_hasp_bills_are_relabelled_through_the_declared_path(db599):
    assert _run(db599, HASP_ARG) == 0
    rows = _all(db599, "SELECT unit, change_source, change_actor FROM sales_transactions"
                       " WHERE product_id IN (1187, 1188)")
    assert len(rows) == 8 and set(rows) == {('กุรุส', 'manual', 'test-operator')}, rows
    audits = _all(db599, "SELECT changed_fields FROM audit_log WHERE table_name='sales_transactions'"
                         " AND action='UPDATE' AND user='test-operator'")
    assert len(audits) == 8 and all('กุรุส' in a[0] for a in audits), audits
    assert _all(db599, "SELECT DISTINCT unit FROM purchase_transactions WHERE product_id IN (1187,1188)") \
        == [('กร',)], "purchases already read a gross spelling; not touched"


def test_hasp_cogs_unchanged(db599):
    before = {pid: _cogs(db599, pid) for pid in HASPS}
    assert all(before.values())
    assert _run(db599, HASP_ARG) == 0
    for pid in HASPS:
        assert _cogs(db599, pid) == pytest.approx(before[pid], abs=0.005), pid


def test_hasp_repeat_customer_answer_unchanged(db599):
    """Before, a gross was asked as 'ตัว'. After, the same bill reads 'กุรุส'."""
    before = {pid: _answers(db599, pid) for pid in HASPS}
    assert _run(db599, HASP_ARG) == 0
    after = {pid: _answers(db599, pid) for pid in HASPS}
    assert sum(len(v) for v in before.values()) == 7, before
    assert after == before


@pytest.mark.parametrize('order', ['599-first', '600-first'])
def test_all_six_after_599_and_600(empty_db, empty_db_conn, order):
    path = _make(empty_db, empty_db_conn, order)
    assert _run(path, SANDPAPER_ARG + ',' + HASP_ARG) == 0
    _assert_rebased_sandpapers(path)
    _assert_rebased_hasps(path)


def test_sandpapers_first_then_hasps_after_599(db):
    """The runbook's order: sandpapers now, #599 lands (its migration upserts
    กุรุส from the live กร row), then the hasps."""
    assert _run(db, SANDPAPER_ARG) == 0
    c = sqlite3.connect(db)
    c.execute("UPDATE unit_map SET word='กุรุส' WHERE book='BSN5657' AND spelling='กร'")
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
              " SELECT product_id, 'กุรุส', ratio FROM unit_conversions WHERE bsn_unit='กร'"
              " ON CONFLICT(product_id, bsn_unit) DO UPDATE SET ratio=excluded.ratio")
    c.commit()
    c.close()
    assert _run(db, HASP_ARG) == 0
    _assert_rebased_sandpapers(db)
    _assert_rebased_hasps(db)


# ── the NEXT import: the stored unit must be what the importer writes ───────
# erp-engineering-discipline: a rewrite of a column an importer also writes must
# equal the importer's own output for the same raw input, or the next re-import
# reads it as a changed line and replaces it. Driven through the REAL importer
# (models.imports.import_weekly), fed the lines as the Express weekly file prints
# them: unit code กร.

CODES = {pid: code for pid, _n, _c, _b, _t, code, _e in PRODUCTS}


def _reimport_sales(pids):
    from models import imports
    entries = [{'doc_no': doc, 'date_iso': date, 'product_code_raw': CODES[pid],
                'product_name_raw': 'raw', 'party': cust, 'party_code': ccode, 'qty': qty,
                'unit': 'กร', 'unit_price': price, 'vat_type': vat, 'discount': '',
                'total': net, 'net': net}
               for pid, date, doc, qty, price, net, vat, cust, ccode in SALES if pid in pids]
    return len(entries), imports.import_weekly(entries, 'sales', 'reimport-603.txt',
                                               apply_removals=False)


def _stock(path, pid):
    return _one(path, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)[0]


def test_reimport_after_the_hasp_rebase_is_a_no_op(db599):
    assert _run(db599, HASP_ARG) == 0
    legs = {pid: _legs(db599, pid) for pid in HASPS}
    n, stats = _reimport_sales(HASPS)
    assert n == 8 and stats['unchanged'] == 8 and stats['overwritten'] == 0, stats
    for pid in HASPS:
        assert _stock(db599, pid) == 0 and _legs(db599, pid) == legs[pid]


def test_why_the_hasps_wait_for_599(db, capsys):
    """The harm the map gate prevents, reproduced: with the gate deleted the run
    itself is consistent, and the very next import undoes it."""
    src = _src()
    mutant = src.replace("if word != GROSS:", "if False:")
    assert mutant != src
    assert _run(db, HASP_ARG, mod=_load(mutant)) == 0
    assert _stock(db, 1187) == 0 and _stock(db, 1188) == 0, "control: consistent right after"
    n, stats = _reimport_sales(HASPS)
    assert n == 8 and stats['overwritten'] == 8, stats
    # every gross sold now counts as one hasp; the purchases still count 144
    assert _stock(db, 1187) == 7 * 144 - 7 and _stock(db, 1188) == 4 * 144 - 4


def test_reimport_of_sandpapers_on_either_side_of_599(db):
    assert _run(db, SANDPAPER_ARG) == 0
    n, stats = _reimport_sales(SANDPAPERS)
    assert n == 18 and stats['unchanged'] == 18, stats
    # #599 lands: กร now reads กุรุส, so the old ตัว lines are replaced — at the same 144
    c = sqlite3.connect(db)
    c.execute("UPDATE unit_map SET word='กุรุส' WHERE book='BSN5657' AND spelling='กร'")
    c.commit()
    c.close()
    n, stats = _reimport_sales(SANDPAPERS)
    assert stats['overwritten'] == 18, stats
    for pid in SANDPAPERS:
        assert _stock(db, pid) == 0
        assert set(_all(db, "SELECT unit FROM sales_transactions WHERE product_id=?", pid)) == {('กุรุส',)}
    assert set(v for k, v in _legs(db, 1047).items() if k.startswith('IV')) == {-144}


# ── scope, modes, double runs ───────────────────────────────────────────────

def test_everything_else_is_untouched(db):
    before = _state(db)
    assert _run(db, SANDPAPER_ARG) == 0
    after = _state(db)
    keep = lambda st: [[r for r in part if not set(r[:2]) & set(SANDPAPERS)] for part in st]
    assert keep(before) == keep(after)
    assert any(r[0] == 1186 for r in keep(after)[0]), "control: 1186 is in the comparison"
    assert any(r[0] == 1187 for r in keep(after)[0]), "control: the hasps are in the comparison"


def test_refuses_to_convert_twice(db599):
    for pids in (SANDPAPER_ARG, HASP_ARG):
        assert _run(db599, pids) == 0
        once = _state(db599)
        assert _run(db599, pids) == 2, "the engine's own unit_type guard cannot see a ตัว->ตัว rerun"
        assert _state(db599) == once


def test_pids_are_required_and_limited_to_the_plan(db, capsys):
    before = _state(db)
    for pids in ('1047,1186', '1047,9999', ''):
        assert _run(db, pids) == 2, pids
    assert _state(db) == before


def test_rehearse_refuses_a_file_the_app_opens(tmp_path, db, capsys):
    app_db = tmp_path / 'inventory.db'
    src = sqlite3.connect(db)
    src.execute("VACUUM INTO ?", (str(app_db),))
    src.close()
    assert _run(str(app_db), SANDPAPER_ARG) == 2
    assert 'inventory.db' in capsys.readouterr().out
    assert _one(str(app_db), "SELECT unit_type FROM products WHERE id=1047")[0] == 'ตัว'


def test_live_needs_its_confirmation(db, capsys):
    before = _state(db)
    assert _run(db, SANDPAPER_ARG, mode='live') == 2
    assert _run(db, SANDPAPER_ARG, '--confirm-live', '1047,1048', mode='live') == 2
    assert _state(db) == before
    backups = pathlib.Path(db).parent / 'backups'
    assert not backups.exists() or not any(backups.iterdir()), "no backup without confirmation"


def test_a_refused_live_run_takes_no_backup(db, capsys):
    """The backup comes after the preconditions: create_backup prunes to two, so a
    refused attempt must not rotate a good rollback point out."""
    assert _run(db, HASP_ARG, '--confirm-live', HASP_ARG, mode='live') == 2
    assert '#599' in capsys.readouterr().out
    backups = pathlib.Path(db).parent / 'backups'
    assert not backups.exists() or not any(backups.iterdir())


def test_live_backs_up_then_commits(db):
    assert _run(db, SANDPAPER_ARG, '--confirm-live', SANDPAPER_ARG, mode='live') == 0
    _assert_rebased_sandpapers(db)
    gz = list((pathlib.Path(db).parent / 'backups').glob('auto-pre-unit-rebase-603-*.db.gz'))
    assert len(gz) == 1, gz


def test_invariant_failure_rolls_back(db, capsys):
    mod = _load()
    mod.PLAN[1052]['expect_opening'] = 24
    before = _state(db)
    assert _run(db, SANDPAPER_ARG, mod=mod) == 1
    out = capsys.readouterr().out
    assert 'ROLLED BACK' in out and 'opening recomputed to 3456, expected 24' in out, out
    assert _state(db) == before


# ── preconditions: each refuses BEFORE the first write ──────────────────────

def _drift(path, sql):
    c = sqlite3.connect(path)
    c.executescript(sql)
    c.commit()
    c.close()


# (id, pids, drift, refusal fragment, mutation that deletes the guard, rc with
#  the guard off, what the guard-off run must print). The last two are MEASURED,
#  not assumed: they name what stands behind each guard, so a guard whose
#  backstop disappears goes red. rc 0 = the guard is the only defence against
#  that drift, and the run commits without it.
GUARDS = [
    ('unit_type', SANDPAPER_ARG, "UPDATE products SET unit_type='แผ่น' WHERE id=1048",
     "unit_type is 'แผ่น'", ("if unit_type != OLD_UNIT:", "if False:"), 0, 'COMMITTED'),
    # a second run of the hasps keeps unit_type ตัว: THIS is their double-run guard
    ('money', SANDPAPER_ARG, "UPDATE products SET base_sell_price=810 WHERE id=1047",
     'cost/opening_cost/base', ("if money != plan['money']:", "if False:"), 2, 'rounded UP'),
    ('stock', SANDPAPER_ARG, "INSERT INTO transactions (product_id, txn_type, quantity_change,"
     " unit_mode, note, created_at) VALUES (1049, 'ADJUST', 1, 'unit', 'นับจริง', '2026-09-01 00:00:00')",
     'stock is 1', ("if stock != 0:", "if False:"), 2, 'non-bill ledger rows'),
    ('ratios', SANDPAPER_ARG, "UPDATE unit_conversions SET ratio=144 WHERE product_id=1047 AND bsn_unit='กร'",
     'unit_conversions', ("if ratios != plan['units'] and ratios != with_gross:", "if False:"),
     0, 'COMMITTED'),
    ('tiers', SANDPAPER_ARG, "UPDATE product_price_tiers SET price=810 WHERE product_id=1048",
     'tiers are', ("if tiers != plan['tiers']:", "if False:"), 0, 'COMMITTED'),
    ('opening', SANDPAPER_ARG, "DELETE FROM transactions WHERE product_id=1047 AND note='%s'"
     % OPENING_NOTE, 'opening plug', ("if opening != plan['opening']:", "if False:"), 0, 'COMMITTED'),
    ('extra_ledger', SANDPAPER_ARG, "INSERT INTO transactions (product_id, txn_type, quantity_change,"
     " unit_mode, note, created_at) VALUES (1047, 'ADJUST', 0, 'unit', 'ปรับมือ', '2026-09-01 00:00:00')",
     'non-bill ledger rows', ("if extra != plan['extra']:", "if False:"), 0, 'COMMITTED'),
    ('bills', SANDPAPER_ARG, "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id,"
     " qty, unit, unit_price, net, vat_type, synced_to_stock, customer) VALUES ('2026-09-10',"
     " 'IV6999999-1', 'IV6999999', 1049, 1, 'ตัว', 880, 880, 1, 1, 'x')",
     'bills are', ("if bills != plan['bills'][table]:", "if False:"),
     1, 'opening recomputed to 144, expected 0'),
    ('bill_unit', SANDPAPER_ARG, "UPDATE sales_transactions SET unit='โหล', change_source='manual',"
     " change_actor='t', change_token='t603', change_reason='ทดสอบหน่วยที่ไม่ใช่กุรุส'"
     " WHERE doc_no='IV6802995-6'",
     'not a gross spelling', ("if units - GROSS_SPELLINGS:", "if False:"),
     2, 'no unit_conversions row'),
    ('unsynced', SANDPAPER_ARG, "UPDATE sales_transactions SET synced_to_stock=0 WHERE doc_no='IV6802317-2'",
     'unsynced', ("if n_unsynced:", "if False:"), 0, 'COMMITTED'),
    ('foreign', SANDPAPER_ARG, "INSERT INTO promotions (product_id, promo_name, promo_type,"
     " discount_value, date_start, is_active) VALUES (1052, 'ทดสอบ', 'percent', 10, '2026-01-01', 1)",
     'promotions', ("if n and table not in HANDLED:", "if False:"), 0, 'COMMITTED'),
    # the run itself is consistent without it; the harm lands on the NEXT import
    ('map_599', HASP_ARG, "SELECT 1", '#599', ("if word != GROSS:", "if False:"), 0, 'COMMITTED'),
]


@pytest.mark.parametrize('gid,pids,drift,fragment,mutation,rc_off,backstop', GUARDS,
                         ids=[g[0] for g in GUARDS])
def test_guard_refuses_before_any_write(db, capsys, gid, pids, drift, fragment, mutation,
                                        rc_off, backstop):
    _drift(db, drift)
    before = _state(db)
    assert _run(db, pids) == 2
    out = capsys.readouterr().out
    assert 'REFUSED' in out and fragment in out, out
    assert _state(db) == before


@pytest.mark.parametrize('gid,pids,drift,fragment,mutation,rc_off,backstop', GUARDS,
                         ids=[g[0] for g in GUARDS])
def test_break_it_once_guard_off_behaves_as_measured(db, capsys, gid, pids, drift, fragment,
                                                      mutation, rc_off, backstop):
    """Delete the guard. The run must COMPLETE (nothing swallowed) and end in the
    measured outcome, so a mutant that dies early, or a backstop that goes
    missing, turns this red — not merely "the guard's own text is gone"."""
    old, new = mutation
    src = _src()
    assert src.count(old) == 1, "mutation target must exist exactly once: %r" % old
    mutant = src.replace(old, new)
    assert mutant != src and mutant.count(old) == 0, "the mutation did not land"
    _drift(db, drift)
    before = _state(db)
    rc = _run(db, pids, mod=_load(mutant))
    out = capsys.readouterr().out
    assert rc == rc_off, (rc, out)
    assert fragment not in out and backstop in out, out
    if rc_off:
        assert _state(db) == before, "a refused or rolled-back run wrote something"


def test_plan_base_must_be_base_over_144_rounded_up(db, capsys):
    mod = _load()
    mod.PLAN[1049]['new_base'] = 6.11            # rounded DOWN: a gross of sheets would undercut the tier
    before = _state(db)
    assert _run(db, SANDPAPER_ARG, mod=mod) == 2
    assert 'not 880/144 rounded UP' in capsys.readouterr().out
    assert _state(db) == before


# ── invariants: a wrong conversion rolls back ───────────────────────────────

INVARIANT_MUTATIONS = [
    # the hasp relabel is the whole same-word story: without it the 8 hasp
    # sales post as single pieces
    ('relabel', ("            _relabel(conn, pid, a.operator)\n", "            pass\n"),
     HASP_ARG, 'posted'),
    # 1052's orphan ADJUST left in the old base
    ('extra_scale', ("        _rescale_extra(conn, pid)\n", "        pass\n"), SANDPAPER_ARG,
     'non-bill ledger row'),
    # the engine's blanket 144 on 1052's dozen
    ('keep_ratio', ("        _restore_kept_ratios(conn, pid)\n", "        pass\n"), SANDPAPER_ARG,
     'unit_conversions'),
    # attribution: every cost-ledger note would name the 1050/1320 script
    ('source', ("source=SOURCE)", ")"), SANDPAPER_ARG, 'does not name'),
]


@pytest.mark.parametrize('iid,mutation,pids,fragment', INVARIANT_MUTATIONS,
                         ids=[m[0] for m in INVARIANT_MUTATIONS])
def test_break_it_once_invariants_catch_a_wrong_conversion(db599, capsys, iid, mutation, pids, fragment):
    old, new = mutation
    src = _src()
    assert src.count(old) == 1, "mutation target must exist exactly once: %r" % old
    mutant = src.replace(old, new)
    assert mutant != src
    before = _state(db599)
    rc = _run(db599, pids, mod=_load(mutant))
    out = capsys.readouterr().out
    assert rc in (1, 2) and fragment in out, out
    assert ('ROLLED BACK' in out) or ('REFUSED' in out), out
    assert _state(db599) == before
