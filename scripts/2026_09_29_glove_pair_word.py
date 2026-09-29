"""2026-09-29 — ถุงมือ 8 ตัวรับคำว่า `คู่`, และ 1362 ได้ราคาตั้งครั้งแรก.

WHY. A sweep of the whole glove category (15 products, prod read-only, report in
`Operations/05_analysis-reports/product/glove_unit_audit_2026-09-29.md`) found
that eight of them hold `unit_type = 'ตัว'` with no `คู่` row at all, so the
resolver RAISES on the one word a customer is most likely to use for a glove.
Nothing about their money is wrong: every one of their bills is in `โหล`, and
their dozen price comes either from a `1 โหล` tier or from base x 12, both of
which already match the printed book.

⭐ THE RULING THIS RESTS ON, and why it had to be a ruling. Put, 2026-09-29:
**ถุงมือนับเป็นคู่เสมอ — a glove's base unit is a PAIR, so one `โหล` of any
glove is 12 คู่.** That cannot be derived from this database, and the reason is
worth writing down because the next person will hit it: Express has its OWN word
for a dozen pairs, `โหลคู่` (code `หค`), which is ratio **24** in all 17
`unit_conversions` rows that use it and appears on 292 real bills, mostly บานพับ
and สายยูหูช้าง — and **three products carry both `โหล` = 12 and `โหลคู่` = 24 at
once** (714, 716, 1607). So the house convention is precise: `โหล` means twelve
of the BASE unit. A glove bill keyed `โหล` therefore means twelve pieces
everywhere else in the book; for gloves it means twelve pairs, because the base
unit is the pair. Do not "correct" that back.

WHAT CHANGES. Eight products gain one row each:

    unit_conversions += (pid, 'คู่', 1.0)   for 1358 1359 1360 1361 1362 1363 1366 1367

Ratio 1, because `ตัว` on these products already denominates a pair. That is the
whole change for seven of them: no price moves, no tier moves, no ledger row is
written, and stock cannot move because every bill they carry is in `โหล`, a unit
this script does not touch.

The eighth, 1362 ถุงมือผ้าขอบเหลืองหนา 700g, also gets its first list price:

    base_sell_price  0.00 -> 6.42        product_price_tiers += ('1 โหล', 77.00)

฿77/โหล is what the 2026 book prints for ถุงมือผ้า COTTON 7 ขีด, and 6.42 is
77/12 rounded the way its three siblings already store theirs (1359 = 5.17 for a
฿62 dozen, 1360 = 5.58 for ฿67, 1361 = 6.42 for ฿77). The tier carries the exact
printed dozen so the ฿0.04 rounding drift never reaches a quote.

⚠ NOBODY'S PRICE EPOCH MOVES HERE, INCLUDING 1362's, and that is a ruling the
app already holds rather than something this script arranges. I expected the
opposite and the rehearsal said no: under #555 (Put, 2026-09-16) `_epoch_candidates`
skips a `product_price_history` row whose `old_value` is NULL or 0, because Sendy
learning a price for the first time is not a price CHANGE — and `_tier_epoch`
likewise ignores a tier's own first-ever INSERT. 1362 goes 0.00 -> 6.42 with its
first tier, so both new rows are exactly the shape #555 excludes. The ten
customers who have bought it keep their last-paid answers.

That also means this script has no business in `price_lookup._UNIT_REBASE_SOURCES`
(#613): it is not re-denominating anything, and it needs no exemption, because
the epoch rule already gets this case right on its own.

Modes:
  rehearse  commits to a COPY. Refuses a file named inventory.db.
  live      needs --confirm-live repeating --pids, and takes the app's own
            refuse-on-failure backup before the first write.

    python3 scripts/2026_09_29_glove_pair_word.py --db /tmp/rehearse-pair.db \\
        --pids 1358,1359,1360,1361,1362,1363,1366,1367 --mode rehearse \\
        --operator put --reason "glove pair word rehearsal"
"""
import argparse
import os
import sqlite3
import sys
from datetime import date

SOURCE = 'script:2026_09_29_glove_pair_word'
OLD_UNIT = 'ตัว'
PAIR_UNIT = 'คู่'
DOZEN_UNIT = 'โหล'
DOZEN_RATIO = 12.0
APP_DB_NAME = 'inventory.db'
BACKUP_REASON = 'pre-glove-pair-word'

# Read off prod 2026-09-29 and asserted again as a precondition. `dozen_lists` is
# what the resolver answers for `โหล` today and must still answer afterwards —
# for 1362 it is the one number in this table that is allowed to change.
PLAN = {
    1358: dict(label='ถุงมือกรีดยาง', base=0.0, tiers=[], dozen_lists=0.0),
    1359: dict(label='ถุงมือผ้าขอบแดง 400g', base=5.17, tiers=[('1 โหล', 62.0)], dozen_lists=62.0),
    1360: dict(label='ถุงมือผ้าขอบเขียว 500g', base=5.58, tiers=[('1 โหล', 67.0)], dozen_lists=67.0),
    1361: dict(label='ถุงมือผ้า 700g สีเทา', base=6.42, tiers=[('1 โหล', 77.0)], dozen_lists=77.0),
    1362: dict(label='ถุงมือผ้าขอบเหลืองหนา 700g', base=0.0, tiers=[], dozen_lists=0.0),
    1363: dict(label='ถุงมือยางหนา Tiger Tex สีดำ', base=48.33, tiers=[('1 โหล', 580.0)], dozen_lists=580.0),
    1366: dict(label='ถุงมือหนังเฟอร์นิเจอร์ยาว', base=50.0, tiers=[('1 โหล', 600.0)], dozen_lists=600.0),
    1367: dict(label='ถุงมือหนังเฟอร์นิเจอร์สั้น', base=43.33, tiers=[('1 โหล', 520.0)], dozen_lists=520.0),
}

# The one product that is also being priced. Kept separate from PLAN so the
# "nothing else moves" invariant can be written as "every pid except this one".
PRICED = 1362
NEW_BASE = 6.42
NEW_TIER = ('1 โหล', 77.0)

# Product-keyed tables and what happens to each. `preconditions` scans
# sqlite_master and refuses if any table outside this dict holds a row for one of
# these products, so a table added to the schema later cannot be skipped in silence.
TABLE_STORY = {
    'products': 'untouched, except base_sell_price on %d' % PRICED,
    'unit_conversions': 'one `คู่` = 1 row is added per product; nothing existing is altered',
    'product_price_tiers': 'untouched, except the `1 โหล` row added to %d' % PRICED,
    'product_price_history': 'append-only audit log; only %d writes one, stamped with SOURCE' % PRICED,
    'stock_levels': 'must not move — every bill is in โหล, a unit this script does not touch',
    'transactions': 'must not move, same reason',
    'sales_transactions': 'read only, to prove every bill is in โหล',
    'purchase_transactions': 'read only, same',
    'product_cost_ledger': 'untouched; no cost is written and no replay happens',
    'product_locations': 'a shelf code, no quantity or unit',
    'product_code_mapping': 'bsn_code -> product, no ratio',
    'legacy_product_sku_map': 'an old SKU number, no quantity or unit',
    'migration_186_uc_deleted': 'audit trail of rows migration 186 removed; read by nothing '
                                'that prices or counts',
    'platform_skus': 'required empty',
    'ecommerce_listings': 'required empty',
    'conversion_formula_inputs': 'required empty',
    'promotions': 'required empty — a promo would make the price answers move for other reasons',
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


def _ledger(conn, pid):
    """(row count, summed movement, summed id) — any ledger write changes one of these."""
    return tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(quantity_change), 0), COALESCE(SUM(id), 0) "
        "FROM transactions WHERE product_id=?", (pid,)).fetchone())


def _tiers(conn, pid):
    return [(r[0], _money(r[1])) for r in conn.execute(
        "SELECT qty_label, price FROM product_price_tiers WHERE product_id=? ORDER BY qty_label", (pid,))]


def _ask(conn, pid, unit, today):
    """(list price, source) for one unit, or None if the resolver refuses the word."""
    import price_lookup
    try:
        o = price_lookup.resolve_price(conn, product_id=pid, unit=unit, qty=1, today=today)
    except ValueError:
        return None
    return (_money(o['list']['list_for_unit']), o['list']['list_source'])


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


def preconditions(conn, pids, today):
    bad = []
    for pid in pids:
        plan = PLAN[pid]
        label = '%s %s' % (pid, plan['label'])
        n_before = len(bad)
        row = conn.execute("SELECT unit_type, base_sell_price, is_active FROM products WHERE id=?",
                           (pid,)).fetchone()
        if row is None:
            bad.append("%s: product does not exist — wrong DB?" % label)
            continue
        if row[0] != OLD_UNIT:
            bad.append("%s: unit_type is %r, expected %r" % (label, row[0], OLD_UNIT))
        if _money(row[1]) != _money(plan['base']):
            bad.append("%s: base_sell_price %r, plan says %r" % (label, row[1], plan['base']))
        if not row[2]:
            bad.append("%s: product is inactive; teaching it a word is not the fix" % label)

        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        if ratios != {DOZEN_UNIT: DOZEN_RATIO}:
            bad.append("%s: unit_conversions %r, expected exactly {%r: %s} — a `%s` row already "
                       "exists, or the dozen is not 12" % (label, ratios, DOZEN_UNIT, DOZEN_RATIO, PAIR_UNIT))
        if _tiers(conn, pid) != [(t[0], _money(t[1])) for t in plan['tiers']]:
            bad.append("%s: tiers %r, plan says %r" % (label, _tiers(conn, pid), plan['tiers']))

        # Ask the resolver only once this product's shape is the one the plan
        # describes. A product whose rows are already wrong has nothing to learn
        # from what the resolver makes of them, and asking anyway would report
        # the same fault twice in two vocabularies.
        if len(bad) == n_before:
            # The claim being acted on is that `ตัว` already denominates a pair.
            # If `คู่` answers today, either that is already recorded or it means
            # something else here, and adding a ratio-1 row would be writing over
            # an answer rather than supplying a missing one.
            if _ask(conn, pid, PAIR_UNIT, today) is not None:
                bad.append("%s: `%s` already resolves; this script only supplies a MISSING word"
                           % (label, PAIR_UNIT))
            dozen = _ask(conn, pid, DOZEN_UNIT, today)
            if dozen is None or dozen[0] != _money(plan['dozen_lists']):
                bad.append("%s: a %s lists %r, plan says %r"
                           % (label, DOZEN_UNIT, dozen, plan['dozen_lists']))

        # Stock cannot move here, and these two make that structural rather than
        # hopeful: a bill in some other unit could be re-read at a new ratio, and
        # an unsynced bill is one a later re-sync would post.
        for table in ('sales_transactions', 'purchase_transactions'):
            for (u, n) in conn.execute(
                    "SELECT unit, COUNT(*) FROM %s WHERE product_id=? GROUP BY unit" % table, (pid,)):
                if u != DOZEN_UNIT:
                    bad.append("%s: %d %s row(s) in unit %r, expected only %r"
                               % (label, n, table, u, DOZEN_UNIT))
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=? AND synced_to_stock=0"
                             % table, (pid,)).fetchone()[0]
            if n:
                bad.append("%s: %d unsynced %s row(s); a re-sync would post them at the new ratio"
                           % (label, n, table))
        if _money(_stock(conn, pid)) != _money(_ledger(conn, pid)[1]):
            bad.append("%s: stock %r != ledger %r before we start"
                       % (label, _stock(conn, pid), _ledger(conn, pid)[1]))

        for table, col in MUST_BE_EMPTY:
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col), (pid,)).fetchone()[0]
            if n:
                bad.append("%s: %s holds %d row(s); it has no story here" % (label, table, n))

    if PRICED in pids:
        if _money(PLAN[PRICED]['base']) != 0.0:
            bad.append("%d: the plan prices a product that already has a base price" % PRICED)
        if PLAN[PRICED]['tiers']:
            bad.append("%d: the plan adds a tier to a product that already has one" % PRICED)

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
                           "— decide what happens to it first" % (table, n))
    return bad


def snapshot(conn, pids, today):
    before = {}
    for pid in pids:
        before[pid] = {
            'base': conn.execute("SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0],
            'tiers': _tiers(conn, pid), 'stock': _stock(conn, pid), 'ledger': _ledger(conn, pid),
            'dozen': _ask(conn, pid, DOZEN_UNIT, today), 'piece': _ask(conn, pid, OLD_UNIT, today),
            'epoch': _epoch(conn, pid, today), 'answers': _answers(conn, pid, today),
        }
    return before


def fingerprint_others(conn, pids):
    marks = ','.join('?' * len(pids))
    q = lambda sql: conn.execute(sql, pids).fetchall()
    return (
        q("SELECT p.id, p.unit_type, p.cost_price, p.base_sell_price, COALESCE(s.quantity, 0) "
          "FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id "
          "WHERE p.id NOT IN (%s) ORDER BY p.id" % marks),
        q("SELECT product_id, bsn_unit, ratio FROM unit_conversions WHERE product_id NOT IN (%s) "
          "ORDER BY product_id, bsn_unit" % marks),
        q("SELECT id, product_id, qty_label, price FROM product_price_tiers "
          "WHERE product_id NOT IN (%s) ORDER BY id" % marks),
        q("SELECT COUNT(*), COALESCE(SUM(quantity_change), 0), COALESCE(SUM(id), 0) "
          "FROM transactions WHERE product_id NOT IN (%s)" % marks),
    )


def apply_one(conn, pid):
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                 (pid, PAIR_UNIT, 1.0))
    if pid != PRICED:
        return
    from models._shared import _set_price_change_source
    _set_price_change_source(conn, SOURCE)
    conn.execute("UPDATE products SET base_sell_price=? WHERE id=?", (NEW_BASE, pid))
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                 (pid, NEW_TIER[0], NEW_TIER[1]))
    _set_price_change_source(conn, None)


def assert_invariants(conn, pids, before, others_before, today):
    bad = []
    for pid in pids:
        plan, b = PLAN[pid], before[pid]
        label = '%s %s' % (pid, plan['label'])

        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        if ratios != {DOZEN_UNIT: DOZEN_RATIO, PAIR_UNIT: 1.0}:
            bad.append("%s: unit_conversions %r != {%r: %s, %r: 1.0}"
                       % (label, ratios, DOZEN_UNIT, DOZEN_RATIO, PAIR_UNIT))

        # the point of the whole script: the word answers, at the piece price,
        # because on these products a piece IS a pair
        pair = _ask(conn, pid, PAIR_UNIT, today)
        piece = _ask(conn, pid, OLD_UNIT, today)
        if pair is None:
            bad.append("%s: `%s` still raises" % (label, PAIR_UNIT))
        elif pair != piece:
            bad.append("%s: `%s` lists %r but `%s` lists %r — they must be the same price"
                       % (label, PAIR_UNIT, pair, OLD_UNIT, piece))

        # nothing that counts or costs may move, for any of them
        if _money(_stock(conn, pid)) != _money(b['stock']):
            bad.append("%s: stock %r -> %r" % (label, b['stock'], _stock(conn, pid)))
        if _ledger(conn, pid) != b['ledger']:
            bad.append("%s: the stock ledger changed %r -> %r" % (label, b['ledger'], _ledger(conn, pid)))

        # #555 (Put, 2026-09-16): a `product_price_history` row whose old_value is
        # NULL or 0 is Sendy LEARNING a price, not a price change, and a tier's
        # own first INSERT is excluded the same way. 1362's two new rows are both
        # exactly that shape, so no epoch may move on any product here — the one
        # being priced included. Asserted for all eight, not just the seven.
        if _epoch(conn, pid, today) != b['epoch']:
            bad.append("%s: price epoch moved %r -> %r; neither a unit word nor a FIRST list "
                       "price is a price change (#555)" % (label, b['epoch'], _epoch(conn, pid, today)))
        # A customer holding a real bill inside the window is answered from that
        # bill, before any list price is consulted, so nothing here can move them.
        after = _answers(conn, pid, today)
        for code, was in sorted(b['answers'].items()):
            now = after.get(code)
            if was[1] == 'last_paid' and (now is None or now[1] != was[1]
                                          or abs((now[2] or 0) - (was[2] or 0)) > 0.005):
                bad.append("%s: repeat customer %s was on last_paid %r and moved to %r"
                           % (label, code, was, now))

        base = conn.execute("SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        if pid == PRICED:
            if _money(base) != _money(NEW_BASE):
                bad.append("%s: base %r != %r" % (label, base, NEW_BASE))
            if _tiers(conn, pid) != [(NEW_TIER[0], _money(NEW_TIER[1]))]:
                bad.append("%s: tiers %r != [%r]" % (label, _tiers(conn, pid), NEW_TIER))
            dozen = _ask(conn, pid, DOZEN_UNIT, today)
            if dozen is None or dozen[0] != _money(NEW_TIER[1]):
                bad.append("%s: a %s lists %r, expected the printed %r"
                           % (label, DOZEN_UNIT, dozen, NEW_TIER[1]))
        else:
            if _money(base) != _money(b['base']):
                bad.append("%s: base moved %r -> %r; only %d is being priced"
                           % (label, b['base'], base, PRICED))
            if _tiers(conn, pid) != b['tiers']:
                bad.append("%s: tiers moved %r -> %r" % (label, b['tiers'], _tiers(conn, pid)))
            if _ask(conn, pid, DOZEN_UNIT, today) != b['dozen']:
                bad.append("%s: a %s listed %r, now %r"
                           % (label, DOZEN_UNIT, b['dozen'], _ask(conn, pid, DOZEN_UNIT, today)))
            for code, was in sorted(b['answers'].items()):
                now = after.get(code)
                if now is None or now[1] != was[1] or abs((now[2] or 0) - (was[2] or 0)) > 0.005:
                    bad.append("%s: repeat customer %s moved %r -> %r" % (label, code, was, now))

    if fingerprint_others(conn, pids) != others_before:
        bad.append("a product OUTSIDE --pids changed")
    return bad


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


def _print_table(conn, pids, title, today):
    print(title)
    for pid in pids:
        base = conn.execute("SELECT base_sell_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)).fetchall())
        print("  %4d %-26s base %-7.2f stock %-5g %-18s ตัว %-16s คู่ %-16s โหล %-16s %s"
              % (pid, PLAN[pid]['label'], base, _stock(conn, pid),
                 ' '.join('%s=%g' % kv for kv in sorted(ratios.items())),
                 _ask(conn, pid, OLD_UNIT, today), _ask(conn, pid, PAIR_UNIT, today),
                 _ask(conn, pid, DOZEN_UNIT, today),
                 ' '.join('%s@%g' % t for t in _tiers(conn, pid)) or '(no tier)'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--pids', required=True, help='comma list drawn from %s' % sorted(PLAN))
    ap.add_argument('--mode', required=True, choices=['rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: repeat --pids exactly')
    ap.add_argument('--operator', required=True, help='who is running this')
    ap.add_argument('--reason', required=True, help='why')
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
        problems = preconditions(conn, pids, today)
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
        _print_table(conn, pids, "BEFORE", today)

        for pid in pids:
            apply_one(conn, pid)

        bad = assert_invariants(conn, pids, before, others_before, today)
    except Exception:
        conn.rollback()
        raise

    if bad:
        conn.rollback()
        print("\nROLLED BACK — invariants failed:")
        for p in bad:
            print("  ✗", p)
        return 1

    _print_table(conn, pids, "\nAFTER (in-transaction)", today)
    print("\nCUSTOMER QUOTES (asked in their own latest bill's unit). Only %d may move: it is the "
          "one getting a price." % PRICED)
    for pid in pids:
        after = _answers(conn, pid, today)
        for code, was in sorted(before[pid]['answers'].items()):
            moved = '' if (after[code][1] == was[1]
                           and abs((after[code][2] or 0) - (was[2] or 0)) <= 0.005) else '  <= MOVED'
            print("  %4d %-10s %-5s %-18s ฿%-9s -> %-18s ฿%-9s%s"
                  % (pid, code, was[0], was[1], was[2], after[code][1], after[code][2], moved))

    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    print("\nCOMMITTED (%s) — re-read on a new connection:" % a.mode)
    n_bad = 0
    for pid in pids:
        base, stock = chk.execute(
            "SELECT p.base_sell_price, COALESCE(s.quantity, 0) FROM products p "
            "LEFT JOIN stock_levels s ON s.product_id = p.id WHERE p.id=?", (pid,)).fetchone()
        ratio = chk.execute("SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
                            (pid, PAIR_UNIT)).fetchone()
        want_base = NEW_BASE if pid == PRICED else PLAN[pid]['base']
        good = (ratio is not None and ratio[0] == 1.0 and _money(base) == _money(want_base)
                and _money(stock) == _money(before[pid]['stock']))
        n_bad += not good
        print("  %s %4d %-26s %s=%s base %-7.2f stock %g"
              % ('OK ' if good else 'BAD', pid, PLAN[pid]['label'], PAIR_UNIT,
                 ratio[0] if ratio else None, base, stock))
    chk.close()
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
