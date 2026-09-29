"""2026-09-29 — ถุงมือ 1364/1365/1368 onto a `คู่` base, 1 โหล = 12 คู่ (#650).

1364/1365 ran live on 2026-09-29 16:51. 1368 was added later the same day, after
a sweep of the whole glove category found it holding the identical ratio shape;
it is the third and last product to carry it. Run it with `--pids 1368` alone —
the two that are already done would be refused by their own preconditions.

WHY. #650 asked whether the cost per glove was really a cost per PAIR. It is,
and the ratio is wrong too, which the ticket does not say. Three independent
measurements agree that a supplier's `โหล` of these gloves is 12 PAIRS, not 12
pieces:

1. Bills, days apart. 2025-08-06 bought 2 โหล, 2025-08-07 sold 24 คู่.
   2025-08-15 bought 1 โหล, 2025-08-16 sold 12 คู่.
2. The whole history, netted at 12 คู่ per โหล: 1364 bought 20 โหล = 240 คู่ and
   sold 240 คู่, landing on exactly ZERO. Today that same history needs a
   back-solved opening of +276 to reach zero. That plug exists to hide the wrong
   ratio; it is not stock that was ever on a shelf.
3. The 2026 catalogue prints these two per `คู่` (สั้น ฿42, ยาว ฿50), and Sendy's
   own base x `คู่ = 2` already produces exactly those two numbers.

So `โหล = 12` on a piece base means 6 pairs. Consequences on prod today: a
purchase of one dozen posts half of what arrived, cost per unit is double, both
products read as selling below cost (base 25.00 against cost 33.25), and — the
live commercial error — a dozen quotes ฿300 when it should quote ฿600.

⛔ THE #603 ENGINE DOES NOT FIT, do not reach for `--pids`. `rebase()` in
`2026_09_19_gross_to_piece.py` is built for a base that gets SMALLER: it divides
cost by the ratio and sets EVERY existing unit's ratio to that same number. Here
the base gets larger (ตัว -> คู่) and the existing `โหล = 12` is a WRONG value,
not a correctly-denominated one, so nothing scales uniformly. No single ratio
works: 1 keeps the cost right and destroys `โหล`, 12 keeps `โหล` right and
divides the cost by twelve. Its replay and opening logic are reused here; its
`rebase()` is not.

WHAT CHANGES, and what deliberately does not:

    pid   unit_type    base           cost            คู่      โหล   tier
    1364  ตัว -> คู่   25.00 -> 50.00  33.25 (same)   2 -> 1  12    drop `1 คู่`
    1365  ตัว -> คู่   21.00 -> 42.00  28.50 (same)   2 -> 1  12    drop `1 คู่`
    1368  ตัว -> คู่    0.00 (same)    86.09 (same)   2 -> 1  12    none either way

The cost NUMBER does not move; only what it means. 33.25 was always the cost of
one pair, mislabelled as the cost of one glove. `โหล` stays 12 because once the
base is a pair, 12 is correct. The `1 คู่` tier goes because it would then say
the same thing as the base price.

1368 has no price at all, and Put's answer to "fix it or price it" was fix it.
That makes this run cheaper than its two siblings rather than riskier: with the
base staying at 0.00 there is no base change for `_epoch_candidates` to read, so
no repeat customer can move, and the product simply keeps answering ฿0 in every
unit. What it stops being is a trap — today `คู่ = 2` with `โหล = 12` says a
dozen is six pairs, so the day anyone gives this product a price, the dozen
quotes half of it. That is the #650 bug waiting on a price.

Put's rulings, 2026-09-28/29:
  * Base is `คู่`, following sibling 689 ถุงมือยาง S, not `ตัว` with `โหล = 24`.
    Nothing is ever sold as a single glove: every sale in the book is in `คู่`.
  * 1365's shelf is EMPTY, so stock is held at 0 even though the corrected bills
    leave +12 คู่. That takes TWO rows, and the split is not cosmetic. 1365 sells
    12 คู่ on 2024-05-08 that no purchase covers, so an opening of +12 is the
    smallest quantity that makes its own recorded history possible — without it
    the running balance goes negative and recalculate_product_wacc freezes WACC
    for the whole product. The remaining -24 is a labelled reconcile at the TAIL,
    where it reads as what it is rather than as opening stock. 1364 needs neither
    row: its bills balance at zero on their own.
  * A dozen is 12 x the pair price with no further discount (฿600 / ฿504), so no
    `1 โหล` tier is created; the ratio produces it. The catalogue prints no dozen
    price for these two.
  * The `ล้าง orphan ledger 2026-07-03` row on 1364 (-36 in the old unit) is
    DELETED, not rescaled. It and the +276 opening were both computed against the
    broken ratio. With the ratio fixed the bills balance on their own, so 1364
    ends with no adjusting row at all. #603 rescaled the same sweep's row on 1052;
    that keeps a 135-product sweep intact but buys it by inventing an opening the
    bills say never existed. Put chose the plainer history.

The cost ledger is REBUILT by the app's own `recalculate_product_wacc`, never
transformed in place. A blanket multiply would be wrong here in a way that is
easy to miss: purchase legs keep their numbers (12 per โหล under both models)
while sale legs halve (24 ตัว becomes 12 คู่), so no single factor applies.

⚠ `SOURCE` MUST ALREADY BE IN `price_lookup._UNIT_REBASE_SOURCES` ON PROD.
base_sell_price doubles, and `_epoch_candidates` reads a base change as a new
price epoch unless the source is on that list, which would move 13 repeat
customers of 1364 and 10 of 1365 off their last-paid onto list price. The list is
read at QUERY time, so the line must be DEPLOYED BEFORE this runs (#613).

Modes:
  rehearse  commits to a COPY. Refuses a file named inventory.db.
  live      needs --confirm-live repeating --pids, and takes the app's own
            refuse-on-failure backup before the first write.

    python3 scripts/2026_09_29_rebase_gloves_650.py --db /tmp/rehearse-650.db \\
        --pids 1364,1365 --mode rehearse --operator put --reason "#650 rehearsal"
    python3 scripts/2026_09_29_rebase_gloves_650.py --db /data/inventory.db \\
        --pids 1364,1365 --mode live --confirm-live 1364,1365 \\
        --operator put --reason "#650 ถุงมือหนังขุย -> ฐานคู่"
--operator and --reason are required: the cost ledger rebuild is a signed write (#590).
"""
import argparse
import importlib.util
import os
import sqlite3
import sys
from datetime import date

SOURCE = 'script:2026_09_29_rebase_gloves_650'
ENGINE_FILE = '2026_09_19_gross_to_piece.py'
OLD_UNIT = 'ตัว'
NEW_UNIT = 'คู่'
DOZEN_UNIT = 'โหล'
DOZEN_RATIO = 12.0
DROP_TIER = '1 คู่'
ORPHAN_NOTE = 'ล้าง orphan ledger 2026-07-03'
RECONCILE_NOTE = 'ปรับยอดคงเหลือ (แปลงหน่วย 2026-09-29)'
APP_DB_NAME = 'inventory.db'
BACKUP_REASON = 'pre-glove-pair-rebase-650'

# Every figure read off prod (1364/1365 on 2026-09-28, 1368 on 2026-09-29) and
# asserted again as a precondition.
#
# `drop_tier` is per product, not a constant: 1364/1365 each carried a `1 คู่`
# tier that says the same thing as the new base and therefore goes, while 1368
# has no tier at all. A shared constant would have demanded a tier that is not
# there and refused the run.
PLAN = {
    1364: dict(label='ถุงมือหนังขุยยาว', old_base=25.0, pair_price=50.0, cost=33.25,
               preserve=0, drop_orphan=True, drop_tier=True),
    1365: dict(label='ถุงมือหนังขุยสั้น', old_base=21.0, pair_price=42.0, cost=28.50,
               preserve=0, drop_orphan=False, drop_tier=True),
    # 1368 is what #650 left as "ยังไม่ได้ตรวจ". Same ratio shape (`คู่` = 2 on a
    # piece base), same supplier convention, and Put ruled on 2026-09-29 that a
    # glove's base unit is a PAIR for the whole category, so a `โหล` of them is
    # 12 คู่. It differs from its two siblings in three ways that the plan has to
    # carry rather than assume: no tier, no price at all (base stays 0.0, so no
    # price epoch can move), and bills that balance without any adjusting row —
    # 1 โหล in on 2025-03-05, 12 คู่ out on 2025-03-06, and the +12 opening the
    # old sweep back-solved stops being needed once the ratio is right.
    1368: dict(label='ถุงมือหนังแท้ Eagle One 13in', old_base=0.0, pair_price=0.0,
               cost=86.09333333333332, preserve=0, drop_orphan=False, drop_tier=False),
}

# Product-keyed tables and what happens to each. `preconditions` scans
# sqlite_master and refuses if any table outside this dict holds a row for a pid
# being rebased, so a table added later cannot be skipped in silence.
TABLE_STORY = {
    'products': 'unit_type and base_sell_price are rewritten; cost is left alone',
    'stock_levels': 'the mig-080 triggers keep it equal to the ledger; 0 before and after',
    'transactions': 'replayed from the bills; the opening and orphan plugs are dropped',
    'sales_transactions': 'the bills the replay re-derives from (only synced_to_stock moves)',
    'purchase_transactions': 'as sales_transactions',
    'unit_conversions': 'คู่ goes 2 -> 1; โหล stays 12, which is correct once the base is a pair',
    'product_cost_ledger': 'rebuilt by models.wacc.recalculate_product_wacc, never transformed',
    'product_price_tiers': 'the redundant `1 คู่` tier is removed',
    'product_price_history': 'append-only audit log, stamped with SOURCE',
    'product_locations': 'a shelf code, no quantity',
    'legacy_product_sku_map': 'an old SKU number, no quantity or unit',
    'product_code_mapping': 'bsn_code -> product, no ratio',
    'platform_skus': 'required empty',
    'ecommerce_listings': 'required empty',
    'conversion_formula_inputs': 'required empty',
    'promotions': 'required empty',
    'migration_186_uc_deleted': 'audit trail of rows migration 186 removed; history, read by '
                                'nothing that prices or counts. 2 rows here.',
}
MUST_BE_EMPTY = (
    ('platform_skus', 'internal_product_id'),
    ('ecommerce_listings', 'product_id'),
    ('conversion_formula_inputs', 'product_id'),
    ('promotions', 'product_id'),
)
PRODUCT_COLS = ('product_id', 'internal_product_id')


def _money(x):
    return round(x + 0.0, 2)


def _stock(conn, pid):
    r = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return (r[0] if r else 0) or 0


def _ledger_sum(conn, pid):
    return conn.execute("SELECT COALESCE(SUM(quantity_change), 0) FROM transactions "
                        "WHERE product_id=?", (pid,)).fetchone()[0]


def min_running_balance(conn, pid):
    run, low = 0, 0
    for (q,) in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? "
            "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
        run += q
        low = min(low, run)
    return low


def preconditions(conn, eng, pids):
    bad = []
    import price_lookup
    if SOURCE not in price_lookup._UNIT_REBASE_SOURCES:
        bad.append(
            "%s is NOT in price_lookup._UNIT_REBASE_SOURCES on this deployment. Ship that one "
            "line first, or 23 repeat customers of these two get quoted list price (#613)." % SOURCE)

    for pid in pids:
        plan = PLAN[pid]
        label = '%s %s' % (pid, plan['label'])
        row = conn.execute("SELECT unit_type, base_sell_price, cost_price FROM products WHERE id=?",
                           (pid,)).fetchone()
        if row is None:
            bad.append("%s: product does not exist — wrong DB?" % label)
            continue
        if row[0] != OLD_UNIT:
            bad.append("%s: unit_type is %r, expected %r (already rebased?)" % (label, row[0], OLD_UNIT))
        if _money(row[1]) != _money(plan['old_base']):
            bad.append("%s: base_sell_price %r, plan says %r" % (label, row[1], plan['old_base']))
        if _money(row[2]) != _money(plan['cost']):
            bad.append("%s: cost_price %r, plan says %r" % (label, row[2], plan['cost']))

        if _money(_stock(conn, pid)) != 0:
            bad.append("%s: stock is %r, this plan is written for 0" % (label, _stock(conn, pid)))
        if _money(_ledger_sum(conn, pid)) != _money(_stock(conn, pid)):
            bad.append("%s: ledger %r != stock %r before we start"
                       % (label, _ledger_sum(conn, pid), _stock(conn, pid)))

        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        if ratios != {NEW_UNIT: 2.0, DOZEN_UNIT: DOZEN_RATIO}:
            bad.append("%s: unit_conversions %r, expected {%r: 2.0, %r: %s}"
                       % (label, ratios, NEW_UNIT, DOZEN_UNIT, DOZEN_RATIO))

        tiers = [tuple(t) for t in conn.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=? ORDER BY id", (pid,))]
        if plan['drop_tier']:
            if len(tiers) != 1 or tiers[0][0] != DROP_TIER or _money(tiers[0][1]) != _money(plan['pair_price']):
                bad.append("%s: tiers %r, expected exactly [(%r, %r)]"
                           % (label, tiers, DROP_TIER, plan['pair_price']))
        elif tiers:
            bad.append("%s: tiers %r, plan expects none — a tier would answer the "
                       "dozen instead of the ratio, which is the thing being fixed" % (label, tiers))

        # the replay reads each bill's own unit; anything else and the ratio it
        # would be converted at is not the one this plan reasoned about
        for table, want in (('sales_transactions', NEW_UNIT), ('purchase_transactions', DOZEN_UNIT)):
            for (u, n) in conn.execute(
                    "SELECT unit, COUNT(*) FROM %s WHERE product_id=? GROUP BY unit" % table, (pid,)):
                if u != want:
                    bad.append("%s: %d %s row(s) in unit %r, expected only %r" % (label, n, table, u, want))

        n_orphan = conn.execute("SELECT COUNT(*) FROM transactions WHERE product_id=? AND note=?",
                                (pid, ORPHAN_NOTE)).fetchone()[0]
        if bool(n_orphan) != plan['drop_orphan']:
            bad.append("%s: %d orphan-cleanup row(s), plan expects %s"
                       % (label, n_orphan, 'one' if plan['drop_orphan'] else 'none'))

        for table, col in MUST_BE_EMPTY:
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col), (pid,)).fetchone()[0]
            if n:
                bad.append("%s: %s holds %d row(s); it has no conversion story here" % (label, table, n))

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
    """The app's own COGS expression (/accounting) over the whole history."""
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
    """{customer_code: (unit, basis, price)} asked in each customer's own latest bill unit."""
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


def snapshot(conn, pids, today):
    before = {}
    for pid in pids:
        p = conn.execute("SELECT unit_type, base_sell_price, cost_price, opening_cost, "
                         "low_stock_threshold FROM products WHERE id=?", (pid,)).fetchone()
        before[pid] = {
            'unit_type': p[0], 'base': p[1], 'cost': p[2], 'opening_cost': p[3], 'threshold': p[4],
            'stock': _stock(conn, pid), 'cogs': _cogs(conn, pid),
            'epoch': _epoch(conn, pid, today), 'answers': _answers(conn, pid, today),
            'bill_ids': ({r[0] for r in conn.execute(
                              "SELECT id FROM sales_transactions WHERE product_id=?", (pid,))},
                         {r[0] for r in conn.execute(
                              "SELECT id FROM purchase_transactions WHERE product_id=?", (pid,))}),
        }
    return before


def fingerprint_others(conn, pids):
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
        q("SELECT COUNT(*), COALESCE(SUM(id), 0) FROM product_cost_ledger "
          "WHERE product_id NOT IN (%s)" % marks),
    )


def rebase_one(conn, eng, pid, plan):
    """Returns the reconciling quantity the corrected ledger needed (0 is the good answer)."""
    from models import bsn_sync
    from models._shared import _set_price_change_source

    # Per product, NOT once around the loop: recalculate_product_wacc sets the
    # source to 'wac-sync' for its own writes (models/wacc.py), so the first
    # product's rebuild leaves the next product's base change unattributed — and
    # an unattributed base change is exactly what #613's epoch reads.
    _set_price_change_source(conn, SOURCE)
    conn.execute("UPDATE products SET unit_type=?, base_sell_price=? WHERE id=?",
                 (NEW_UNIT, plan['pair_price'], pid))
    cur = conn.execute("UPDATE unit_conversions SET ratio=1.0 WHERE product_id=? AND bsn_unit=?",
                       (pid, NEW_UNIT))
    if cur.rowcount != 1:
        raise RuntimeError("pid %s: %s ratio update touched %s rows" % (pid, NEW_UNIT, cur.rowcount))
    if plan['drop_tier']:
        cur = conn.execute("DELETE FROM product_price_tiers WHERE product_id=? AND qty_label=?",
                           (pid, DROP_TIER))
        if cur.rowcount != 1:
            raise RuntimeError("pid %s: dropping the %r tier touched %s rows"
                               % (pid, DROP_TIER, cur.rowcount))

    # replay, exactly as bsn_sync.update_unit_conversion_ratio does
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
        raise RuntimeError("pid %s: replay lost %d movement(s) — %r" % (pid, len(lost), sorted(lost)[:5]))

    # both plugs were computed against the broken ratio; neither survives it
    conn.execute("DELETE FROM transactions WHERE product_id=? AND note IN (?,?,?)",
                 (pid, eng.OPENING_NOTE, eng.RECONCILE_NOTE, ORPHAN_NOTE))

    # Two rows at most, each with a job the other cannot do.
    #
    # The OPENING is the smallest quantity that makes the recorded history
    # possible: 1365 sells 12 คู่ on 2024-05-08 before any purchase covers them,
    # so with no opening the running balance goes negative and
    # recalculate_product_wacc takes its "negative stock — freeze WACC" branch,
    # after which WACC is no longer a weighted average of anything. It is derived
    # from the bills, never back-solved to hit a target.
    #
    # The RECONCILE at the tail then carries whatever is left over to the stock
    # Put confirmed on the shelf. Putting that difference in the opening instead
    # would dress it as history the bills do not support.
    def _running_min():
        run, low = 0, 0
        for (q,) in conn.execute(
                "SELECT quantity_change FROM transactions WHERE product_id=? "
                "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
            run += q
            low = min(low, run)
        return low

    head = conn.execute("SELECT MIN(created_at) FROM transactions WHERE product_id=?",
                        (pid,)).fetchone()[0]
    opening = max(0.0, -_running_min())
    if opening:
        stamp = (conn.execute("SELECT datetime(?, '-1 second')", (head,)).fetchone()[0]
                 if head else eng.OPENING_STAMP)
        conn.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
                     " note, created_at) VALUES (?, 'ADJUST', ?, 'unit', ?, ?)",
                     (pid, opening, eng.OPENING_NOTE, stamp))
    needed = plan['preserve'] - _ledger_sum(conn, pid)
    if abs(needed) > 1e-9:
        tail = conn.execute("SELECT MAX(created_at) FROM transactions WHERE product_id=?",
                            (pid,)).fetchone()[0]
        conn.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
                     " note, created_at) VALUES (?, 'ADJUST', ?, 'unit', ?, ?)",
                     (pid, needed, RECONCILE_NOTE, tail or eng.OPENING_STAMP))

    # the cost ledger is rebuilt from the corrected ledger, never transformed
    from models import recalculate_product_wacc
    recalculate_product_wacc(pid, conn, operation='ratio_replay')
    return {'opening': opening, 'reconcile': needed}


def assert_invariants(conn, pids, before, others_before, needed, today):
    bad = []
    for pid in pids:
        plan, b = PLAN[pid], before[pid]
        label = '%s %s' % (pid, plan['label'])
        row = conn.execute("SELECT unit_type, base_sell_price, cost_price, low_stock_threshold "
                           "FROM products WHERE id=?", (pid,)).fetchone()
        if row[0] != NEW_UNIT:
            bad.append("%s: unit_type %r != %r" % (label, row[0], NEW_UNIT))
        if _money(row[1]) != _money(plan['pair_price']):
            bad.append("%s: base %r != %r" % (label, row[1], plan['pair_price']))
        # a PREDICTION, not a setting: WACC is rebuilt from the bills, and the
        # bills only give back 33.25 if one โหล really is twelve pairs
        if _money(row[2]) != _money(plan['cost']):
            bad.append("%s: cost rebuilt to %r, expected %r — the ratio story does not hold"
                       % (label, row[2], plan['cost']))
        if row[3] != b['threshold']:
            bad.append("%s: low_stock_threshold %r != %r" % (label, row[3], b['threshold']))

        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        if ratios != {NEW_UNIT: 1.0, DOZEN_UNIT: DOZEN_RATIO}:
            bad.append("%s: unit_conversions %r != {%r: 1.0, %r: %s}"
                       % (label, ratios, NEW_UNIT, DOZEN_UNIT, DOZEN_RATIO))
        tiers = [tuple(t) for t in conn.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=?", (pid,))]
        if tiers:
            bad.append("%s: tiers %r, expected none (the base price says it)" % (label, tiers))

        stock, ledger = _stock(conn, pid), _ledger_sum(conn, pid)
        if _money(stock) != _money(plan['preserve']):
            bad.append("%s: stock %r != %r" % (label, stock, plan['preserve']))
        if _money(ledger) != _money(stock):
            bad.append("%s: ledger %r != stock_levels %r" % (label, ledger, stock))
        if min_running_balance(conn, pid) < -1e-9:
            bad.append("%s: running stock goes negative — WACC would freeze" % label)
        if conn.execute("SELECT COUNT(*) FROM transactions WHERE product_id=? AND note=?",
                        (pid, ORPHAN_NOTE)).fetchone()[0]:
            bad.append("%s: the orphan-cleanup row survived; Put ruled it is dropped" % label)

        # ⚠ COGS MUST move here, and that is the point. This is not a pure
        # redenomination like #649: today every sale of 12 คู่ costs the books
        # 24 x 33.25 because the sale posts twice the pairs it moved. So the
        # oracle is not "unchanged", it is an independently computed figure —
        # pairs actually sold times the cost of one pair — which the rebuilt
        # ledger has to land on.
        pairs_sold = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM sales_transactions WHERE product_id=? AND unit=?",
            (pid, NEW_UNIT)).fetchone()[0]
        want_cogs = pairs_sold * plan['cost']
        cogs = _cogs(conn, pid)
        if abs(cogs - want_cogs) > 0.005:
            bad.append("%s: COGS %.4f, expected %g คู่ x %g = %.4f"
                       % (label, cogs, pairs_sold, plan['cost'], want_cogs))
        if abs(b['cogs'] - want_cogs * 2) > 0.005:
            bad.append("%s: COGS was %.4f, expected exactly double (%.4f) before the fix"
                       % (label, b['cogs'], want_cogs * 2))

        now_ids = ({r[0] for r in conn.execute(
                        "SELECT id FROM sales_transactions WHERE product_id=?", (pid,))},
                   {r[0] for r in conn.execute(
                        "SELECT id FROM purchase_transactions WHERE product_id=?", (pid,))})
        if now_ids != b['bill_ids']:
            bad.append("%s: the bill set changed" % label)

        epoch = _epoch(conn, pid, today)
        if epoch != b['epoch']:
            bad.append("%s: base-price epoch moved %r -> %r — is SOURCE in "
                       "price_lookup._UNIT_REBASE_SOURCES? (#613)" % (label, b['epoch'], epoch))
        after = _answers(conn, pid, today)
        for code, was in sorted(b['answers'].items()):
            now = after.get(code)
            if now is None or now[1] != was[1] or abs((now[2] or 0) - (was[2] or 0)) > 0.005:
                bad.append("%s: repeat customer %s moved %r -> %r" % (label, code, was, now))

        # the whole point: a dozen must stop quoting half price
        import price_lookup
        dozen = price_lookup.resolve_price(conn, product_id=pid, unit=DOZEN_UNIT, qty=1)
        want = plan['pair_price'] * DOZEN_RATIO
        if abs(dozen['list']['list_for_unit'] - want) > 0.005:
            bad.append("%s: a %s lists at %r, expected %r"
                       % (label, DOZEN_UNIT, dozen['list']['list_for_unit'], want))

    if fingerprint_others(conn, pids) != others_before:
        bad.append("a product OUTSIDE --pids changed")
    return bad


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


def _print_table(conn, pids, title):
    import price_lookup
    print(title)
    for pid in pids:
        r = conn.execute("SELECT unit_type, cost_price, base_sell_price FROM products WHERE id=?",
                         (pid,)).fetchone()
        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        tiers = [tuple(t) for t in conn.execute(
            "SELECT qty_label, price FROM product_price_tiers WHERE product_id=?", (pid,))]
        try:
            dozen = '%.2f' % price_lookup.resolve_price(
                conn, product_id=pid, unit=DOZEN_UNIT, qty=1)['list']['list_for_unit']
        except Exception as exc:
            dozen = type(exc).__name__
        print("  %4d %-22s %-4s stock %-4g cost %-9.4f base %-8.2f โหล lists %-9s %-16s %s"
              % (pid, PLAN[pid]['label'], r[0], _stock(conn, pid), r[1], r[2], dozen,
                 ' '.join('%s=%g' % kv for kv in sorted(ratios.items())),
                 ' '.join('%s@%g' % t for t in tiers) or '(no tier)'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--pids', required=True, help='comma list drawn from %s' % sorted(PLAN))
    ap.add_argument('--mode', required=True, choices=['rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: repeat --pids exactly')
    ap.add_argument('--operator', required=True, help='who is running this (#590: signed cost writes)')
    ap.add_argument('--reason', required=True, help='why this rebase')
    a = ap.parse_args(argv)

    pids = _parse_pids(a.pids)
    if pids is None:
        print("REFUSED — --pids %r must be a non-empty comma list drawn from %s" % (a.pids, sorted(PLAN)))
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
            print("BACKUP", db_backup.guarded_backup(BACKUP_REASON, policy='refuse', db_path=a.db,
                                                     backup_dir=db_backup.default_backup_dir(a.db)))

        before = snapshot(conn, pids, today)
        others_before = fingerprint_others(conn, pids)
        _print_table(conn, pids, "BEFORE")

        # the source is set per product INSIDE rebase_one, because the WACC
        # rebuild overwrites it; cleared once here when every product is done
        rows = {pid: rebase_one(conn, eng, pid, PLAN[pid]) for pid in pids}
        from models._shared import _set_price_change_source
        _set_price_change_source(conn, None)

        bad = assert_invariants(conn, pids, before, others_before, rows, today)
    except Exception:
        conn.rollback()
        raise

    if bad:
        conn.rollback()
        print("\nROLLED BACK — invariants failed:")
        for p in bad:
            print("  ✗", p)
        return 1

    _print_table(conn, pids, "\nAFTER (in-transaction)")
    print("\nADJUSTING ROWS the corrected ledger needed (both 0 = the bills balance on their own)")
    for pid in pids:
        print("  %4d %-22s opening %+g คู่  reconcile %+g คู่   lowest running %g"
              % (pid, PLAN[pid]['label'], rows[pid]['opening'], rows[pid]['reconcile'],
                 min_running_balance(conn, pid)))
    print("\nREPEAT CUSTOMERS (asked in their own latest bill's unit; must not move)")
    for pid in pids:
        after = _answers(conn, pid, today)
        for code, was in sorted(before[pid]['answers'].items()):
            print("  %4d %-10s %-5s %-18s ฿%-9s -> %-18s ฿%s"
                  % (pid, code, was[0], was[1], was[2], after[code][1], after[code][2]))

    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    print("\nCOMMITTED (%s) — re-read on a new connection:" % a.mode)
    n_bad = 0
    for pid in pids:
        plan = PLAN[pid]
        unit, base, cost, stock = chk.execute(
            "SELECT p.unit_type, p.base_sell_price, p.cost_price, s.quantity FROM products p "
            "JOIN stock_levels s ON s.product_id = p.id WHERE p.id=?", (pid,)).fetchone()
        led = chk.execute("SELECT COALESCE(SUM(quantity_change), 0) FROM transactions "
                          "WHERE product_id=?", (pid,)).fetchone()[0]
        good = (unit == NEW_UNIT and _money(base) == _money(plan['pair_price'])
                and _money(cost) == _money(plan['cost'])
                and _money(stock) == _money(plan['preserve']) and _money(led) == _money(stock))
        n_bad += not good
        print("  %s %4d %-22s %-4s base %-8.2f cost %-9.4f stock %-4g ledger %g"
              % ('OK ' if good else 'BAD', pid, plan['label'], unit, base, cost, stock, led))
    chk.close()
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
