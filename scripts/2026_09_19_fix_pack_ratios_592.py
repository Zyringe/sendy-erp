"""2026-09-19 — pack ratios for 8 products whose pack unit posted as one base unit (#592).

WHY. Each has a `unit_conversions` row left at 1.0 for a pack unit, so a bill
written in the pack took one base unit off the stock ledger instead of a pack.
Express's own pack factor on the same bill lines (`STCRD.TFACTOR`) and the
marketplace `qty_per_sale` agree on every ratio below. Evidence per row:
Operations/05_analysis-reports/data-quality/unit_ratio_suspects_592_2026-09-19.md
in the workspace repo.

Put ruled 2026-09-19 ("A"): 304 ซอง = 12 · 623 ซอง = 10 · 574 / 575 / 576 /
578 / 926 ชุด = 5 · 577 แพ็ค = 5. Nothing else changes: unit_type, prices and
the other conversion rows stay as they are.

CONTRACT: keep the 2026-02-23 physical count. The opening plugs were back-solved
against that count at the BSN cutoff (2026-03-03) on 2026-05-18 and re-pinned on
2026-05-30, so a bill dated on or before the cutoff was already absorbed by it.
What the script keeps is the ledger's sum up to the cutoff UNCHANGED, not equal
to the count: on 576 and 578 it reads 206 / 1,535 against a count of 170 / 275,
because the 2026-07-03 'ล้าง orphan ledger' ADJUSTs (-36 / -1,260) that removed
phantom pre-cutoff rows are stamped after the cutoff. Do not "repair" those
sums. Only bills after the count really overstate stock, and only those move it:
  304 4,199 -> 4,100 · 623 88 -> 52 · 576 69 -> 65 · the other five unchanged.
The replay re-posts every bill at the new ratio; one compensating ADJUST per
product, stamped strictly before its head, puts back what the absorbed bills
now take a second time.

⚠ NOT through /unit-conversions. `bsn_sync.update_unit_conversion_ratio`
replays the ledger but never re-solves the opening, so it subtracts the absorbed
bills again: 578 would land on 231 and 926 on -8.

Stock only. No price is written, so the #613 epoch rule is not involved and this
script has no business in `price_lookup._UNIT_REBASE_SOURCES`; an invariant
below refuses a run that writes any `product_price_history` row.

    python3 scripts/2026_09_19_fix_pack_ratios_592.py --db PATH
    python3 scripts/2026_09_19_fix_pack_ratios_592.py --db PATH --apply
"""
import argparse
import collections
import os
import sqlite3
import sys

CUTOFF = '2026-03-03 23:59:59'
CUTOFF_DATE = CUTOFF[:10]
PIN_NOTE = 'ปรับยอดยกมา: 1 {unit} = {ratio:g} {unit_type} (#592) คงยอดนับ 2026-02-23'

# `absorbed` = every bill in `unit` dated on or before the cutoff, as
# (doc_no, qty), read from prod 2026-09-19 (prod-2026-09-19T1103Z-post-mig187).
# It is history, so it is pinned: a prod that disagrees refuses.
# `later_count` = a physical count after the cutoff that must be kept as well.
PLAN = {
    304: {'label': 'ดจ.สแตนเลสซอง GL 9/64', 'unit_type': 'ดอก', 'unit': 'ซอง', 'ratio': 12,
          'absorbed': []},
    623: {'label': 'ดจ.สแตนเลส SUNFLOWER 7/32', 'unit_type': 'ดอก', 'unit': 'ซอง', 'ratio': 10,
          'absorbed': [('IV6802932-1', 1.0)],
          # the team's 100 SUNFLOWER bits, counted on pid 850's sheet row, moved here
          'later_count': '2026-06-09 14:14:48'},
    575: {'label': 'กระดาษทรายตีนตุ๊กแก #60', 'unit_type': 'แผ่น', 'unit': 'ชุด', 'ratio': 5,
          'absorbed': [('IV6803061-1', 2.0)]},
    578: {'label': 'กระดาษทรายตีนตุ๊กแก #120', 'unit_type': 'แผ่น', 'unit': 'ชุด', 'ratio': 5,
          'absorbed': [('IV6702662-2', 10.0), ('IV6900002-1', 1.0)]},
    577: {'label': 'กระดาษทรายตีนตุ๊กแก #100', 'unit_type': 'แผ่น', 'unit': 'แพ็ค', 'ratio': 5,
          'absorbed': [('IV6900242-1', 1.0)]},
    576: {'label': 'กระดาษทรายตีนตุ๊กแก #80', 'unit_type': 'แผ่น', 'unit': 'ชุด', 'ratio': 5,
          'absorbed': [('IV6803018-1', 1.0)]},
    574: {'label': 'กระดาษทรายตีนตุ๊กแก #40', 'unit_type': 'แผ่น', 'unit': 'ชุด', 'ratio': 5,
          'absorbed': [('IV6802105-1', 1.0)]},
    926: {'label': 'กระดาษทรายกลม เกือกม้า #100', 'unit_type': 'ตัว', 'unit': 'ชุด', 'ratio': 5,
          'absorbed': [('IV6703400-1', 2.0)]},
}


class Refused(Exception):
    """A step found the product outside its expected state. Nothing is committed."""


def _stock(conn, pid):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0


def _ledger_sum(conn, pid, upto=None):
    sql, args = "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?", [pid]
    if upto:
        sql += " AND created_at <= ?"
        args.append(upto)
    return conn.execute(sql, args).fetchone()[0]


def _unit_bills(conn, pid, unit):
    """{doc_no: qty} for the sales lines written in `unit` (credit notes refused upstream)."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT doc_no, qty FROM sales_transactions WHERE product_id=? AND unit=?", (pid, unit))}


def _legs(conn, pid):
    return collections.Counter(tuple(r) for r in conn.execute(
        "SELECT reference_no, note, quantity_change FROM transactions "
        "WHERE product_id=? AND note LIKE 'BSN%'", (pid,)))


def min_running_balance(conn, pid):
    """Lowest running stock in the order recalculate_product_wacc walks the ledger."""
    run, low = 0, 0
    for (q,) in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? "
            "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
        run += q
        low = min(low, run)
    return low


def preconditions(conn, normalize_unit):
    bad = []
    for pid, plan in PLAN.items():
        label, unit = plan['label'], plan['unit']
        row = conn.execute("SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            bad.append("%s (pid %s) does not exist — wrong DB?" % (label, pid))
            continue
        if row[0] != plan['unit_type']:
            bad.append("%s: unit_type is %r, expected %r" % (label, row[0], plan['unit_type']))

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        if ratios.get(plan['unit']) != 1.0:
            bad.append("%s: %s ratio is %r, expected 1.0%s" % (
                label, unit, ratios.get(unit),
                ' — already fixed, refusing to run twice' if ratios.get(unit) == plan['ratio'] else ''))

        twins = sorted(u for u in ratios if u != unit and normalize_unit(u) == unit)
        if twins:
            bad.append("%s: conversion row %s normalises to %s — a bill spelled that way would "
                       "keep its own ratio" % (label, twins, unit))

        n_unsynced = sum(conn.execute(
            "SELECT COUNT(*) FROM %s WHERE product_id=? AND COALESCE(synced_to_stock,0)=0" % t,
            (pid,)).fetchone()[0] for t in ('sales_transactions', 'purchase_transactions'))
        if n_unsynced:
            bad.append("%s: %d unsynced bill row(s) — sync first" % (label, n_unsynced))

        n_purch = conn.execute("SELECT COUNT(*) FROM purchase_transactions "
                               "WHERE product_id=? AND unit=?", (pid, unit)).fetchone()[0]
        if n_purch:
            bad.append("%s: %d line(s) bought in the unit %s — not covered by the ruling"
                       % (label, n_purch, unit))

        n_sr = conn.execute("SELECT COUNT(*) FROM sales_transactions WHERE product_id=? "
                            "AND unit=? AND doc_no LIKE 'SR%'", (pid, unit)).fetchone()[0]
        if n_sr:
            bad.append("%s: %d credit note(s) in %s — they post the other way" % (label, n_sr, unit))

        absorbed = sorted((d, q) for d, q in conn.execute(
            "SELECT doc_no, qty FROM sales_transactions WHERE product_id=? AND unit=? "
            "AND date_iso <= ?", (pid, unit, CUTOFF_DATE)))
        if absorbed != plan['absorbed']:
            bad.append("%s: absorbed bills %r, expected %r" % (label, absorbed, plan['absorbed']))

        if plan.get('later_count'):
            between = conn.execute(
                "SELECT doc_no FROM sales_transactions WHERE product_id=? AND unit=? "
                "AND date_iso > ? AND date_iso <= ?",
                (pid, unit, CUTOFF_DATE, plan['later_count'][:10])).fetchall()
            if between:
                bad.append("%s: %d bill(s) in %s between the cutoff and the later count"
                           % (label, len(between), unit))

        bill_units = {u for (u,) in conn.execute(
            "SELECT unit FROM sales_transactions WHERE product_id=? "
            "UNION SELECT unit FROM purchase_transactions WHERE product_id=?", (pid, pid))}
        unmapped = sorted(bill_units - set(ratios) - {plan['unit_type']})
        if unmapped:
            bad.append("%s: bills use %s with no unit_conversions row — the replay would skip them"
                       % (label, unmapped))
    return bad


def fix(conn, pid, plan, bsn_sync, wacc):
    """Set the ratio, replay, compensate the absorbed bills. Returns the compensation."""
    pinned = _ledger_sum(conn, pid, CUTOFF)
    cur = conn.execute("UPDATE unit_conversions SET ratio=? WHERE product_id=? AND bsn_unit=? "
                       "AND ratio=1.0", (plan['ratio'], pid, plan['unit']))
    if cur.rowcount != 1:
        raise Refused("pid %s: %s row not at 1.0 when written" % (pid, plan['unit']))

    # The app's own Reconciliation Procedure, as update_unit_conversion_ratio
    # runs it: reset synced -> delete every ledger row a BSN sync wrote ->
    # re-sync -> every row synced before is synced again.
    synced_before = bsn_sync._synced_source_ids(conn, pid)
    for table in ('sales_transactions', 'purchase_transactions'):
        conn.execute("UPDATE %s SET synced_to_stock=0 WHERE product_id=?" % table, (pid,))
    conn.execute(
        "DELETE FROM transactions WHERE product_id=? AND ({})".format(
            " OR ".join("note LIKE ?" for _ in bsn_sync._BSN_LEDGER_NOTE_PATTERNS)),
        (pid, *bsn_sync._BSN_LEDGER_NOTE_PATTERNS))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
    lost = synced_before - bsn_sync._synced_source_ids(conn, pid)
    if lost:
        raise Refused("pid %s: replay lost %d movement(s) %r" % (pid, len(lost), sorted(lost)[:5]))

    delta = pinned - _ledger_sum(conn, pid, CUTOFF)
    if delta:
        # Strictly before the head: WACC sorts IN first on a created_at tie, so a
        # row sharing the head's timestamp would be costed after a purchase
        # stamped there (same placement as mapping.repoint_bsn_code).
        stamp = conn.execute("SELECT datetime(MIN(created_at), '-1 second') FROM transactions "
                             "WHERE product_id=?", (pid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
            "reference_no, note, created_at) VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)",
            (pid, delta, PIN_NOTE.format(**plan), stamp))
    wacc.recalculate_product_wacc(pid, conn)
    return delta


def snapshot(conn):
    out = {}
    for pid, plan in PLAN.items():
        out[pid] = {
            'stock': _stock(conn, pid),
            'pinned': _ledger_sum(conn, pid, CUTOFF),
            'later': _ledger_sum(conn, pid, plan['later_count']) if plan.get('later_count') else None,
            'ratios': dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions "
                                        "WHERE product_id=?", (pid,)).fetchall()),
            'legs': _legs(conn, pid),
            'cost': conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0],
            'n_hist': conn.execute("SELECT COUNT(*) FROM product_price_history WHERE product_id=?",
                                   (pid,)).fetchone()[0],
            'low': min_running_balance(conn, pid),
        }
    return out


def expected(conn, pid, plan):
    """(compensation, stock after) derived from the BILLS, not from the ledger."""
    step = plan['ratio'] - 1
    bills = conn.execute("SELECT date_iso, qty FROM sales_transactions WHERE product_id=? AND unit=?",
                         (pid, plan['unit'])).fetchall()
    absorbed = sum(q for d, q in bills if d <= CUTOFF_DATE)
    after = sum(q for d, q in bills if d > CUTOFF_DATE)
    return step * absorbed, _stock(conn, pid) - step * after


def fingerprint_others(conn):
    ids = ','.join(str(p) for p in PLAN)
    q = lambda sql: [tuple(r) for r in conn.execute(sql % ids)]
    return (
        q("SELECT p.id, p.unit_type, p.cost_price, p.opening_cost, p.base_sell_price, "
          "COALESCE(s.quantity, 0) FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id "
          "WHERE p.id NOT IN (%s) ORDER BY p.id"),
        q("SELECT product_id, bsn_unit, ratio FROM unit_conversions "
          "WHERE product_id NOT IN (%s) ORDER BY 1, 2"),
        q("SELECT COUNT(*), SUM(quantity_change), SUM(id) FROM transactions "
          "WHERE product_id NOT IN (%s)"),
        q("SELECT COUNT(*), SUM(qty_change * unit_cost), SUM(id) FROM product_cost_ledger "
          "WHERE product_id NOT IN (%s)"),
        q("SELECT COUNT(*) FROM product_price_history WHERE product_id NOT IN (%s)"),
    )


def assert_invariants(conn, before, want, deltas, others_before):
    bad = []
    for pid, plan in PLAN.items():
        label, unit, r, b = plan['label'], plan['unit'], plan['ratio'], before[pid]
        want_delta, want_stock = want[pid]

        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions "
                                   "WHERE product_id=?", (pid,)).fetchall())
        if ratios != {**b['ratios'], unit: float(r)}:
            bad.append("%s: conversion rows %r" % (label, ratios))

        # every bill in the unit now posts qty x ratio; every other leg is as it was
        bills = _unit_bills(conn, pid, unit)
        legs = _legs(conn, pid)
        mine = collections.Counter({k: n for k, n in legs.items() if k[0] in bills})
        if sum(mine.values()) != len(bills) or not bills:
            bad.append("%s: %d legs for %d bills in %s" % (label, sum(mine.values()), len(bills), unit))
        for (doc, note, change) in mine:
            if note != 'BSN ขาย' or change != -bills[doc] * r:
                bad.append("%s: %s %g %s posted %g (%s), expected %g"
                           % (label, doc, bills[doc], unit, change, note, -bills[doc] * r))
        rest = lambda c: collections.Counter({k: n for k, n in c.items() if k[0] not in bills})
        if rest(legs) != rest(b['legs']):
            bad.append("%s: a bill outside %s posted differently" % (label, unit))

        # THE CONTRACT: the count is kept, and only bills after it move stock
        pinned = _ledger_sum(conn, pid, CUTOFF)
        if pinned != b['pinned']:
            bad.append("%s: count pin broken — ledger to %s is %g, was %g"
                       % (label, CUTOFF_DATE, pinned, b['pinned']))
        if plan.get('later_count') and _ledger_sum(conn, pid, plan['later_count']) != b['later']:
            bad.append("%s: count pin broken at the later count %s" % (label, plan['later_count']))
        stock, led = _stock(conn, pid), _ledger_sum(conn, pid)
        if stock != want_stock:
            bad.append("%s: stock %g, expected %g from the bills" % (label, stock, want_stock))
        if stock != led:
            bad.append("%s: stock %g != SUM(ledger) %g" % (label, stock, led))
        if deltas[pid] != want_delta:
            bad.append("%s: compensation %g, expected %g" % (label, deltas[pid], want_delta))
        n_pin = conn.execute("SELECT COUNT(*) FROM transactions WHERE product_id=? AND note=?",
                             (pid, PIN_NOTE.format(**plan))).fetchone()[0]
        if n_pin != (1 if want_delta else 0):
            bad.append("%s: %d compensation row(s)" % (label, n_pin))

        low = min_running_balance(conn, pid)
        if low < min(0, b['low']):
            bad.append("%s: running balance now dips to %g" % (label, low))
        cost = conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        if abs((cost or 0) - (b['cost'] or 0)) > 1e-9:
            bad.append("%s: cost_price moved %r -> %r" % (label, b['cost'], cost))
        n_hist = conn.execute("SELECT COUNT(*) FROM product_price_history WHERE product_id=?",
                              (pid,)).fetchone()[0]
        if n_hist != b['n_hist']:
            bad.append("%s: %d price-history row(s) written" % (label, n_hist - b['n_hist']))

    if fingerprint_others(conn) != others_before:
        bad.append("a product OUTSIDE the plan changed")
    return bad


def _app_dir():
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isdir(os.path.join(cand, 'models')):
            return cand
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args(argv)

    app_dir = _app_dir()
    if app_dir is None:
        print("REFUSED — cannot locate inventory_app; set SENDY_APP_DIR")
        return 2
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    from bsn_units import normalize_unit
    from models import bsn_sync, wacc

    conn = sqlite3.connect(a.db, timeout=15)
    # as models.database.get_connection: the replay indexes rows by NAME
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        problems = preconditions(conn, normalize_unit)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            return 2

        before = snapshot(conn)
        want = {pid: expected(conn, pid, plan) for pid, plan in PLAN.items()}
        others_before = fingerprint_others(conn)
        print("BEFORE")
        for pid, plan in PLAN.items():
            print("  %-28s 1 %-4s = %-3g %-4s  stock %6g  ledger to %s %6g  -> expect stock %6g, "
                  "compensation %g" % (plan['label'], plan['unit'], plan['ratio'], plan['unit_type'],
                                       before[pid]['stock'], CUTOFF_DATE, before[pid]['pinned'],
                                       want[pid][1], want[pid][0]))
            later = conn.execute(
                "SELECT date_iso, doc_no, qty FROM sales_transactions WHERE product_id=? "
                "AND unit=? AND date_iso > ? ORDER BY date_iso, doc_no",
                (pid, plan['unit'], CUTOFF_DATE)).fetchall()
            print("      after the count: %s" % (', '.join('%s %s %g' % tuple(r) for r in later) or '-'))

        deltas = {pid: fix(conn, pid, plan, bsn_sync, wacc) for pid, plan in PLAN.items()}
        bad = assert_invariants(conn, before, want, deltas, others_before)
    except Refused as exc:
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

    print("\nAFTER (in-transaction)")
    for pid, plan in PLAN.items():
        print("  %-28s stock %6g -> %6g  compensation %g  min-running %g"
              % (plan['label'], before[pid]['stock'], _stock(conn, pid), deltas[pid],
                 min_running_balance(conn, pid)))

    if not a.apply:
        conn.rollback()
        conn.close()
        print("\nREHEARSAL — rolled back, nothing written.")
        return 0

    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    print("\nCOMMITTED — re-read on a new connection:")
    n_bad = 0
    for pid, plan in PLAN.items():
        ratio = chk.execute("SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
                            (pid, plan['unit'])).fetchone()[0]
        stock = chk.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()[0]
        led = chk.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id=?",
                          (pid,)).fetchone()[0]
        pinned = chk.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id=? "
                             "AND created_at <= ?", (pid, CUTOFF)).fetchone()[0]
        good = (ratio == plan['ratio'] and stock == led == want[pid][1]
                and pinned == before[pid]['pinned'])
        n_bad += not good
        print("  %s %-28s 1 %s = %g  stock %g  ledger %g  ledger to %s %g"
              % ('OK ' if good else 'BAD', plan['label'], plan['unit'], ratio, stock, led,
                 CUTOFF_DATE, pinned))
    chk.close()
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
