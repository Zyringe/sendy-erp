"""2026-09-20 — six "กร = 1.0" products onto a piece base unit, 1 กุรุส = 144 (#603).

WHY. Same defect as 1050/1320 (#581): `unit_conversions` says `กร = 1.0` on
products whose base unit `ตัว` is really a gross, because Sendy's unit map read
Express `กร` as ตัว (#595). Put ruled 2026-09-19 on #603:
  * 1047, 1048, 1049, 1052 (กระดาษทรายขัดไม้ จระเข้ #0/#1/#2/#4) -> แผ่น, the word
    1050 already uses.
  * 1187, 1188 (ขอสับ 6 CR / AC) -> ตัว. The 2026 paper catalog sells them as
    "1 กุรุส (144)" at ฿960, the price Sendy holds.
  * 1186: not a gross. 1321, 1338, 1801: parked. None of them is in this plan.
Independent oracle: on the BSN5657 stock card (STCRD) every Sendy-era line of
all six (46 of 46) is unit `กร` with TFACTOR 144, and Express's own base unit is
ใบ (sandpaper) / ตว (hasps).

Conventions carried from 1050/1320 (Put, 2026-08-17): cost divides EXACTLY, the
sell price rounds UP to 2 dp, low_stock_threshold is kept, a tier is the pack
total and is not touched, the opening is recomputed and never rescaled. All six
are at stock 0, so nothing on the shelf moves.

THE SAME-WORD CASE, 1187/1188 (ตัว -> ตัว). Every hasp sale is stored as `ตัว`
and means a gross. Once ตัว is the base unit, `bsn_sync._get_base_qty`,
`price_lookup._bill_ratio` and `sales_filters.base_qty_sql` all read a bill in
the base unit at ratio 1 whatever unit_conversions says, so those bills would
count single hasps: stock, COGS and every last-paid quote off by 144. So:
  1. the bills that say ตัว are relabelled through `declared_update` (mig 173)
     to the unit map's own word for Express กร, i.e. exactly what the importer
     writes for them (กุรุส once #599 lands);
  2. today's base is first given its true word (unit_type ตัว -> กุรุส, inside the
     transaction), so the UNCHANGED engine re-denominates กุรุส -> ตัว and its own
     "already converted" guard still holds.
Until #599 ships the importer writes ตัว for Express กร: the next DBF re-diff
would put the relabel back (`bsn_line._unit_same`) and any new hasp bill would
post one hasp per gross. The hasps therefore REFUSE while the unit map still
reads กร as anything but กุรุส. The sandpapers are safe in either order.

1052 has one hand-written ledger row, the 2026-07-03 orphan cleanup (-24). It was
written in the old base and the engine rescales only bill legs, so it is scaled
here (x144); the opening then recomputes to +3456 (the same 24 gross), which keeps
every WACC blend in the same proportion. 1052's `โหล = 12` (0 bills ever) keeps
12, since 1 โหล = 12 แผ่น, where the engine would set every non-base unit to 144.

#599 adds a กุรุส conversion at the กร row's ratio. This script upserts กุรุส at
1.0 (the old base) before the engine scales every row, so either order ends at
กุรุส = 144.

Modes:
  rehearse  commits to a COPY. Refuses a file named inventory.db (every DB the app
            opens has that name), so it cannot land on prod or a dev DB.
  live      needs --confirm-live repeating --pids, and takes the app's own
            refuse-on-failure backup before the first write.

    python3 scripts/2026_09_20_rebase_gross_603.py --db /tmp/rehearse-603.db \\
        --pids 1047,1048,1049,1052 --mode rehearse --operator NAME
    python3 scripts/2026_09_20_rebase_gross_603.py --db /data/inventory.db \\
        --pids 1047,1048,1049,1052 --mode live --confirm-live 1047,1048,1049,1052 --operator NAME
"""
import argparse
import importlib.util
import math
import os
import re
import sqlite3
import sys
from datetime import date

SOURCE = 'script:2026_09_20_rebase_gross_603'
ENGINE_FILE = '2026_09_19_gross_to_piece.py'
RATIO = 144
OLD_UNIT = 'ตัว'
GROSS = 'กุรุส'
BOOK = 'BSN5657'
# Every spelling a bill of these six uses for ONE GROSS today: ตัว is the old
# base, กร the raw code of batch 37's purchases, กุรุส what #599/#600 write.
GROSS_SPELLINGS = {'ตัว', 'กร', 'กุรุส'}
APP_DB_NAME = 'inventory.db'
BACKUP_REASON = 'pre-unit-rebase-603'
RELABEL_REASON = ('#603: this line is Express กร = 1 กุรุส (STCRD TFACTOR 144); '
                  'ตัว is now a single piece')
ORPHAN_NOTE = 'ล้าง orphan ledger 2026-07-03'


def _bills(sales, purchases):
    return {'sales_transactions': sorted(sales), 'purchase_transactions': sorted(purchases)}


def _plan(label, cost, base, new_base, sales, purchases, **kw):
    """The sandpaper shape; a hasp overrides new_unit, 1052 its extras."""
    plan = {'label': label, 'new_unit': 'แผ่น', 'money': (cost, 0.0, base), 'new_base': new_base,
            'units': {'กร': 1.0, 'ตัว': 1.0}, 'keep': {}, 'tiers': [('1 กุรุส', base)],
            'opening': [0.0], 'extra': [], 'bills': _bills(sales, purchases),
            'expect_opening': 0}
    plan.update(kw)
    return plan


# The exact pre-state on prod, read 2026-09-19 17:07Z (identical to
# prod-2026-09-19T1536Z-pre-592.db). `bills` = (doc_no, qty) per table. A prod
# that has drifted since refuses instead of applying.
PLAN = {
    1047: _plan(
        'กระดาษทรายขัดไม้ จระเข้ #0', 614.02, 800.0, 5.56,
        [('IV6700242-4', 1.0), ('IV6701274-6', 1.0), ('IV6701822-7', 1.0), ('IV6702197-3', 1.0),
         ('IV6703189-5', 1.0), ('IV6800465-1', 1.0), ('IV6802317-1', 1.0), ('IV6802995-5', 1.0)],
        [('RR6700041', 1.0), ('RR6700200', 1.0), ('RR6700289', 1.0), ('RR6700333', 1.0),
         ('RR6700481', 1.0), ('RR6800085', 1.0), ('RR6800407', 1.0), ('RR6800561', 1.0)]),
    1048: _plan(
        'กระดาษทรายขัดไม้ จระเข้ #1', 614.02, 800.0, 5.56,
        [('IV6801500-8', 2.0), ('IV6802317-2', 1.0)],
        [('RR6800265', 2.0), ('RR6800407', 1.0)]),
    1049: _plan(
        'กระดาษทรายขัดไม้ จระเข้ #2', 664.48, 880.0, 6.12,
        [('IV6802995-6', 1.0)], [('RR6800561', 1.0)]),
    1052: _plan(
        'กระดาษทรายขัดไม้ จระเข้ #4', 788.0142307692307, 980.0, 6.81,
        [('IV6700242-5', 1.0), ('IV6701274-8', 1.0), ('IV6702896-3', 1.0), ('IV6703189-6', 1.0),
         ('IV6800068-10', 1.0), ('IV6800126-1', 1.0), ('IV6801500-10', 2.0)],
        [('RR6700041', 1.0), ('RR6700200', 1.0), ('RR6700424', 1.0), ('RR6700481', 1.0),
         ('RR6800014', 2.0), ('RR6800265', 2.0)],
        units={'กร': 1.0, 'ตัว': 1.0, 'โหล': 12.0}, keep={'โหล': 12.0}, opening=[24.0],
        extra=[('ADJUST', -24.0, ORPHAN_NOTE, '2026-07-03 00:00:00')],
        expect_opening=3456),
    1187: _plan(
        'ขอสับ 6 สีโครเมียม (CR)', 684.0, 960.0, 6.67,
        [('IV6701146-6', 1.0), ('IV6702591-3', 4.0), ('IV6702875-4', 1.0), ('IV6801764-11', 1.0)],
        [('RR6700174', 1.0), ('RR6700379', 4.0), ('RR6700415', 1.0), ('RR6800316', 1.0)],
        new_unit='ตัว'),
    1188: _plan(
        'ขอสับ 6 สีรมดำ (AC)', 684.0, 960.0, 6.67,
        [('IV6700203-5', 1.0), ('IV6702875-3', 1.0), ('IV6802085-1', 1.0), ('IV6802850-5', 1.0)],
        [('RR6700038', 1.0), ('RR6700415', 1.0), ('RR6800360', 1.0), ('RR6800515', 1.0)],
        new_unit='ตัว'),
}

# Tables that hold rows for these products, and why each is safe. A row in any
# other table that references a product carries a quantity, unit or price of
# its own and would need its own conversion story, so the script refuses.
HANDLED = {
    'stock_levels': 'kept at 0; the mig-080 triggers keep it equal to the ledger',
    'transactions': 'replayed by the engine; the one hand-written row is scaled',
    'sales_transactions': 'the bills the replay re-derives from; hasp units relabelled',
    'purchase_transactions': 'as sales_transactions',
    'unit_conversions': 'rescaled by the engine',
    'product_cost_ledger': 're-denominated by the engine',
    'product_price_tiers': 'pack totals, pinned and asserted unchanged',
    'product_price_history': 'append-only audit log',
    'product_locations': 'a shelf code, no quantity',
    'legacy_product_sku_map': 'an old SKU number, no quantity or unit',
    'product_code_mapping': 'bsn_code -> product, no ratio',
}


def _ceil2(x):
    # round to 6 dp first: 6.00 must not become 6.01 through float noise
    return math.ceil(round(x * 100, 6)) / 100


def _product_refs(conn):
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if table.startswith('migration_') or re.match(r'mig\d+_', table):
            continue
        for col in conn.execute('PRAGMA table_info("%s")' % table):
            if col[1] == 'product_id' or col[1].endswith('_product_id'):
                out.append((table, col[1]))
    return out


def _extra_rows(conn, eng, pid, with_id=False):
    """Ledger rows neither a bill sync nor the engine's opening wrote."""
    from models import bsn_sync
    pats = bsn_sync._BSN_LEDGER_NOTE_PATTERNS
    rows = conn.execute(
        "SELECT id, txn_type, quantity_change, note, created_at FROM transactions"
        " WHERE product_id=? AND COALESCE(note,'') NOT IN (?,?) AND NOT ({})"
        " ORDER BY created_at, id".format(" OR ".join("COALESCE(note,'') LIKE ?" for _ in pats)),
        (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE, *pats)).fetchall()
    return [tuple(r) if with_id else tuple(r)[1:] for r in rows]


def preconditions(conn, eng, pids):
    import bsn_units
    bad = []
    word = bsn_units.translate('กร', BOOK, conn=conn)
    refs = _product_refs(conn)
    for pid in pids:
        plan = PLAN[pid]
        label = plan['label']
        row = conn.execute("SELECT unit_type, cost_price, opening_cost, base_sell_price "
                           "FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            bad.append("%s (pid %s) does not exist — wrong DB?" % (label, pid))
            continue
        unit_type, money = row[0], tuple(row[1:])
        if unit_type != OLD_UNIT:
            bad.append("%s: unit_type is %r, expected %r — already converted?" % (label, unit_type, OLD_UNIT))
        if money != plan['money']:
            bad.append("%s: cost/opening_cost/base %r, expected %r — already converted?"
                       % (label, money, plan['money']))
        if plan['new_base'] != _ceil2(money[2] / RATIO):
            bad.append("%s: planned base %.2f is not %g/%d rounded UP (%.2f)"
                       % (label, plan['new_base'], money[2], RATIO, _ceil2(money[2] / RATIO)))
        # THE SAME-WORD CASE: see the docstring. Only safe once the importer
        # writes กุรุส for Express กร.
        if plan['new_unit'] == OLD_UNIT:
            if word != GROSS:
                bad.append("%s: needs #599 first — the unit map still reads Express กร as %r, so "
                           "a relabelled bill would be put back to ตัว by the next import and a "
                           "new one would post one piece per gross" % (label, word))

        stock = eng._stock(conn, pid)
        if stock != 0:
            bad.append("%s: stock is %g, expected 0" % (label, stock))

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        with_gross = dict(plan['units'], **{GROSS: 1.0})
        if ratios != plan['units'] and ratios != with_gross:
            bad.append("%s: unit_conversions %r, expected %r (a กุรุส row at 1.0 is allowed: #599)"
                       % (label, ratios, plan['units']))

        tiers = sorted(tuple(r) for r in conn.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=?", (pid,)))
        if tiers != plan['tiers']:
            bad.append("%s: tiers are %r, expected %r" % (label, tiers, plan['tiers']))

        opening = [r[0] for r in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? AND note IN (?,?)",
            (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE))]
        if opening != plan['opening']:
            bad.append("%s: opening plug %r, expected %r" % (label, opening, plan['opening']))

        extra = _extra_rows(conn, eng, pid)
        if extra != plan['extra']:
            bad.append("%s: non-bill ledger rows %r, expected %r — each needs its own story"
                       % (label, extra, plan['extra']))

        units = set()
        for table in ('sales_transactions', 'purchase_transactions'):
            rows = conn.execute("SELECT doc_no, qty, unit FROM %s WHERE product_id=?" % table,
                                (pid,)).fetchall()
            bills = sorted((r[0], r[1]) for r in rows)
            if bills != plan['bills'][table]:
                bad.append("%s: %s bills are %r, expected %r" % (label, table, bills, plan['bills'][table]))
            units |= {r[2] for r in rows}
        if units - GROSS_SPELLINGS:
            bad.append("%s: bill unit %s is not a gross spelling (%s) — the plan assumes every "
                       "bill is one กุรุส" % (label, sorted(units - GROSS_SPELLINGS),
                                            sorted(GROSS_SPELLINGS)))
        if units - set(ratios) - {unit_type}:
            bad.append("%s: bills use %s with no unit_conversions row — the replay would skip them"
                       % (label, sorted(units - set(ratios) - {unit_type})))

        n_unsynced = sum(conn.execute(
            "SELECT COUNT(*) FROM %s WHERE product_id=? AND COALESCE(synced_to_stock,0)=0" % t,
            (pid,)).fetchone()[0] for t in ('sales_transactions', 'purchase_transactions'))
        if n_unsynced:
            bad.append("%s: %d unsynced bill row(s) — sync before converting" % (label, n_unsynced))

        for table, col in refs:
            n = conn.execute('SELECT COUNT(*) FROM "%s" WHERE "%s"=?' % (table, col),
                             (pid,)).fetchone()[0]
            if n and table not in HANDLED:
                bad.append("%s: %d row(s) in %s.%s — needs its own conversion story"
                           % (label, n, table, col))
    return bad


# ── the steps around the engine ─────────────────────────────────────────────

def _relabel(conn, pid, operator):
    """Same-word products only: a bill still in the new base word means a gross.

    It is rewritten to the unit map's own word for Express กร, read here rather
    than typed, so the stored value IS what the importer writes for the same line
    and the next import finds it unchanged. Before #599 that word is ตัว, the
    rewrite is a no-op and the invariants roll the run back: a backstop behind
    the precondition, not instead of it."""
    import bsn_units
    from models._shared import declared_update
    if PLAN[pid]['new_unit'] != OLD_UNIT:
        return 0
    word = bsn_units.translate('กร', BOOK, conn=conn)
    n = 0
    for table in ('sales_transactions', 'purchase_transactions'):
        for (rid,) in conn.execute("SELECT id FROM %s WHERE product_id=? AND unit=? ORDER BY id"
                                   % table, (pid, OLD_UNIT)).fetchall():
            declared_update(conn, table, rid, {'unit': word}, actor=operator,
                            source='manual', reason=RELABEL_REASON)
            n += 1
    return n


def _add_gross_row(conn, pid):
    """กุรุส at the OLD base's 1.0 — the engine then scales it with every other row.
    An upsert, because #599 may already have added it (at 1.0, checked above)."""
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, ?, 1.0) "
                 "ON CONFLICT(product_id, bsn_unit) DO NOTHING", (pid, GROSS))


def _rescale_extra(conn, pid):
    """A hand-written ledger row is denominated in the old base, like the bills."""
    eng = _ENGINE
    for tid, *_ in _extra_rows(conn, eng, pid, with_id=True):
        conn.execute("UPDATE transactions SET quantity_change = quantity_change * ? WHERE id=?",
                     (RATIO, tid))


def _restore_kept_ratios(conn, pid):
    """The engine sets every non-base unit to RATIO; a real dozen is 12 pieces."""
    for unit, ratio in PLAN[pid]['keep'].items():
        conn.execute("UPDATE unit_conversions SET ratio=? WHERE product_id=? AND bsn_unit=?",
                     (ratio, pid, unit))


# ── measurements taken before and after, in the same transaction ───────────

def _cogs(conn, pid):
    """The app's own COGS expression (/accounting) over the product's whole history."""
    import sales_filters
    return conn.execute(
        "SELECT COALESCE(SUM({q} * COALESCE(p.cost_price, 0)), 0) FROM sales_transactions st"
        " JOIN products p ON p.id = st.product_id {j} WHERE st.product_id = ?".format(
            q=sales_filters.base_qty_sql(), j=sales_filters.unit_conversion_join()),
        (pid,)).fetchone()[0]


def _answers(conn, pid, today):
    """{customer_code: (sales row id, unit, basis, price)} — asked in the unit of
    that customer's own latest bill, the way a repeat order is keyed."""
    import price_lookup
    latest = {}
    for sid, code in conn.execute(
            "SELECT id, customer_code FROM sales_transactions WHERE product_id=?"
            " AND customer_code IS NOT NULL ORDER BY date_iso, id", (pid,)):
        latest[code] = sid
    out = {}
    for code, sid in latest.items():
        unit = conn.execute("SELECT unit FROM sales_transactions WHERE id=?", (sid,)).fetchone()[0]
        a = price_lookup.resolve_price(conn, product_id=pid, customer_code=code, unit=unit,
                                       today=today)['answer']
        out[code] = (sid, unit, a['basis'], a['price_per_unit'])
    return out


def _epoch(conn, pid, today):
    import price_lookup
    return price_lookup._epoch_candidates(conn, pid, None, today)['base_changed']


def min_running_balance(conn, pid):
    """Lowest running stock in the order recalculate_product_wacc walks the ledger."""
    run, low = 0, 0
    for (q,) in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? "
            "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
        run += q
        low = min(low, run)
    return low


def snapshot(conn, eng, pids, today):
    out = {}
    for pid in pids:
        p = conn.execute("SELECT cost_price, opening_cost, base_sell_price, low_stock_threshold "
                         "FROM products WHERE id=?", (pid,)).fetchone()
        out[pid] = {
            'cost': p[0], 'opening_cost': p[1], 'base': p[2], 'threshold': p[3],
            'stock': eng._stock(conn, pid),
            'tiers': [tuple(r) for r in conn.execute(
                "SELECT id, qty_label, price FROM product_price_tiers WHERE product_id=? ORDER BY id",
                (pid,))],
            'n_bills': sum(conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=?" % t,
                                        (pid,)).fetchone()[0]
                           for t in ('sales_transactions', 'purchase_transactions')),
            'n_old_word': sum(conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=? AND unit=?" % t,
                                           (pid, OLD_UNIT)).fetchone()[0]
                              for t in ('sales_transactions', 'purchase_transactions')),
            'n_cost_rows': conn.execute("SELECT COUNT(*) FROM product_cost_ledger WHERE product_id=?",
                                        (pid,)).fetchone()[0],
            'ledger_value': conn.execute("SELECT COALESCE(SUM(qty_change*unit_cost),0) FROM "
                                         "product_cost_ledger WHERE product_id=?", (pid,)).fetchone()[0],
            'extra': _extra_rows(conn, eng, pid, with_id=True),
            'cogs': _cogs(conn, pid),
            'answers': _answers(conn, pid, today),
            'epoch': _epoch(conn, pid, today),
            'min_running': min_running_balance(conn, pid),
        }
    return out


def fingerprint_others(conn, pids):
    """Every product outside this run: money, stock, ratios, tiers, ledgers, bills."""
    ids = ','.join(str(p) for p in pids)
    q = lambda sql: [tuple(r) for r in conn.execute(sql % ids)]
    return (
        q("SELECT p.id, p.unit_type, p.cost_price, p.opening_cost, p.base_sell_price, "
          "COALESCE(s.quantity, 0) FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id "
          "WHERE p.id NOT IN (%s) ORDER BY p.id"),
        q("SELECT product_id, bsn_unit, ratio FROM unit_conversions "
          "WHERE product_id NOT IN (%s) ORDER BY 1, 2"),
        q("SELECT id, product_id, qty_label, price FROM product_price_tiers "
          "WHERE product_id NOT IN (%s) ORDER BY 1"),
        q("SELECT COUNT(*), SUM(quantity_change), SUM(id) FROM transactions "
          "WHERE product_id NOT IN (%s)"),
        q("SELECT COUNT(*), SUM(qty_change * unit_cost), SUM(id) FROM product_cost_ledger "
          "WHERE product_id NOT IN (%s)"),
        q("SELECT id, unit, qty, synced_to_stock FROM sales_transactions "
          "WHERE COALESCE(product_id, 0) NOT IN (%s) ORDER BY id"),
        q("SELECT id, unit, qty, synced_to_stock FROM purchase_transactions "
          "WHERE COALESCE(product_id, 0) NOT IN (%s) ORDER BY id"),
    )


def assert_invariants(conn, eng, pids, before, others_before, openings, today):
    bad = []
    for pid in pids:
        plan, b = PLAN[pid], before[pid]
        label, new_unit = plan['label'], plan['new_unit']
        row = conn.execute("SELECT unit_type, cost_price, opening_cost, base_sell_price, "
                           "low_stock_threshold FROM products WHERE id=?", (pid,)).fetchone()
        stock = eng._stock(conn, pid)

        if row[0] != new_unit:
            bad.append("%s: unit_type %r != %r" % (label, row[0], new_unit))
        # THE CONTRACT: the same goods (none), counted in the new unit.
        if stock != 0 or eng._ledger_sum(conn, pid) != 0:
            bad.append("%s: stock %g / SUM(ledger) %g, expected 0" % (label, stock, eng._ledger_sum(conn, pid)))
        if abs(row[1] - b['cost'] / RATIO) > 1e-12:
            bad.append("%s: cost %r != %r (must divide EXACTLY)" % (label, row[1], b['cost'] / RATIO))
        if abs(row[2] - (b['opening_cost'] or b['cost']) / RATIO) > 1e-12:
            bad.append("%s: opening_cost %r != %r" % (label, row[2], (b['opening_cost'] or b['cost']) / RATIO))
        if row[3] != plan['new_base']:
            bad.append("%s: base_sell %r != planned %r" % (label, row[3], plan['new_base']))
        if row[4] != b['threshold']:
            bad.append("%s: low_stock_threshold moved %r -> %r (Put: keep)" % (label, b['threshold'], row[4]))

        tiers = [tuple(r) for r in conn.execute(
            "SELECT id, qty_label, price FROM product_price_tiers WHERE product_id=? ORDER BY id", (pid,))]
        if tiers != b['tiers']:
            bad.append("%s: tiers %r != %r — a tier is the PACK total" % (label, tiers, b['tiers']))

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        planned = {u: float(RATIO) for u in plan['units']}
        planned.update({GROSS: float(RATIO), new_unit: 1.0})
        planned.update(plan['keep'])
        if ratios != planned:
            bad.append("%s: unit_conversions %r != %r" % (label, ratios, planned))

        # THE CORE CLAIM: every bill is one gross per unit of qty, whatever it is
        # spelled — and no bill may still say the new base word.
        legs = conn.execute(
            "SELECT -1, s.qty, s.unit, t.quantity_change, s.doc_no FROM transactions t "
            "  JOIN sales_transactions s ON s.doc_no = t.reference_no AND s.product_id = t.product_id "
            " WHERE t.product_id=? AND t.note LIKE 'BSN%' "
            "UNION ALL "
            "SELECT 1, p.qty, p.unit, t.quantity_change, p.doc_no FROM transactions t "
            "  JOIN purchase_transactions p ON p.doc_no = t.reference_no AND p.product_id = t.product_id "
            " WHERE t.product_id=? AND t.note LIKE 'BSN%'", (pid, pid)).fetchall()
        if not legs or len(legs) != b['n_bills']:
            bad.append("%s: %d ledger legs for %d bills" % (label, len(legs), b['n_bills']))
        for sign, qty, unit, change, doc in legs:
            if abs(change - sign * qty * RATIO) > 1e-6:
                bad.append("%s: %s %g %s posted %g, expected %g"
                           % (label, doc, qty, unit, change, sign * qty * RATIO))
        n_new_word = sum(conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=? AND unit=?" % t,
                                      (pid, new_unit)).fetchone()[0]
                         for t in ('sales_transactions', 'purchase_transactions'))
        if n_new_word:
            bad.append("%s: %d bill(s) still say %s, which now means ONE piece" % (label, n_new_word, new_unit))
        if new_unit == OLD_UNIT:
            n_declared = conn.execute(
                "SELECT COUNT(*) FROM sales_transactions WHERE product_id=? AND unit=? AND "
                "change_reason=?", (pid, GROSS, RELABEL_REASON)).fetchone()[0] + conn.execute(
                "SELECT COUNT(*) FROM purchase_transactions WHERE product_id=? AND unit=? AND "
                "change_reason=?", (pid, GROSS, RELABEL_REASON)).fetchone()[0]
            if n_declared != b['n_old_word']:
                bad.append("%s: %d bill(s) relabelled through the declared path, expected %d"
                           % (label, n_declared, b['n_old_word']))

        for tid, txn_type, qty, note, created_at in b['extra']:
            now = conn.execute("SELECT quantity_change FROM transactions WHERE id=?", (tid,)).fetchone()
            if now is None or abs(now[0] - qty * RATIO) > 1e-9:
                bad.append("%s: non-bill ledger row %s (%s) is %r, expected %g"
                           % (label, tid, note, now and now[0], qty * RATIO))

        # the recomputed opening, pinned: a drifted prod rolls back
        if abs(openings[pid] - plan['expect_opening']) > 1e-9:
            bad.append("%s: opening recomputed to %g, expected %g"
                       % (label, openings[pid], plan['expect_opening']))
        placed = conn.execute(
            "SELECT note, quantity_change, created_at FROM transactions "
            "WHERE product_id=? AND note IN (?,?)", (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE)).fetchall()
        head = conn.execute("SELECT MIN(created_at) FROM transactions WHERE product_id=? AND note NOT IN (?,?)",
                            (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE)).fetchone()[0]
        if plan['expect_opening'] > 0:
            ok_place = (len(placed) == 1 and placed[0][0] == eng.OPENING_NOTE and placed[0][2] < head)
        else:
            ok_place = not placed
        if not ok_place:
            bad.append("%s: opening row misplaced %r (head %s)" % (label, [tuple(p) for p in placed], head))
        if min_running_balance(conn, pid) != b['min_running'] * RATIO:
            bad.append("%s: lowest running stock %g, expected %g (the old one x%d)"
                       % (label, min_running_balance(conn, pid), b['min_running'] * RATIO, RATIO))

        # money: stock value, cost-ledger value, COGS over the whole history
        lv = conn.execute("SELECT COALESCE(SUM(qty_change*unit_cost),0) FROM product_cost_ledger "
                          "WHERE product_id=?", (pid,)).fetchone()[0]
        if abs(lv - b['ledger_value']) > 0.005:
            bad.append("%s: cost-ledger value %.4f != %.4f" % (label, lv, b['ledger_value']))
        notes = [n for (n,) in conn.execute("SELECT note FROM product_cost_ledger WHERE product_id=?", (pid,))]
        n_named = sum(' แปลงหน่วย ' in n and n.endswith('(%s)' % SOURCE) for n in notes)
        if len(notes) != b['n_cost_rows'] or n_named != len(notes):
            bad.append("%s: cost-ledger note does not name %s on %d of %d rows"
                       % (label, SOURCE, len(notes) - n_named, len(notes)))
        cogs = _cogs(conn, pid)
        if abs(cogs - b['cogs']) > 0.005:
            bad.append("%s: COGS over its history %.4f != %.4f" % (label, cogs, b['cogs']))

        # #613: a rebase must not move the base-price epoch, or every repeat
        # customer is quoted list instead of what they last paid
        epoch = _epoch(conn, pid, today)
        if epoch != b['epoch']:
            bad.append("%s: base-price epoch moved %r -> %r — is SOURCE in "
                       "price_lookup._UNIT_REBASE_SOURCES? (#613)" % (label, b['epoch'], epoch))
        after = _answers(conn, pid, today)
        for code, (sid, unit, basis, price) in b['answers'].items():
            a = after.get(code)
            if a is None or a[0] != sid or a[2] != basis or (basis == 'last_paid' and a[3] != price):
                bad.append("%s: customer %s asked %s was %s ฿%s, now %r"
                           % (label, code, unit, basis, price, a))

    if fingerprint_others(conn, pids) != others_before:
        bad.append("a product OUTSIDE this run changed")
    return bad


# ── plumbing ────────────────────────────────────────────────────────────────

_ENGINE = None


def _load_engine():
    """The 1050/1320 script, by path. Tried beside this file first, then where
    the repo sits on the prod container — this file may be copied to /tmp."""
    for d in (os.path.dirname(os.path.abspath(__file__)),
              os.environ.get('SENDY_SCRIPTS_DIR'), '/app/scripts'):
        path = os.path.join(d, ENGINE_FILE) if d else None
        if path and os.path.isfile(path):
            spec = importlib.util.spec_from_file_location('gross_to_piece', path)
            eng = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(eng)
            return eng
    return None


def _app_dir():
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isdir(os.path.join(cand, 'models')):
            return cand
    return None


def _parse_pids(text):
    try:
        pids = [int(p) for p in text.split(',') if p.strip()]
    except ValueError:
        return None
    if not pids or len(set(pids)) != len(pids) or set(pids) - set(PLAN):
        return None
    return pids


def _print_table(conn, eng, pids, title):
    print(title)
    for pid in pids:
        r = conn.execute("SELECT unit_type, cost_price, opening_cost, base_sell_price FROM products "
                         "WHERE id=?", (pid,)).fetchone()
        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        print("  %4d %-28s %-5s stock %g  cost %.6f  opening_cost %.6f  base %.2f  COGS %.2f  %s"
              % (pid, PLAN[pid]['label'], r[0], eng._stock(conn, pid), r[1], r[2], r[3],
                 _cogs(conn, pid), ' '.join('%s=%g' % kv for kv in sorted(ratios.items()))))


def main(argv=None):
    global _ENGINE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--pids', required=True, help='comma list, each one in PLAN: %s' % sorted(PLAN))
    ap.add_argument('--mode', required=True, choices=['rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: repeat --pids exactly')
    ap.add_argument('--operator', required=True, help='who runs it; stamped on every relabelled bill')
    a = ap.parse_args(argv)

    pids = _parse_pids(a.pids)
    if pids is None:
        print("REFUSED — --pids %r must be a non-empty comma list drawn from %s" % (a.pids, sorted(PLAN)))
        return 2
    if a.mode == 'rehearse' and os.path.basename(a.db) == APP_DB_NAME:
        print("REFUSED — rehearse runs on a named COPY; %s is a file the app opens (%s)" % (a.db, APP_DB_NAME))
        return 2
    if a.mode == 'live' and a.confirm_live != a.pids:
        print("REFUSED — live needs --confirm-live %s (repeat --pids exactly)" % a.pids)
        return 2
    if not os.path.isfile(a.db):
        print("REFUSED — no DB at %s" % a.db)
        return 2

    eng = _ENGINE = _load_engine()
    if eng is None:
        print("REFUSED — cannot locate %s; set SENDY_SCRIPTS_DIR" % ENGINE_FILE)
        return 2
    app_dir = _app_dir()
    if app_dir is None:
        print("REFUSED — cannot locate inventory_app; set SENDY_APP_DIR")
        return 2
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    today = date.today().isoformat()
    conn = sqlite3.connect(a.db, timeout=15)
    # as models.database.get_connection: the replay and the resolver index rows by NAME
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        problems = preconditions(conn, eng, pids)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            return 2

        if a.mode == 'live':
            # After the preconditions, so a refused run leaves the backup rotation
            # alone (create_backup prunes to 2). A reader, so WAL lets it run
            # beside this connection's write lock and it sees the committed state.
            import db_backup
            info = db_backup.guarded_backup(BACKUP_REASON, policy='refuse', db_path=a.db,
                                            backup_dir=db_backup.default_backup_dir(a.db))
            print("BACKUP", info)

        before = snapshot(conn, eng, pids, today)
        others_before = fingerprint_others(conn, pids)
        _print_table(conn, eng, pids, "BEFORE")

        conn.execute("INSERT INTO price_change_source (id, source) VALUES (1, ?) "
                     "ON CONFLICT(id) DO UPDATE SET source = excluded.source", (SOURCE,))
        openings = {}
        for pid in pids:
            plan = PLAN[pid]
            _relabel(conn, pid, a.operator)
            _add_gross_row(conn, pid)
            _rescale_extra(conn, pid)
            # name today's base by its true word, so the engine re-denominates
            # กุรุส -> piece (and its "already converted" guard still holds for ตัว)
            conn.execute("UPDATE products SET unit_type=? WHERE id=?", (GROSS, pid))
            openings[pid] = eng.rebase(conn, pid, plan['new_unit'], RATIO, plan['new_base'], 0,
                                       source=SOURCE)
            _restore_kept_ratios(conn, pid)
        conn.execute("INSERT INTO price_change_source (id, source) VALUES (1, NULL) "
                     "ON CONFLICT(id) DO UPDATE SET source = NULL")

        bad = assert_invariants(conn, eng, pids, before, others_before, openings, today)
    except eng.RebaseRefused as exc:
        conn.rollback()
        print("REFUSED —", exc)
        return 2
    except Exception:
        conn.rollback()
        raise

    if bad:
        conn.rollback()
        print("\nROLLED BACK — invariants failed:")
        for p in bad:
            print("  ✗", p)
        return 1

    _print_table(conn, eng, pids, "\nAFTER (in-transaction)")
    print("\nOPENING (recomputed, never rescaled) and lowest running stock")
    for pid in pids:
        print("  %4d %-28s opening %g   lowest running %g -> %g"
              % (pid, PLAN[pid]['label'], openings[pid], before[pid]['min_running'],
                 min_running_balance(conn, pid)))
    print("\nREPEAT CUSTOMERS (asked in their own latest bill's unit; last_paid must not move)")
    after = {pid: _answers(conn, pid, today) for pid in pids}
    for pid in pids:
        for code, (sid, unit, basis, price) in sorted(before[pid]['answers'].items()):
            a2 = after[pid][code]
            print("  %4d %-10s %-5s %-18s ฿%-10s -> %-5s %-18s ฿%s"
                  % (pid, code, unit, basis, price, a2[1], a2[2], a2[3]))

    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    print("\nCOMMITTED (%s) — re-read on a new connection:" % a.mode)
    n_bad = 0
    for pid in pids:
        plan = PLAN[pid]
        unit, base, stock = chk.execute(
            "SELECT p.unit_type, p.base_sell_price, s.quantity FROM products p "
            "JOIN stock_levels s ON s.product_id = p.id WHERE p.id=?", (pid,)).fetchone()
        led = chk.execute("SELECT COALESCE(SUM(quantity_change),0) FROM transactions WHERE product_id=?",
                          (pid,)).fetchone()[0]
        good = unit == plan['new_unit'] and base == plan['new_base'] and stock == led == 0
        n_bad += not good
        print("  %s %4d %-28s %-5s stock %g  ledger %g  base %.2f"
              % ('OK ' if good else 'BAD', pid, plan['label'], unit, stock, led, base))
    chk.close()
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
