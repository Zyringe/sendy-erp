"""2026-09-19 — base unit rebase for pid 689 (โหล -> คู่) and pid 767 (แพ็ค -> ม้วน).

WHY. Issue #586. Each carries a `unit_conversions` row defaulted to 1.0 for a
unit that is not its base unit (the 2026-04-06 backfill), so a bill written in
the small unit posted its quantity as if it were the pack:
  * 689 ถุงมือยาง S สีส้ม, base โหล, `คู่ = 1.0`: IV6702067-4 sold 11 คู่ at
    ฿24.17 (x12 = ฿290.04, the dozen price) and posted -11 โหล.
  * 767 ผ้ายิปซั่ม Eagle One, base แพ็ค, `ม้วน = 1.0`: IV6800087-9 and
    IV6900023-8 sold 150 ม้วน each at ฿13.33 (x3 = ฿39.99, the pack price) and
    each posted -150 แพ็ค.
A back-solved opening ADJUST (+10 / +281) hides the difference exactly.

Put ruled 2026-09-19:
  * 689: base unit โหล -> คู่, 1 โหล = 12 คู่. ADD a tier `1 โหล` = ฿290 so a
    dozen still quotes ฿290. Siblings 690/691 (M/L) are NOT touched.
  * 767: base unit แพ็ค -> ม้วน, 1 แพ็ค = 3 ม้วน (not the 2 #586 inferred from
    cash-vs-cost). Relabel its tier `1 แพค` -> `1 แพ็ค`, ฿40 kept, so the tier
    matches the unit's spelling.
  * The 1050/1320 conventions (2026-08-17): cost divides exactly, sell price
    rounds UP to 2 dp, low_stock_threshold kept, the physical stock preserved
    (7 โหล -> 84 คู่, 48 แพ็ค -> 144 ม้วน), the opening recomputed and never
    rescaled, a negative opening at a labelled tail row, never the head.

The engine is `rebase()` in scripts/2026_09_19_gross_to_piece.py, LOADED rather
than copied. This file adds only what differs from that run: a ratio per
product, a non-zero stock carried into the new unit, the exact pre-state of
these two products, and the tier changes.

⚠ Unlike 1050/1320 these two have live stock, so `preserve_stock` is the old
quantity TIMES the ratio. Passing the old number through would silently shrink
689 from 84 คู่ to 7 คู่.

    python3 scripts/2026_09_19_rebase_689_767.py --db PATH
    python3 scripts/2026_09_19_rebase_689_767.py --db PATH --apply
"""
import argparse
import importlib.util
import math
import os
import re
import sqlite3
import sys

SOURCE = 'script:2026_09_19_rebase_689_767'
ENGINE_FILE = '2026_09_19_gross_to_piece.py'

# The exact pre-state on prod (read 2026-09-19 from prod-2026-09-19T0820Z-post-uc4.db).
# `money` = (cost_price, opening_cost, base_sell_price). `bug_rows` = the ledger
# rows #586 is about, as (doc, posted quantity, bill qty, bill unit).
# `expect_opening` = what the opening must recompute to — the rehearsal's number,
# pinned so a prod that has drifted since rolls back instead of applying.
PLAN = {
    689: {
        'label': 'ถุงมือยาง S สีส้ม', 'old_unit': 'โหล', 'new_unit': 'คู่', 'ratio': 12,
        'money': (181.9, 181.9, 290.0), 'new_base': 24.17, 'stock': 7, 'opening': 10,
        'units': {'คู่', 'หล', 'โหล'},
        'bug_rows': [('IV6702067-4', -11, 11.0, 'คู่')],
        'tiers_before': [], 'tiers_after': [('1 โหล', 290.0)],
        'expect_opening': -1,
    },
    767: {
        'label': 'ผ้ายิปซั่ม Eagle One', 'old_unit': 'แพ็ค', 'new_unit': 'ม้วน', 'ratio': 3,
        'money': (29.21, 29.21, 40.0), 'new_base': 13.34, 'stock': 48, 'opening': 281,
        'units': {'ม้วน', 'แพ', 'แพ็ค'},
        'bug_rows': [('IV6800087-9', -150, 150.0, 'ม้วน'), ('IV6900023-8', -150, 150.0, 'ม้วน')],
        'tiers_before': [('1 แพค', 40.0)], 'tiers_after': [('1 แพ็ค', 40.0)],
        'expect_opening': 243,
    },
}

# Tables that may hold rows for these products, and why each is safe. A row in
# ANY other table that references a product (promotions, platform_skus,
# purchase_order_lines, listing_bundles, ...) carries a quantity, unit or price
# of its own and would need its own conversion story, so the script refuses.
HANDLED = {
    'stock_levels': 'preserved; the mig-080 triggers keep it equal to the ledger',
    'transactions': 'replayed by the engine',
    'sales_transactions': 'the bills the replay re-derives from (only synced_to_stock moves)',
    'purchase_transactions': 'as sales_transactions',
    'unit_conversions': 'rescaled by the engine',
    'product_cost_ledger': 're-denominated by the engine',
    'product_price_tiers': 'pinned before, changed and asserted below',
    'product_price_history': 'append-only audit log',
    'product_locations': 'a shelf code, no quantity',
    'legacy_product_sku_map': 'an old SKU number, no quantity or unit',
    'product_code_mapping': 'bsn_code -> product, no ratio',
}


def _ceil2(x):
    # round to 6 dp first: 24.00 must not become 24.01 through float noise
    return math.ceil(round(x * 100, 6)) / 100


def _product_refs(conn):
    """(table, column) for every column that references a product id.

    Migration snapshot tables are forensic copies no code reads."""
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if table.startswith('migration_') or re.match(r'mig\d+_', table):
            continue
        for col in conn.execute('PRAGMA table_info("%s")' % table):
            if col[1] == 'product_id' or col[1].endswith('_product_id'):
                out.append((table, col[1]))
    return out


def preconditions(conn, eng):
    bad = []
    refs = _product_refs(conn)
    for pid, plan in PLAN.items():
        label = plan['label']
        row = conn.execute("SELECT unit_type, cost_price, opening_cost, base_sell_price "
                           "FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            bad.append("%s (pid %s) does not exist — wrong DB?" % (label, pid))
            continue
        unit_type, money = row[0], tuple(row[1:])
        if unit_type != plan['old_unit']:
            bad.append("%s: unit_type is %r, expected %r%s" % (
                label, unit_type, plan['old_unit'],
                ' — already converted, refusing to convert twice'
                if unit_type == plan['new_unit'] else ''))
            continue
        if money != plan['money']:
            bad.append("%s: cost/base/opening_cost moved to %r, expected %r"
                       % (label, money, plan['money']))
        if plan['new_base'] != _ceil2(money[2] / plan['ratio']):
            bad.append("%s: planned base %.2f is not %g/%g rounded UP (%.2f)" % (
                label, plan['new_base'], money[2], plan['ratio'], _ceil2(money[2] / plan['ratio'])))

        stock = eng._stock(conn, pid)
        if stock != plan['stock']:
            bad.append("%s: stock is %g, expected %g" % (label, stock, plan['stock']))

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        if ratios != {u: 1.0 for u in plan['units']}:
            bad.append("%s: unit_conversions %r, expected exactly %s all at 1.0"
                       % (label, ratios, sorted(plan['units'])))

        tiers = [tuple(r) for r in conn.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=? "
            "ORDER BY qty_label", (pid,))]
        if tiers != plan['tiers_before']:
            bad.append("%s: tiers are %r, expected %r" % (label, tiers, plan['tiers_before']))

        openings = [r[0] for r in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? AND note IN (?,?)",
            (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE))]
        if openings != [plan['opening']]:
            bad.append("%s: opening plug %r, expected [%g]" % (label, openings, plan['opening']))

        for doc, posted, qty, unit in plan['bug_rows']:
            ok = conn.execute(
                "SELECT COUNT(*) FROM transactions t JOIN sales_transactions s "
                "  ON s.doc_no = t.reference_no AND s.product_id = t.product_id "
                " WHERE t.product_id=? AND t.reference_no=? AND t.note='BSN ขาย' "
                "   AND t.quantity_change=? AND s.qty=? AND s.unit=?",
                (pid, doc, posted, qty, unit)).fetchone()[0] == 1
            if not ok:
                bad.append("%s: bug row %s (%g %s posted as %g) is not there as expected"
                           % (label, doc, qty, unit, posted))

        # An unsynced bill means the ledger does not reflect the bills, so the
        # replay would start from a base nobody measured.
        n_unsynced = sum(conn.execute(
            "SELECT COUNT(*) FROM %s WHERE product_id=? AND COALESCE(synced_to_stock,0)=0" % t,
            (pid,)).fetchone()[0] for t in ('sales_transactions', 'purchase_transactions'))
        if n_unsynced:
            bad.append("%s: %d unsynced bill row(s) — sync before converting" % (label, n_unsynced))

        bill_units = {u for (u,) in conn.execute(
            "SELECT unit FROM sales_transactions WHERE product_id=? "
            "UNION SELECT unit FROM purchase_transactions WHERE product_id=?", (pid, pid))}
        if bill_units - plan['units']:
            bad.append("%s: bills use %s with no unit_conversions row — the replay would skip them"
                       % (label, sorted(bill_units - plan['units'])))

        for table, col in refs:
            n = conn.execute('SELECT COUNT(*) FROM "%s" WHERE "%s"=?' % (table, col),
                             (pid,)).fetchone()[0]
            if n and table not in HANDLED:
                bad.append("%s: %d row(s) in %s.%s — needs its own conversion story"
                           % (label, n, table, col))
    return bad


def apply_tiers(conn):
    """689 gains its dozen; 767's tier is relabelled IN PLACE (same row, same history)."""
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
                 "VALUES (689, '1 โหล', 290.0)")
    conn.execute("UPDATE product_price_tiers SET qty_label='1 แพ็ค' "
                 "WHERE product_id=767 AND qty_label='1 แพค'")


def fingerprint_others(conn):
    """Every product outside the plan: money, stock, ratios, tiers and ledger."""
    ids = ','.join(str(p) for p in PLAN)
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
    )


def snapshot(conn, eng):
    out = {}
    for pid in PLAN:
        p = conn.execute("SELECT cost_price, opening_cost, base_sell_price, low_stock_threshold "
                         "FROM products WHERE id=?", (pid,)).fetchone()
        out[pid] = {
            'cost': p[0], 'opening_cost': p[1], 'base': p[2], 'threshold': p[3],
            'stock': eng._stock(conn, pid),
            'tier_ids': {r[0] for r in conn.execute(
                "SELECT id FROM product_price_tiers WHERE product_id=?", (pid,))},
            'n_bills': sum(conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=?" % t,
                                        (pid,)).fetchone()[0]
                           for t in ('sales_transactions', 'purchase_transactions')),
            'n_cost_rows': conn.execute("SELECT COUNT(*) FROM product_cost_ledger "
                                        "WHERE product_id=?", (pid,)).fetchone()[0],
            'ledger_value': conn.execute("SELECT COALESCE(SUM(qty_change*unit_cost),0) FROM "
                                         "product_cost_ledger WHERE product_id=?", (pid,)).fetchone()[0],
        }
    return out


def min_running_balance(conn, pid):
    """Lowest running stock in the order recalculate_product_wacc walks the ledger.

    Below zero at a purchase is where WACC takes its freeze branch."""
    run, low = 0, 0
    for (q,) in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? "
            "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
        run += q
        low = min(low, run)
    return low


def assert_invariants(conn, eng, before, others_before, openings):
    bad = []
    for pid, plan in PLAN.items():
        label, b, r = plan['label'], before[pid], plan['ratio']
        row = conn.execute("SELECT unit_type, cost_price, opening_cost, base_sell_price, "
                           "low_stock_threshold FROM products WHERE id=?", (pid,)).fetchone()
        stock, want = eng._stock(conn, pid), b['stock'] * r

        if row[0] != plan['new_unit']:
            bad.append("%s: unit_type %r != %r" % (label, row[0], plan['new_unit']))
        # THE CONTRACT: the same goods, counted in the new unit.
        if abs(stock - want) > 1e-9:
            bad.append("%s: stock %g != preserved %g (%g %s x %g)"
                       % (label, stock, want, b['stock'], plan['old_unit'], r))
        led = eng._ledger_sum(conn, pid)
        if abs(led - stock) > 1e-9:
            bad.append("%s: stock %g != SUM(ledger) %g" % (label, stock, led))
        if abs(row[1] - b['cost'] / r) > 1e-12:
            bad.append("%s: cost %r != %r (must divide EXACTLY)" % (label, row[1], b['cost'] / r))
        if abs(row[2] - b['opening_cost'] / r) > 1e-12:
            bad.append("%s: opening_cost %r != %r" % (label, row[2], b['opening_cost'] / r))
        if abs(row[3] - plan['new_base']) > 1e-9:
            bad.append("%s: base_sell %r != planned %r" % (label, row[3], plan['new_base']))
        if row[4] != b['threshold']:
            bad.append("%s: low_stock_threshold moved %r -> %r (Put: keep)"
                       % (label, b['threshold'], row[4]))
        if abs(stock * row[1] - b['stock'] * b['cost']) > 0.005:
            bad.append("%s: stock value %.4f != %.4f"
                       % (label, stock * row[1], b['stock'] * b['cost']))

        tiers = [tuple(t) for t in conn.execute(
            "SELECT id, qty_label, price FROM product_price_tiers WHERE product_id=? "
            "ORDER BY qty_label", (pid,))]
        if [t[1:] for t in tiers] != plan['tiers_after']:
            bad.append("%s: tier set %r != planned %r"
                       % (label, [t[1:] for t in tiers], plan['tiers_after']))
        if not b['tier_ids'] <= {t[0] for t in tiers}:
            bad.append("%s: a tier row was deleted — it must be relabelled in place" % label)

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        planned = {u: 1.0 if u == plan['new_unit'] else float(r) for u in plan['units']}
        if ratios != planned:
            bad.append("%s: unit_conversions %r != %r" % (label, ratios, planned))

        # THE CORE CLAIM: every bill now posts its own quantity in the new unit.
        legs = conn.execute(
            "SELECT -1, s.qty, s.unit, t.quantity_change, s.doc_no FROM transactions t "
            "  JOIN sales_transactions s ON s.doc_no = t.reference_no AND s.product_id = t.product_id "
            " WHERE t.product_id=? AND t.note='BSN ขาย' "
            "UNION ALL "
            "SELECT 1, p.qty, p.unit, t.quantity_change, p.doc_no FROM transactions t "
            "  JOIN purchase_transactions p ON p.doc_no = t.reference_no AND p.product_id = t.product_id "
            " WHERE t.product_id=? AND t.note='BSN ซื้อ'", (pid, pid)).fetchall()
        if not legs or len(legs) != b['n_bills']:
            bad.append("%s: %d ledger legs for %d bills" % (label, len(legs), b['n_bills']))
        for sign, qty, unit, change, doc in legs:
            expect = sign * qty * (1.0 if unit == plan['new_unit'] else r)
            if abs(change - expect) > 1e-6:
                bad.append("%s: %s %g %s posted %g, expected %g" % (label, doc, qty, unit, change, expect))

        # The free evidence, pinned: a drifted prod rolls back rather than applies.
        if abs(openings[pid] - plan['expect_opening']) > 1e-9:
            bad.append("%s: opening recomputed to %g, expected %g"
                       % (label, openings[pid], plan['expect_opening']))
        placed = conn.execute(
            "SELECT note, quantity_change, created_at FROM transactions "
            "WHERE product_id=? AND note IN (?,?)",
            (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE)).fetchall()
        head, tail = conn.execute(
            "SELECT MIN(created_at), MAX(created_at) FROM transactions "
            "WHERE product_id=? AND note NOT IN (?,?)",
            (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE)).fetchone()
        if plan['expect_opening'] > 0:
            ok_place = (len(placed) == 1 and placed[0][0] == eng.OPENING_NOTE
                        and placed[0][2] < head)
        else:
            ok_place = (len(placed) == 1 and placed[0][0] == eng.RECONCILE_NOTE
                        and placed[0][2] == tail)
        if not ok_place:
            bad.append("%s: opening row misplaced %r (head %s, tail %s)"
                       % (label, [tuple(p) for p in placed], head, tail))

        lv = conn.execute("SELECT COALESCE(SUM(qty_change*unit_cost),0) FROM product_cost_ledger "
                          "WHERE product_id=?", (pid,)).fetchone()[0]
        if abs(lv - b['ledger_value']) > 0.005:
            bad.append("%s: cost-ledger value %.4f != %.4f" % (label, lv, b['ledger_value']))
        notes = [n for (n,) in conn.execute(
            "SELECT note FROM product_cost_ledger WHERE product_id=?", (pid,))]
        n_named = sum(' แปลงหน่วย ' in n and n.endswith('(%s)' % SOURCE) for n in notes)
        if len(notes) != b['n_cost_rows'] or n_named != len(notes):
            bad.append("%s: cost-ledger note does not name %s on %d of %d rows"
                       % (label, SOURCE, len(notes) - n_named, len(notes)))

    if fingerprint_others(conn) != others_before:
        bad.append("a product OUTSIDE the plan changed")
    return bad


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args(argv)

    eng = _load_engine()
    if eng is None:
        print("REFUSED — cannot locate %s; set SENDY_SCRIPTS_DIR" % ENGINE_FILE)
        return 2
    app_dir = _app_dir()
    if app_dir is None:
        print("REFUSED — cannot locate inventory_app; set SENDY_APP_DIR")
        return 2
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    conn = sqlite3.connect(a.db, timeout=15)
    # as models.database.get_connection: the replay indexes rows by NAME
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        problems = preconditions(conn, eng)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            return 2

        before = snapshot(conn, eng)
        others_before = fingerprint_others(conn)
        print("BEFORE")
        for pid, b in before.items():
            plan = PLAN[pid]
            print("  %-22s %-5s stock %4g  cost %8.4f  base %7.2f  opening-plug %g  min-running %g"
                  % (plan['label'], plan['old_unit'], b['stock'], b['cost'], b['base'],
                     plan['opening'], min_running_balance(conn, pid)))

        conn.execute("INSERT INTO price_change_source (id, source) VALUES (1, ?) "
                     "ON CONFLICT(id) DO UPDATE SET source = excluded.source", (SOURCE,))
        openings = {}
        for pid, plan in PLAN.items():
            openings[pid] = eng.rebase(
                conn, pid, plan['new_unit'], plan['ratio'], plan['new_base'],
                before[pid]['stock'] * plan['ratio'], source=SOURCE)
        apply_tiers(conn)
        conn.execute("INSERT INTO price_change_source (id, source) VALUES (1, NULL) "
                     "ON CONFLICT(id) DO UPDATE SET source = NULL")

        bad = assert_invariants(conn, eng, before, others_before, openings)
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

    print("\nAFTER (in-transaction)")
    for pid, plan in PLAN.items():
        r = conn.execute("SELECT unit_type, cost_price, base_sell_price FROM products WHERE id=?",
                         (pid,)).fetchone()
        print("  %-22s %-5s stock %4g  cost %8.6f  base %7.2f  min-running %g"
              % (plan['label'], r[0], eng._stock(conn, pid), r[1], r[2],
                 min_running_balance(conn, pid)))
    print("\nRECOMPUTED OPENING — the free evidence (negative = a tail row, never the head)")
    for pid, plan in PLAN.items():
        print("    %-22s old plug %5g %s  ->  recomputed %5g %s"
              % (plan['label'], plan['opening'], plan['old_unit'], openings[pid], plan['new_unit']))

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
        unit, base, stock = chk.execute(
            "SELECT p.unit_type, p.base_sell_price, s.quantity FROM products p "
            "JOIN stock_levels s ON s.product_id = p.id WHERE p.id=?", (pid,)).fetchone()
        led = chk.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id=?",
                          (pid,)).fetchone()[0]
        tiers = [tuple(t) for t in chk.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=?", (pid,))]
        good = (unit == plan['new_unit'] and base == plan['new_base']
                and stock == led == plan['stock'] * plan['ratio'] and tiers == plan['tiers_after'])
        n_bad += not good
        print("  %s %-22s %-5s stock %g  ledger %g  base %.2f  tiers %r"
              % ('OK ' if good else 'BAD', plan['label'], unit, stock, led, base, tiers))
    chk.close()
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
