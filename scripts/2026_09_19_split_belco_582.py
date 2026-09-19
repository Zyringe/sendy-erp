"""2026-09-19 — split the BELCO lines off pid 1305 (issue #582).

WHY. pid 1305 is `ดอกลมหัวลูกบล็อก META 8mmx65mm`. Two lines keyed under its
bsn_code 528ด8655 carry a BELCO raw name:

  * purchase HP6900041 (2026-07-17, ขจรศิลป์, 13 อัน @ ฿7.00)
  * sale     IV6901138-7 (2026-07-18, 56ภ01,   13 อัน @ ฿10.00)

The purchase landed at walk stock 0, so #546 took its ฿7.00 as the whole WACC
and 1305 fell from ฿24.50 (RR6700438: 30 อน @ ฿35 less 30%) to ฿7.00.

Put ruled 2026-09-19: BELCO is a different product from META. Create it and
move those two lines onto it. The team re-keys them in Express under a new item
code later; until then both rows keep bsn_code 528ด8655 and that code keeps
mapping to 1305, so the move is ONLY on these two rows.

⚠ A full-history import covering July reverts this. `import_weekly` resolves
528ด8655 -> 1305 through the mapping, `bsn_line.field_diff` sees product_id
differ, and the line is DELETE+INSERTed back onto 1305. The daily DBF upload
(since_days=60) no longer reaches 2026-07-17/18, so only a manual
full-history upload does this. See the PR for the exact revert condition.

The replay is the app's own Reconciliation Procedure (the one
`bsn_sync.update_unit_conversion_ratio` and `mapping.repoint_bsn_code` run):
reset synced -> delete every ledger row a BSN sync wrote -> re-sync through
`_sync_bsn_to_stock` -> check identity by identity -> recompute WACC.
Not `repoint_bsn_code`: that moves EVERY row of a bsn_code, and the ruling is
about two lines of it, not the code.

Shape borrowed from scripts/2026_09_19_gross_to_piece.py: refuse on any drift
BEFORE the first write, fingerprint every other product, assert invariants
inside the transaction, roll back unless --apply.

    python3 scripts/2026_09_19_split_belco_582.py --db PATH
    python3 scripts/2026_09_19_split_belco_582.py --db PATH --apply
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter

ACTOR = 'script:2026_09_19_split_belco_582'
REASON = ('#582 (Put 2026-09-19): BELCO is a different product from META; '
          'this line moves off pid 1305 onto the new BELCO product')
CREATED_VIA = 'script #582 split from 1305'

META_PID = 1305
META_NAME = 'ดอกลมหัวลูกบล็อก META 8mmx65mm'
NEW_NAME = 'ดอกลมหัวลูกบล็อก BELCO 8mmx65mm'
BSN_CODE = '528ด8655'
UNITS = {'อัน': 1.0, 'อน': 1.0}   # mirror 1305, so the moved rows sync

# (table, doc_no, product_name_raw). The purchase raw name really has two spaces.
MOVES = (
    ('purchase_transactions', 'HP6900041', 'ดอกลมหัวลูกบล็อก 8mmx65mm  BELCO'),
    ('sales_transactions', 'IV6901138-7', 'ดอกลมหัวลูกบล็อก 8mmx65mm BELCO'),
)
# 1305's whole source history. Any other line changes what the costs below mean.
META_DOCS = {
    'purchase_transactions': ('HP6900041', 'RR6700438'),
    'sales_transactions': ('IV6702971-2', 'IV6901138-7'),
}
COST_BEFORE = 7.00        # what HP6900041 left on 1305 (#546 zero-stock branch)
META_COST_AFTER = 24.50   # RR6700438: 30 @ ฿35 less 30%
BELCO_COST_AFTER = 7.00   # HP6900041: 13 @ ฿7
TABLES = ('sales_transactions', 'purchase_transactions')


class InvariantFailed(Exception):
    """Raised mid-split; the caller rolls the whole transaction back."""


def _stock(conn, pid):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0


def _ledger_sum(conn, pid):
    return conn.execute("SELECT COALESCE(SUM(quantity_change), 0) FROM transactions "
                        "WHERE product_id=?", (pid,)).fetchone()[0]


def _movements(conn, pid):
    """A product's ledger as a multiset of movements, ids and product left out,
    so the same movement on a different product compares equal."""
    return Counter(tuple(r) for r in conn.execute(
        "SELECT txn_type, quantity_change, reference_no, note, created_at, "
        "source_bsn_code, source_line_seq FROM transactions WHERE product_id=?", (pid,)))


def _source_pids(conn):
    return {(t, r[0]): r[1] for t in TABLES
            for r in conn.execute("SELECT id, product_id FROM %s" % t)}


def _others(conn, exclude):
    """Every other product's money, stock and ledger columns, as one comparable list."""
    ex = ','.join(str(p) for p in exclude)
    return [tuple(r) for r in conn.execute(
        "SELECT p.id, p.product_name, p.unit_type, p.cost_price, p.base_sell_price, p.is_active,"
        "       COALESCE(s.quantity, 0),"
        "       (SELECT COUNT(*) || '/' || COALESCE(SUM(quantity_change), 0)"
        "          FROM transactions t WHERE t.product_id = p.id),"
        "       (SELECT COUNT(*) FROM product_cost_ledger c WHERE c.product_id = p.id)"
        "  FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id"
        " WHERE p.id NOT IN (%s) ORDER BY p.id" % ex)]


def product_state(conn, pid):
    return {
        'cost': conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0],
        'stock': _stock(conn, pid),
        'ledger': [tuple(r) for r in conn.execute(
            "SELECT substr(created_at, 1, 10), txn_type, quantity_change, reference_no, note "
            "FROM transactions WHERE product_id=? ORDER BY created_at, id", (pid,))],
        'cost_ledger': [tuple(r) for r in conn.execute(
            "SELECT event_date, event_type, reference_no, qty_change, unit_cost, wacc_after "
            "FROM product_cost_ledger WHERE product_id=? ORDER BY id", (pid,))],
        'source': [(t,) + tuple(r) for t in TABLES for r in conn.execute(
            "SELECT doc_no, qty, unit, net, synced_to_stock FROM %s WHERE product_id=? "
            "ORDER BY date_iso, id" % t, (pid,))],
    }


def preconditions(conn):
    """(problems, moves, brand_id). Reads only. `moves` is [(table, row id)]."""
    bad, moves = [], []
    p = conn.execute("SELECT product_name, unit_type, cost_price FROM products WHERE id=?",
                     (META_PID,)).fetchone()
    if p is None or p['product_name'] != META_NAME:
        bad.append("pid 1305 is %r, expected %r — wrong DB?"
                   % (p['product_name'] if p else None, META_NAME))
    else:
        if p['unit_type'] != 'ตัว':
            bad.append("pid 1305 unit_type %r, expected 'ตัว'" % p['unit_type'])
        if abs(p['cost_price'] - COST_BEFORE) > 1e-9:
            bad.append("pid 1305 cost_price %r, expected %.2f — already fixed, or a later "
                       "bill moved it" % (p['cost_price'], COST_BEFORE))
    if _stock(conn, META_PID) != 0:
        bad.append("pid 1305 stock %r, expected 0" % _stock(conn, META_PID))
    uc = {r[0]: r[1] for r in conn.execute(
        "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (META_PID,))}
    if uc != UNITS:
        bad.append("pid 1305 unit_conversions %r, expected %r" % (uc, UNITS))

    for table, doc_no, raw in MOVES:
        rows = conn.execute(
            "SELECT id FROM %s WHERE doc_no=? AND bsn_code=? AND product_id=? AND product_name_raw=?"
            % table, (doc_no, BSN_CODE, META_PID, raw)).fetchall()
        if len(rows) != 1:
            bad.append("%s %s: %d row(s) match (doc_no, bsn_code, product_id 1305, raw name), "
                       "expected exactly 1" % (table, doc_no, len(rows)))
        else:
            moves.append((table, rows[0][0]))

    for table, docs in META_DOCS.items():
        have = sorted(r[0] for r in conn.execute(
            "SELECT doc_no FROM %s WHERE product_id=?" % table, (META_PID,)))
        if have != sorted(docs):
            bad.append("pid 1305 %s lines are %s, expected %s" % (table, have, sorted(docs)))
        n = conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=? AND synced_to_stock=0"
                         % table, (META_PID,)).fetchone()[0]
        if n:
            bad.append("pid 1305: %d unsynced row(s) in %s — sync before splitting" % (n, table))

    dup = conn.execute("SELECT id FROM products WHERE product_name=?", (NEW_NAME,)).fetchall()
    if dup:
        bad.append("%r already exists (pid %s) — refusing to split twice"
                   % (NEW_NAME, [r[0] for r in dup]))

    brands = [r[0] for r in conn.execute(
        "SELECT id FROM brands WHERE upper(name)='BELCO' OR upper(code)='BELCO'")]
    if len(brands) > 1:
        bad.append("more than one BELCO brand %s — pick one by hand" % brands)
    return bad, moves, (brands[0] if brands else None)


def snapshot(conn):
    from models import bsn_sync
    return {
        'movements': _movements(conn, META_PID),
        'synced': bsn_sync._synced_source_ids(conn, META_PID),
        'source_pids': _source_pids(conn),
        'others': _others(conn, (META_PID,)),
    }


def split(conn, moves, brand_id, before):
    """Create the product, move the rows, replay both ledgers, recompute both
    costs. Writes; never commits. Returns the new product id."""
    from models import _shared, bsn_sync, wacc
    from models.products import create_structured_product

    src = conn.execute("SELECT category_id, sub_category, size FROM products WHERE id=?",
                       (META_PID,)).fetchone()
    new_pid = create_structured_product({
        'product_name': NEW_NAME, 'unit_type': 'ตัว', 'cost_price': 0.0, 'base_sell_price': 0.0,
        'brand_id': brand_id, 'category_id': src['category_id'],
        'sub_category': src['sub_category'], 'size': src['size'],
    }, CREATED_VIA, conn=conn)
    for unit, ratio in UNITS.items():
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                     (new_pid, unit, ratio))

    for table, row_id in moves:
        _shared.declared_update(conn, table, row_id, {'product_id': new_pid, 'synced_to_stock': 0},
                                actor=ACTOR, source='manual', reason=REASON)

    pids = (META_PID, new_pid)
    for table in TABLES:
        conn.execute("UPDATE %s SET synced_to_stock=0 WHERE product_id IN (?,?)" % table, pids)
    conn.execute(
        "DELETE FROM transactions WHERE product_id IN (?,?) AND ({})".format(
            " OR ".join("note LIKE ?" for _ in bsn_sync._BSN_LEDGER_NOTE_PATTERNS)),
        pids + tuple(bsn_sync._BSN_LEDGER_NOTE_PATTERNS))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=pids)
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=pids)

    # Identity by identity, before any cost is computed off the rebuilt ledger:
    # every row that held a movement before must hold one now, on either product.
    lost = before['synced'] - (bsn_sync._synced_source_ids(conn, META_PID)
                               | bsn_sync._synced_source_ids(conn, new_pid))
    if lost:
        raise InvariantFailed("replay lost %d movement(s): %s" % (len(lost), sorted(lost)))

    wacc.preflight_batch(conn, pids, operation='split_belco_582')
    for pid in pids:
        wacc.recalculate_product_wacc(pid, conn)
    return new_pid


def assert_invariants(conn, before, new_pid, moves):
    from models import mapping
    bad = []
    for pid, want in ((META_PID, META_COST_AFTER), (new_pid, BELCO_COST_AFTER)):
        cost = conn.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
        if abs(cost - want) > 1e-9:
            bad.append("pid %s cost_price %r, expected %.2f" % (pid, cost, want))
        stock, led = _stock(conn, pid), _ledger_sum(conn, pid)
        if stock != 0:
            bad.append("pid %s stock %r, expected 0" % (pid, stock))
        if abs(led - stock) > 1e-9:
            bad.append("pid %s stock %r != SUM(ledger) %r" % (pid, stock, led))

    # A pure relabel: the same movements, only split across two products.
    after = _movements(conn, META_PID) + _movements(conn, new_pid)
    if after != before['movements']:
        bad.append("ledger movement set changed — lost %s, gained %s"
                   % (sorted(before['movements'] - after), sorted(after - before['movements'])))

    now = _source_pids(conn)
    was = before['source_pids']
    changed = {k: (was.get(k), now.get(k)) for k in set(was) | set(now) if was.get(k) != now.get(k)}
    expected = {k: (META_PID, new_pid) for k in moves}
    if changed != expected:
        bad.append("source rows that changed product_id: %s, expected exactly %s"
                   % (sorted(changed.items()), sorted(expected.items())))

    n = mapping._bsn_code_ledger_orphans(conn, BSN_CODE)
    if n:
        bad.append("%d orphan ledger row(s) on %s's documents" % (n, BSN_CODE))

    if _others(conn, (META_PID, new_pid)) != before['others']:
        bad.append("a product outside the split changed")
    return bad


def run(conn, apply):
    """(exit code, report). 0 = done (rolled back unless apply), 1 = an
    invariant failed and everything was rolled back, 2 = refused before writing."""
    report = {'problems': [], 'before': {}, 'after': {}, 'new_pid': None, 'moves': []}
    conn.execute("BEGIN IMMEDIATE")
    try:
        bad, moves, brand_id = preconditions(conn)
        if bad:
            conn.rollback()
            report['problems'] = bad
            return 2, report
        report['moves'] = moves
        report['brand_id'] = brand_id
        report['before'][META_PID] = product_state(conn, META_PID)
        before = snapshot(conn)
        try:
            new_pid = split(conn, moves, brand_id, before)
            bad = assert_invariants(conn, before, new_pid, moves)
        except InvariantFailed as exc:
            bad = [str(exc)]
        if bad:
            conn.rollback()
            report['problems'] = bad
            return 1, report
        report['new_pid'] = new_pid
        report['after'][META_PID] = product_state(conn, META_PID)
        report['after']['belco'] = product_state(conn, new_pid)
        report['sku_code'] = conn.execute("SELECT sku_code FROM products WHERE id=?",
                                          (new_pid,)).fetchone()[0]
    except Exception:
        conn.rollback()
        raise
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return 0, report


def _print_state(label, s):
    print("  %s: cost_price %.4f  stock %g" % (label, s['cost'], s['stock']))
    print("    source rows (table, doc_no, qty, unit, net, synced):")
    for r in s['source']:
        print("      %s" % (r,))
    print("    ledger (date, type, qty, ref, note):")
    for r in s['ledger']:
        print("      %s" % (r,))
    print("    cost ledger (date, event, ref, qty, unit_cost, wacc_after):")
    for r in s['cost_ledger']:
        print("      %s" % (r,))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    # Same app-dir discovery as 2026_09_19_gross_to_piece.py: a copy of this
    # script on the prod container must not resolve `models` against `/`.
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isdir(os.path.join(cand, 'models')):
            sys.path.insert(0, cand)
            break
    else:
        print("REFUSED — cannot locate inventory_app; set SENDY_APP_DIR")
        sys.exit(2)

    conn = sqlite3.connect(a.db, timeout=15)
    # set up like models.database.get_connection: _sync_bsn_to_stock reads rows by name
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    code, report = run(conn, a.apply)
    if code:
        print("REFUSED — preconditions not met:" if code == 2
              else "ROLLED BACK — invariants failed:")
        for p in report['problems']:
            print("  ✗", p)
        sys.exit(code)

    print("BEFORE")
    _print_state("pid 1305 META", report['before'][META_PID])
    print("\nAFTER (in-transaction)")
    _print_state("pid 1305 META", report['after'][META_PID])
    _print_state("pid %s BELCO (new, sku %s, brand_id %s)"
                 % (report['new_pid'], report['sku_code'], report['brand_id']),
                 report['after']['belco'])
    if not a.apply:
        print("\nREHEARSAL — rolled back, nothing written.")
        return
    conn.close()

    # Independent re-read on a fresh connection, not the report above.
    chk = sqlite3.connect(a.db)
    new_pid = report['new_pid']
    costs = dict(chk.execute("SELECT id, cost_price FROM products WHERE id IN (?,?)",
                             (META_PID, new_pid)))
    moved = [chk.execute("SELECT product_id FROM %s WHERE id=?" % t, (i,)).fetchone()[0]
             for t, i in report['moves']]
    ok = (abs(costs.get(META_PID, 0) - META_COST_AFTER) < 1e-9
          and abs(costs.get(new_pid, 0) - BELCO_COST_AFTER) < 1e-9
          and moved == [new_pid] * len(MOVES))
    print("\nCOMMITTED — re-read on a new connection: %s  1305 cost %r, %s cost %r, moved rows on %s"
          % ('OK' if ok else 'BAD', costs.get(META_PID), new_pid, costs.get(new_pid), moved))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
