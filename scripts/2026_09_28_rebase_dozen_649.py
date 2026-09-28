"""2026-09-28 — five โหล-based products onto an อัน base, 1 โหล = 12 (#649).

WHY. Put's standing rule (2026-09-22): `unit_type` and cost are stored per the
SMALLEST unit, the B2B price follows the catalogue, and the pack price lives in
a `product_price_tiers` row. 831 and 1867-1870 still carry `unit_type = โหล`
while their own family siblings 1865/1866 are already `อัน` with `โหล = 12` and
a `1 โหล` tier. Put chose (2026-09-23, catalogue price review Q2 option A) to
price them per โหล first and rebase afterwards, so quotes stayed right meanwhile.

    pid   catalogue    cost/โหล   stock      -> base/อัน   tier `1 โหล`
    831   690 /โหล     447.23     3 โหล         57.50      690
    1867  700 /โหล     498.75     0             58.333…    700
    1868  800 /โหล     570.00     0             66.666…    800
    1869  450 /โหล     320.62     0             37.50      450
    1870  445 /โหล     317.06     0             37.083…    445

The rebase itself is `scripts/2026_09_19_gross_to_piece.py`'s engine, the same
one #581 and #603 used. This file is the plan, the preconditions and the
invariants. Four things are specific to these five and are why it is not just
another --pids run of the 603 script:

1. THE SELL PRICE DIVIDES EXACTLY, IT IS NOT ROUNDED UP. 603 rounds up to 2 dp
   (a convention from the 2026-08-17 โหล->ตัว batch). Here the target is to look
   like 1865/1866, whose bases the 2026-09-23 catalogue import wrote by exact
   division and left unrounded (`46.666666666666664` for a ฿560 โหล). Exact
   division also buys an invariant that rounding would destroy:
   `base * 12 == the catalogue's โหล price`, asserted below to 1e-9. The tier
   carries the catalogue figure itself, so the โหล quote is exact either way; it
   is the per-อัน quote that would drift by a fraction of a satang.

2. 831 HAS NO `unit_conversions` ROW AT ALL. The engine rescales the rows it
   finds and inserts the new base at 1.0, so 831 would come out with `อัน = 1`
   and NO `โหล = 12` — the one row the issue actually asks for. Worse, the
   replay in the engine's step 3 converts each bill through `unit_conversions`,
   and every bill 831 has ever had is written in โหล. So `โหล = 1.0` is seeded
   BEFORE the rebase (its true ratio today, since unit_type is โหล), and the
   engine's own rescale then takes it to 12. That makes 831's pre-state identical
   to 1867-1870's instead of a special case inside the engine.

3. 831 CARRIES AN ACTIVE PROMOTION, which the engine's own `MUST_BE_EMPTY` list
   would refuse outright. It is `percent` (10%, from 2026-09-23, `catalog-import`),
   and a percentage is ratio-invariant: 10% off ฿690/โหล and 10% off ฿57.50/อัน
   are the same deal. A FIXED-amount promo would not be — it would silently
   become 12x its intended discount. So percent promos are allowed through and
   asserted byte-identical afterwards; anything else refuses.

4. THE `1 โหล` TIER IS CREATED HERE. 603 pins existing tiers as unchanged; none
   of these five has one, and without it the โหล price disappears from the
   catalogue the moment the base unit changes.

⚠ `SOURCE` MUST ALREADY BE IN `price_lookup._UNIT_REBASE_SOURCES` ON PROD.
`_epoch_candidates` treats a `base_sell_price` change as a new price epoch unless
its source is on that list, and a new epoch makes every repeat customer get
quoted list price instead of their last-paid. That is not hypothetical: it hit
1050/1320 on prod for ~7 hours on 2026-09-19 (#613). The allowlist is read at
QUERY time, so the line must be DEPLOYED BEFORE this script runs live, not after.
Preconditions refuse if it is missing, rather than letting the epoch invariant
discover it after the writes.

Modes:
  rehearse  commits to a COPY. Refuses a file named inventory.db (every DB the
            app opens has that name), so it cannot land on prod or a dev DB.
  live      needs --confirm-live repeating --pids, and takes the app's own
            refuse-on-failure backup before the first write.

    python3 scripts/2026_09_28_rebase_dozen_649.py --db /tmp/rehearse-649.db \\
        --pids 831,1867,1868,1869,1870 --mode rehearse \\
        --operator put --reason "#649 rehearsal"
    python3 scripts/2026_09_28_rebase_dozen_649.py --db /data/inventory.db \\
        --pids 831,1867,1868,1869,1870 --mode live \\
        --confirm-live 831,1867,1868,1869,1870 \\
        --operator put --reason "#649 โหล -> อัน"
--operator and --reason are required: the cost writes are signed with them (#590).
"""
import argparse
import importlib.util
import os
import sqlite3
import sys
from datetime import date

SOURCE = 'script:2026_09_28_rebase_dozen_649'
ENGINE_FILE = '2026_09_19_gross_to_piece.py'
RATIO = 12
OLD_UNIT = 'โหล'
NEW_UNIT = 'อัน'
TIER_LABEL = '1 โหล'
APP_DB_NAME = 'inventory.db'
BACKUP_REASON = 'pre-unit-rebase-649'

# pid: label, the catalogue's โหล price (= the tier), cost per โหล, stock in โหล.
# Every figure read off prod 2026-09-28 and asserted again as a precondition.
PLAN = {
    831:  dict(label='เหล็กสกัดหุ้มยางปากชุบแข็ง+ Eagle One', dozen=690.0, cost=447.23, stock=3),
    1867: dict(label='สิ่วลบเหลี่ยมด้ามไม้ 1.25in',           dozen=700.0, cost=498.75, stock=0),
    1868: dict(label='สิ่วลบเหลี่ยมด้ามไม้ 1.5in',            dozen=800.0, cost=570.00, stock=0),
    1869: dict(label='สิ่วลบเหลี่ยมด้ามไม้ 1/2in',            dozen=450.0, cost=320.62, stock=0),
    1870: dict(label='สิ่วลบเหลี่ยมด้ามไม้ 3/8in',            dozen=445.0, cost=317.06, stock=0),
}
for _p in PLAN.values():
    _p['new_base'] = _p['dozen'] / RATIO
    _p['preserve'] = _p['stock'] * RATIO

# Every table that names a product AND holds rows for these five, with what happens
# to it here. It is not a hand-kept inventory of the schema: `preconditions` scans
# sqlite_master and REFUSES if any product-keyed table outside this dict holds a row
# for a pid being rebased, so a table added later cannot be silently skipped, and one
# that never applies here costs nobody a line.
TABLE_STORY = {
    'products': 'unit_type, cost, opening_cost and base_sell_price are rewritten',
    'stock_levels': 'the mig-080 triggers keep it equal to the ledger; x12 for 831, 0 for the rest',
    'transactions': 'replayed by the engine; 831 opening is recomputed, never rescaled',
    'sales_transactions': 'the bills the replay re-derives from (only synced_to_stock moves)',
    'purchase_transactions': 'as sales_transactions',
    'unit_conversions': 'rescaled by the engine; โหล seeded at 1.0 first for 831',
    'product_cost_ledger': 're-denominated by the engine',
    'product_price_tiers': 'the 1 โหล tier is CREATED here at the catalogue price',
    'product_price_history': 'append-only audit log, stamped with SOURCE',
    'promotions': 'percent only, ratio-invariant, asserted unchanged',
    'product_locations': 'a shelf code, no quantity',
    'legacy_product_sku_map': 'an old SKU number, no quantity or unit',
    'product_code_mapping': 'bsn_code -> product, no ratio',
    'platform_skus': 'required empty',
    'ecommerce_listings': 'required empty',
    'conversion_formula_inputs': 'required empty',
    'migration_186_uc_deleted': 'audit trail of rows migration 186 removed; history, '
                                'read by nothing that prices or counts. 4 rows here.',
}
PRODUCT_COLS = ('product_id', 'internal_product_id')
MUST_BE_EMPTY = (
    ('platform_skus', 'internal_product_id'),
    ('ecommerce_listings', 'product_id'),
    ('conversion_formula_inputs', 'product_id'),
)

_ENGINE = None


def _money(x):
    return round(x + 0.0, 2)


def preconditions(conn, eng, pids):
    """Every reason to refuse, collected before the first write."""
    bad = []

    import price_lookup
    if SOURCE not in price_lookup._UNIT_REBASE_SOURCES:
        bad.append(
            "%s is NOT in price_lookup._UNIT_REBASE_SOURCES on this deployment. Ship that one "
            "line first or every repeat customer of these five gets quoted list price (#613)."
            % SOURCE)

    for pid in pids:
        plan = PLAN[pid]
        label = '%s %s' % (pid, plan['label'][:24])
        row = conn.execute(
            "SELECT unit_type, cost_price, base_sell_price, low_stock_threshold "
            "FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            bad.append("%s: product does not exist — wrong DB?" % label)
            continue
        unit_type, cost, base, _threshold = row
        if unit_type != OLD_UNIT:
            bad.append("%s: unit_type is %r, expected %r (already rebased?)"
                       % (label, unit_type, OLD_UNIT))
        if _money(base) != _money(plan['dozen']):
            bad.append("%s: base_sell_price %r, plan says the catalogue prints %r"
                       % (label, base, plan['dozen']))
        if _money(cost) != _money(plan['cost']):
            bad.append("%s: cost_price %r, plan says %r" % (label, cost, plan['cost']))

        stock = eng._stock(conn, pid)
        if _money(stock) != _money(plan['stock']):
            bad.append("%s: stock %r โหล, plan says %r" % (label, stock, plan['stock']))
        ledger = eng._ledger_sum(conn, pid)
        if _money(ledger) != _money(stock):
            bad.append("%s: stock %r != ledger %r before we start" % (label, stock, ledger))

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        if ratios not in ({}, {OLD_UNIT: 1.0}):
            bad.append("%s: unit_conversions is %r, expected {} or {%r: 1.0}"
                       % (label, ratios, OLD_UNIT))

        tiers = conn.execute(
            "SELECT COUNT(*) FROM product_price_tiers WHERE product_id=?", (pid,)).fetchone()[0]
        if tiers:
            bad.append("%s: already has %d price tier(s); this script CREATES the %r tier"
                       % (label, tiers, TIER_LABEL))

        # a percentage survives a ratio change untouched; a fixed amount does not
        for pr in conn.execute(
                "SELECT id, promo_type, discount_value FROM promotions WHERE product_id=?", (pid,)):
            if pr[1] != 'percent':
                bad.append("%s: promotion %s is %r (%s) — only a percent promo is ratio-invariant"
                           % (label, pr[0], pr[1], pr[2]))

        # the replay reads each bill's own unit; anything but โหล means the ratio
        # it would be converted at is not the one this plan reasoned about
        for table in ('sales_transactions', 'purchase_transactions'):
            for (u, n) in conn.execute(
                    "SELECT unit, COUNT(*) FROM %s WHERE product_id=? GROUP BY unit" % table,
                    (pid,)):
                if u != OLD_UNIT:
                    bad.append("%s: %d %s row(s) in unit %r, expected only %r"
                               % (label, n, table, u, OLD_UNIT))

        for table, col in MUST_BE_EMPTY:
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col), (pid,)).fetchone()[0]
            if n:
                bad.append("%s: %s holds %d row(s); it has no conversion story here" % (label, table, n))

    # Nothing keyed on these products may be left undecided. Measured, not listed:
    # a table added to the schema after this was written shows up here the first
    # time it actually holds one of these products.
    marks = ','.join('?' * len(pids))
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if table in TABLE_STORY:
            continue
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for col in PRODUCT_COLS:
            if col not in cols:
                continue
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)" % (table, col, marks),
                             pids).fetchone()[0]
            if n:
                bad.append("%s holds %d row(s) for these products and has no story in TABLE_STORY "
                           "— decide what happens to it before rebasing" % (table, n))
    return bad


def _cogs(conn, pid):
    """The app's own COGS expression (/accounting) over the product's whole history.

    Physically nothing moves, so this is the money oracle: base qty scales x12
    and cost_price divides by 12, and the product must come out to the satang.
    """
    import sales_filters
    return conn.execute(
        "SELECT COALESCE(SUM({q} * COALESCE(p.cost_price, 0)), 0) FROM sales_transactions st"
        " JOIN products p ON p.id = st.product_id {j} WHERE st.product_id = ?".format(
            q=sales_filters.base_qty_sql(), j=sales_filters.unit_conversion_join()),
        (pid,)).fetchone()[0]


def _epoch(conn, pid, today):
    import price_lookup
    return price_lookup._epoch_candidates(conn, pid, None, today)['base_changed']


def _answers(conn, pid, today):
    """{customer_code: (unit, basis, price)} asked in each customer's own latest
    bill unit — the way a repeat order is keyed. Must not move."""
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
        out[code] = (unit, a['basis'], a['price_per_unit'])
    return out


def snapshot(conn, eng, pids, today):
    before = {}
    for pid in pids:
        p = conn.execute(
            "SELECT unit_type, cost_price, base_sell_price, opening_cost, low_stock_threshold "
            "FROM products WHERE id=?", (pid,)).fetchone()
        before[pid] = {
            'unit_type': p[0], 'cost': p[1], 'base': p[2], 'opening_cost': p[3], 'threshold': p[4],
            'stock': eng._stock(conn, pid),
            'cogs': _cogs(conn, pid),
            'epoch': _epoch(conn, pid, today),
            'answers': _answers(conn, pid, today),
            'promos': [tuple(r) for r in conn.execute(
                "SELECT id, promo_type, discount_value, date_start, date_end, is_active, source "
                "FROM promotions WHERE product_id=? ORDER BY id", (pid,))],
            'cost_ledger': [tuple(r) for r in conn.execute(
                "SELECT id, qty_change, unit_cost, stock_after, wacc_after FROM product_cost_ledger "
                "WHERE product_id=? ORDER BY id", (pid,))],
            'bill_ids': (
                {r[0] for r in conn.execute(
                    "SELECT id FROM sales_transactions WHERE product_id=?", (pid,))},
                {r[0] for r in conn.execute(
                    "SELECT id FROM purchase_transactions WHERE product_id=?", (pid,))}),
        }
    return before


def fingerprint_others(conn, pids):
    """Every other product's shape. Nothing outside `pids` may move."""
    marks = ','.join('?' * len(pids))
    q = lambda sql: conn.execute(sql, pids).fetchall()
    return (
        q("SELECT p.id, p.unit_type, p.cost_price, p.base_sell_price, p.opening_cost, "
          "COALESCE(s.quantity, 0) FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id "
          "WHERE p.id NOT IN (%s) ORDER BY p.id" % marks),
        q("SELECT product_id, bsn_unit, ratio FROM unit_conversions WHERE product_id NOT IN (%s) "
          "ORDER BY product_id, bsn_unit" % marks),
        q("SELECT id, product_id, qty_label, price FROM product_price_tiers "
          "WHERE product_id NOT IN (%s) ORDER BY id" % marks),
        q("SELECT COUNT(*), COALESCE(SUM(quantity_change), 0), COALESCE(SUM(id), 0) "
          "FROM transactions WHERE product_id NOT IN (%s)" % marks),
    )


def assert_invariants(conn, eng, pids, before, others_before, openings, today):
    bad = []
    for pid in pids:
        plan, b = PLAN[pid], before[pid]
        label = '%s %s' % (pid, plan['label'][:24])

        row = conn.execute(
            "SELECT unit_type, cost_price, base_sell_price, opening_cost, low_stock_threshold "
            "FROM products WHERE id=?", (pid,)).fetchone()
        if row[0] != NEW_UNIT:
            bad.append("%s: unit_type %r != %r" % (label, row[0], NEW_UNIT))
        if abs(row[1] - b['cost'] / RATIO) > 1e-12:
            bad.append("%s: cost %r != %r (must divide EXACTLY)" % (label, row[1], b['cost'] / RATIO))
        if abs(row[3] - (b['opening_cost'] or b['cost']) / RATIO) > 1e-12:
            bad.append("%s: opening_cost %r != %r" % (label, row[3], (b['opening_cost'] or b['cost']) / RATIO))
        if abs(row[2] - plan['new_base']) > 1e-12:
            bad.append("%s: base %r != %r" % (label, row[2], plan['new_base']))
        # the whole reason the base is not rounded up
        if abs(row[2] * RATIO - plan['dozen']) > 1e-9:
            bad.append("%s: base x %d = %r, catalogue prints %r" % (label, RATIO, row[2] * RATIO, plan['dozen']))
        if row[4] != b['threshold']:
            bad.append("%s: low_stock_threshold %r != %r (Put 08-17: keep it)" % (label, row[4], b['threshold']))

        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        if ratios != {NEW_UNIT: 1.0, OLD_UNIT: float(RATIO)}:
            bad.append("%s: unit_conversions %r != {%r: 1.0, %r: %s}"
                       % (label, ratios, NEW_UNIT, OLD_UNIT, float(RATIO)))

        tiers = conn.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=? ORDER BY id",
            (pid,)).fetchall()
        if len(tiers) != 1 or tiers[0][0] != TIER_LABEL or abs(tiers[0][1] - plan['dozen']) > 1e-9:
            bad.append("%s: tiers %r, expected exactly [(%r, %r)]"
                       % (label, tiers, TIER_LABEL, plan['dozen']))

        stock, ledger = eng._stock(conn, pid), eng._ledger_sum(conn, pid)
        if abs(stock - plan['preserve']) > 1e-9:
            bad.append("%s: stock %r != %r (%r โหล x %d)" % (label, stock, plan['preserve'], plan['stock'], RATIO))
        if abs(ledger - stock) > 1e-9:
            bad.append("%s: ledger %r != stock_levels %r" % (label, ledger, stock))
        if min_running_balance(conn, pid) < -1e-9:
            bad.append("%s: running stock goes negative — WACC would freeze" % label)

        # money oracle: same physical history, re-denominated
        cogs = _cogs(conn, pid)
        if abs(cogs - b['cogs']) > 0.005:
            bad.append("%s: COGS moved %.4f -> %.4f" % (label, b['cogs'], cogs))

        promos = [tuple(r) for r in conn.execute(
            "SELECT id, promo_type, discount_value, date_start, date_end, is_active, source "
            "FROM promotions WHERE product_id=? ORDER BY id", (pid,))]
        if promos != b['promos']:
            bad.append("%s: promotions changed %r -> %r" % (label, b['promos'], promos))

        # no bill row may appear or vanish; only synced_to_stock moves
        now_ids = ({r[0] for r in conn.execute(
                        "SELECT id FROM sales_transactions WHERE product_id=?", (pid,))},
                   {r[0] for r in conn.execute(
                        "SELECT id FROM purchase_transactions WHERE product_id=?", (pid,))})
        if now_ids != b['bill_ids']:
            bad.append("%s: the bill set changed" % label)

        cl = [tuple(r) for r in conn.execute(
            "SELECT id, qty_change, unit_cost, stock_after, wacc_after FROM product_cost_ledger "
            "WHERE product_id=? ORDER BY id", (pid,))]
        if len(cl) != len(b['cost_ledger']):
            bad.append("%s: cost ledger went from %d to %d rows" % (label, len(b['cost_ledger']), len(cl)))
        else:
            for was, now in zip(b['cost_ledger'], cl):
                if was[0] != now[0]:
                    bad.append("%s: cost ledger row ids moved" % label)
                elif (abs(now[1] - was[1] * RATIO) > 1e-9 or abs(now[2] - was[2] / RATIO) > 1e-9
                      or abs(now[3] - was[3] * RATIO) > 1e-9 or abs(now[4] - was[4] / RATIO) > 1e-9):
                    bad.append("%s: cost ledger row %s not re-denominated by %d" % (label, was[0], RATIO))
        n_named = conn.execute(
            "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id=? AND note LIKE ?",
            (pid, '%(' + SOURCE + ')')).fetchone()[0]
        if n_named != len(cl):
            bad.append("%s: %d of %d cost-ledger notes name %s" % (label, n_named, len(cl), SOURCE))

        epoch = _epoch(conn, pid, today)
        if epoch != b['epoch']:
            bad.append("%s: base-price epoch moved %r -> %r — is SOURCE in "
                       "price_lookup._UNIT_REBASE_SOURCES? (#613)" % (label, b['epoch'], epoch))
        for code, was in sorted(b['answers'].items()):
            now = _answers(conn, pid, today).get(code)
            if now is None or now[1] != was[1] or abs((now[2] or 0) - (was[2] or 0)) > 0.005:
                bad.append("%s: repeat customer %s moved %r -> %r" % (label, code, was, now))

    if fingerprint_others(conn, pids) != others_before:
        bad.append("a product OUTSIDE --pids changed")
    return bad


def min_running_balance(conn, pid):
    """Lowest running stock in the order recalculate_product_wacc walks the ledger."""
    run, low = 0, 0
    for (q,) in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? "
            "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
        run += q
        low = min(low, run)
    return low


def _load_engine():
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
        r = conn.execute("SELECT unit_type, cost_price, opening_cost, base_sell_price "
                         "FROM products WHERE id=?", (pid,)).fetchone()
        ratios = dict(conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)).fetchall())
        tiers = conn.execute("SELECT qty_label, price FROM product_price_tiers "
                             "WHERE product_id=? ORDER BY id", (pid,)).fetchall()
        print("  %4d %-30s %-4s stock %-6g cost %-11.6f base %-11.6f COGS %-9.2f %-18s %s"
              % (pid, PLAN[pid]['label'][:30], r[0], eng._stock(conn, pid), r[1], r[3],
                 _cogs(conn, pid), ' '.join('%s=%g' % kv for kv in sorted(ratios.items())),
                 ' '.join('%s@%g' % tuple(t) for t in tiers)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--pids', required=True, help='comma list drawn from %s' % sorted(PLAN))
    ap.add_argument('--mode', required=True, choices=['rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: repeat --pids exactly')
    ap.add_argument('--operator', required=True, help='who is running this (#590: cost writes are signed)')
    ap.add_argument('--reason', required=True, help='why this rebase')
    a = ap.parse_args(argv)

    pids = _parse_pids(a.pids)
    if pids is None:
        print("REFUSED — --pids %r must be a non-empty comma list drawn from %s"
              % (a.pids, sorted(PLAN)))
        return 2
    if a.mode == 'rehearse' and os.path.basename(a.db) == APP_DB_NAME:
        print("REFUSED — rehearse runs on a named COPY; %s is a file the app opens" % a.db)
        return 2
    if a.mode == 'live' and a.confirm_live != a.pids:
        print("REFUSED — live needs --confirm-live %s (repeat --pids exactly)" % a.pids)
        return 2
    if not os.path.isfile(a.db):
        print("REFUSED — no DB at %s" % a.db)
        return 2

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

    today = date.today().isoformat()
    import database
    conn = database.script_connection(__file__, operator=a.operator, reason=a.reason, db_path=a.db)
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
            # 831 has no rows at all, so the engine would have nothing to rescale
            # into โหล = 12 and the replay would have no ratio for its โหล bills
            conn.execute(
                "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,1.0) "
                "ON CONFLICT(product_id, bsn_unit) DO NOTHING", (pid, OLD_UNIT))
            openings[pid] = eng.rebase(conn, pid, NEW_UNIT, RATIO, plan['new_base'],
                                       plan['preserve'], source=SOURCE)
            conn.execute(
                "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                (pid, TIER_LABEL, plan['dozen']))
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
        print("  %4d %-30s opening %g   lowest running %g"
              % (pid, PLAN[pid]['label'][:30], openings[pid], min_running_balance(conn, pid)))
    print("\nREPEAT CUSTOMERS (asked in their own latest bill's unit; must not move)")
    for pid in pids:
        after = _answers(conn, pid, today)
        for code, was in sorted(before[pid]['answers'].items()):
            print("  %4d %-10s %-6s %-18s ฿%-10s -> %-18s ฿%s"
                  % (pid, code, was[0], was[1], was[2], after[code][1], after[code][2]))
        if not before[pid]['answers']:
            print("  %4d (no customer has ever bought this)" % pid)

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
        led = chk.execute("SELECT COALESCE(SUM(quantity_change), 0) FROM transactions "
                          "WHERE product_id=?", (pid,)).fetchone()[0]
        tier = chk.execute("SELECT price FROM product_price_tiers WHERE product_id=? AND qty_label=?",
                           (pid, TIER_LABEL)).fetchone()
        good = (unit == NEW_UNIT and abs(base - plan['new_base']) < 1e-12
                and abs(stock - plan['preserve']) < 1e-9 and abs(led - stock) < 1e-9
                and tier is not None and abs(tier[0] - plan['dozen']) < 1e-9)
        n_bad += not good
        print("  %s %4d %-30s %-4s stock %-6g ledger %-6g base %-11.6f tier %s"
              % ('OK ' if good else 'BAD', pid, plan['label'][:30], unit, stock, led, base,
                 tier[0] if tier else None))
    chk.close()
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
