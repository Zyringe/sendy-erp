"""2026-09-29 — ตะปูคอนกรีต META family 413 onto one base, `กล่องเล็ก` (#657).

WHY. The 2026 catalogue (p.155) prices all 18 META concrete nails per
`กล่องเล็ก`, and 16 of them carry that as `unit_type`. 970 and 973 say `ตัว`,
which reads as "one nail" while the price is a whole box. Put ruled 2026-09-29
that their `ตัว` IS one กล่องเล็ก.

THE RATIO IS 1, SO NOTHING SCALES. Measured on prod 2026-09-29 (migration 196):
both are at stock 0 = ledger 0, cost 0, no tier, no promotion, and every bill
they ever had (973: 6, 970: 1) is keyed `กล่อง` at ratio 1. So this is a rename
of the base word, not the #603/#649 engine: cost, base_sell_price, stock and the
ledger are untouched and asserted untouched. base_sell_price does not change,
so no price-history row is written and `_UNIT_REBASE_SOURCES` is not involved.

    pid   unit_type           unit_conversions
    970   ตัว -> กล่องเล็ก     กล่อง=1 (kept)
    973   ตัว -> กล่องเล็ก     กล่อง=1 (kept), กล่องเล็ก=1 dropped (it IS the base now;
                              added 2026-09-28 only to mask this issue)
    678   กล่องเล็ก            + กล่อง=1   the two siblings with no row at all,
    680   กล่องเล็ก            + กล่อง=1   brought level with the other 14

End state asserted for the whole family, not only these four: every member of
413 is `กล่องเล็ก` with exactly `{กล่อง: 1}`.

Modes:
  rehearse  commits to a COPY; refuses a file named inventory.db.
  live      needs --confirm-live 657 and takes the app's refuse-on-failure backup.

    python3 scripts/2026_09_29_rebase_meta_nails_657.py --db /tmp/rehearse-657.db \\
        --mode rehearse --operator put --reason "#657 rehearsal"
    python3 scripts/2026_09_29_rebase_meta_nails_657.py --db /data/inventory.db \\
        --mode live --confirm-live 657 --operator put --reason "#657 ตัว -> กล่องเล็ก"
"""
import argparse
import os
import sqlite3
import sys

FAMILY_ID = 413
OLD_UNIT = 'ตัว'
BASE = 'กล่องเล็ก'
BOX = 'กล่อง'
TARGET_CONV = {BOX: 1.0}
APP_DB_NAME = 'inventory.db'
BACKUP_REASON = 'pre-unit-rebase-657'

RENAME = {970: {BOX: 1.0}, 973: {BOX: 1.0, BASE: 1.0}}   # pid: conversions measured on prod
ADD_BOX = (678, 680)
TOUCHED = tuple(RENAME) + ADD_BOX


def _conv(conn, pid):
    return dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                             (pid,)).fetchall())


def _product(conn, pid):
    return conn.execute("SELECT unit_type, family_id, cost_price, opening_cost, base_sell_price "
                        "FROM products WHERE id=?", (pid,)).fetchone()


def _stock(conn, pid):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0


def _ledger(conn, pid):
    return conn.execute("SELECT COALESCE(SUM(quantity_change), 0) FROM transactions "
                        "WHERE product_id=?", (pid,)).fetchone()[0]


def _bills(conn, pid):
    return conn.execute(
        "SELECT 'sale', id, unit, qty FROM sales_transactions WHERE product_id=? "
        "UNION ALL SELECT 'buy', id, unit, qty FROM purchase_transactions WHERE product_id=?",
        (pid, pid)).fetchall()


def family(conn):
    return [r[0] for r in conn.execute("SELECT id FROM products WHERE family_id=? ORDER BY id",
                                       (FAMILY_ID,))]


def preconditions(conn):
    problems = []
    members = family(conn)
    for pid in TOUCHED:
        if pid not in members:
            problems.append("%d is not in family %d" % (pid, FAMILY_ID))
    for pid, conv in RENAME.items():
        p = _product(conn, pid)
        if p is None:
            continue
        if p[0] != OLD_UNIT:
            problems.append("%d unit_type %r, expected %r" % (pid, p[0], OLD_UNIT))
        if p[2] != 0 or p[3] != 0:
            problems.append("%d carries cost %r / opening %r, expected 0" % (pid, p[2], p[3]))
        if _stock(conn, pid) != 0 or _ledger(conn, pid) != 0:
            problems.append("%d stock %r ledger %r, expected 0" % (pid, _stock(conn, pid), _ledger(conn, pid)))
        if _conv(conn, pid) != conv:
            problems.append("%d conversions %r, expected %r" % (pid, _conv(conn, pid), conv))
        units = {u for _k, _i, u, _q in _bills(conn, pid)}
        if units - {BOX}:
            problems.append("%d has bill lines in %r; the 1:1 rename assumes only %r"
                            % (pid, sorted(units - {BOX}), BOX))
        for table, col in (('product_price_tiers', 'product_id'), ('promotions', 'product_id'),
                           ('platform_skus', 'internal_product_id')):
            if conn.execute("SELECT 1 FROM %s WHERE %s=?" % (table, col), (pid,)).fetchone():
                problems.append("%d has rows in %s" % (pid, table))
    for pid in ADD_BOX:
        p = _product(conn, pid)
        if p is not None and p[0] != BASE:
            problems.append("%d unit_type %r, expected %r" % (pid, p[0], BASE))
        if _conv(conn, pid):
            problems.append("%d already has conversions %r" % (pid, _conv(conn, pid)))
    for pid in set(members) - set(TOUCHED):
        p = _product(conn, pid)
        if p[0] != BASE or _conv(conn, pid) != TARGET_CONV:
            problems.append("sibling %d is %r %r; the target shape assumes %r %r"
                            % (pid, p[0], _conv(conn, pid), BASE, TARGET_CONV))
    return problems


def snapshot(conn, pids):
    from models import bsn_sync
    out = {}
    for pid in pids:
        p = _product(conn, pid)
        out[pid] = dict(money=tuple(p[2:]), stock=_stock(conn, pid), ledger=_ledger(conn, pid),
                        base_qty={(k, i): bsn_sync._get_base_qty(conn, pid, p[0], u, q)
                                  for k, i, u, q in _bills(conn, pid)})
    return out


def other_products_fingerprint(conn, pids):
    marks = ','.join('?' * len(pids))
    return (conn.execute(
        "SELECT group_concat(id || '|' || unit_type || '|' || cost_price || '|' || opening_cost "
        "|| '|' || base_sell_price, ';') FROM (SELECT * FROM products WHERE id NOT IN (%s) "
        "ORDER BY id)" % marks, pids).fetchone()[0], conn.execute(
        "SELECT group_concat(product_id || '|' || bsn_unit || '|' || ratio, ';') FROM "
        "(SELECT * FROM unit_conversions WHERE product_id NOT IN (%s) ORDER BY product_id, bsn_unit)"
        % marks, pids).fetchone()[0])


def assert_invariants(conn, members, before, others_before):
    bad = []
    after = snapshot(conn, members)
    for pid in members:
        p = _product(conn, pid)
        if p[0] != BASE:
            bad.append("%d unit_type %r" % (pid, p[0]))
        if _conv(conn, pid) != TARGET_CONV:
            bad.append("%d conversions %r" % (pid, _conv(conn, pid)))
        for k in ('money', 'stock', 'ledger', 'base_qty'):
            if after[pid][k] != before[pid][k]:
                bad.append("%d %s moved: %r -> %r" % (pid, k, before[pid][k], after[pid][k]))
        if after[pid]['stock'] != after[pid]['ledger']:
            bad.append("%d stock %r != ledger %r" % (pid, after[pid]['stock'], after[pid]['ledger']))
    if other_products_fingerprint(conn, members) != others_before:
        bad.append("a product outside family %d changed" % FAMILY_ID)
    return bad


def _app_dir():
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isdir(os.path.join(cand, 'models')):
            return cand
    return None


def _print(conn, members, title):
    print(title)
    for pid in members:
        p = _product(conn, pid)
        print("  %4d %-10s stock %-5g base %-7g %s%s"
              % (pid, p[0], _stock(conn, pid), p[4],
                 ' '.join('%s=%g' % kv for kv in sorted(_conv(conn, pid).items())) or '(none)',
                 '  <-' if pid in TOUCHED else ''))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--mode', required=True, choices=['rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: 657')
    ap.add_argument('--operator', required=True, help='who is running this (#590)')
    ap.add_argument('--reason', required=True, help='why')
    a = ap.parse_args(argv)

    if a.mode == 'rehearse' and os.path.basename(a.db) == APP_DB_NAME:
        print("REFUSED — rehearse runs on a named COPY; %s is a file the app opens" % a.db)
        return 2
    if a.mode == 'live' and a.confirm_live != '657':
        print("REFUSED — live needs --confirm-live 657")
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

    import database
    conn = database.script_connection(__file__, operator=a.operator, reason=a.reason, db_path=a.db)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        problems = preconditions(conn)
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

        members = family(conn)
        before = snapshot(conn, members)
        others_before = other_products_fingerprint(conn, members)
        _print(conn, members, "BEFORE")

        for pid in RENAME:
            conn.execute("UPDATE products SET unit_type=? WHERE id=?", (BASE, pid))
            conn.execute("DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit=?", (pid, BASE))
        for pid in ADD_BOX:
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                         (pid, BOX, TARGET_CONV[BOX]))

        bad = assert_invariants(conn, members, before, others_before)
    except Exception:
        conn.rollback()
        raise
    if bad:
        conn.rollback()
        print("\nROLLED BACK — invariants failed:")
        for p in bad:
            print("  ✗", p)
        return 1
    _print(conn, members, "\nAFTER (in-transaction)")
    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    try:
        rows = chk.execute(
            "SELECT p.id, p.unit_type, (SELECT group_concat(bsn_unit || '=' || ratio) "
            "FROM unit_conversions u WHERE u.product_id = p.id) FROM products p "
            "WHERE p.family_id=? ORDER BY p.id", (FAMILY_ID,)).fetchall()
    finally:
        chk.close()
    off = [r for r in rows if r[1] != BASE or r[2] != '%s=1.0' % BOX]
    print("\nCOMMITTED (%s) — re-read on a new connection: %d members, %d off-shape %s"
          % (a.mode, len(rows), len(off), off or ''))
    return 1 if off else 0


if __name__ == '__main__':
    sys.exit(main())
