"""2026-09-21 — RR6700253's 26in saw line: ปื้น -> โหล on pid 1658 (#586 tail).

WHY. The 2024-06-19 purchase line for เลื่อยลันดา 26" Eagle One (`633ล5026`)
was keyed `0.5 ปน` in Express, so Sendy posted **0.5 ปื้น** where the bill means
0.5 โหล = 6 ปื้น. WACC then spread ฿356.25 over half a piece: **฿712.50/ปื้น**,
against ฿56.41 for the 24in line of the very same bill.

The team re-keyed it in Express. Confirmed in their 2026-09-21 15:42 export
(`STCRD`, doc RR6700253 seq 3: `0.5 หล`, `TFACTOR 12`), and the 2026-09-19
upload's own drift scan already reported the document as differing on
`line:unit`. Sendy cannot pick it up by importing: `commit_express_dbf` scopes
the ledger to `since_days=60` and the bill is from 2024, with no UI control to
widen that window.

CONTRACT: the `+5.5` opening plug IS the 5.5 ปื้น this line failed to bring in
— the 2026-02-23 physical count already absorbed the error — so correcting the
leg must NOT add stock. Keep `SUM(ledger <= 2026-03-03)` unchanged: the leg
goes 0.5 -> 6 and the plug goes 5.5 -> 0 in the same transaction. Stock stays
0, which is what the count says, and cost lands on 356.25 / 6 = ฿59.375.

⚠ After this runs, do NOT upload a BSN5657 history file exported BEFORE the
team's re-key (2026-09-19 15:42 or earlier) that covers June 2024: the importer
would see `ปน` again, rewrite the line to 0.5 ปื้น, and with the plug gone
stock would read -5.5. A fresh export is a no-op (same line, same unit).

The Sendy spelling is NOT hardcoded here: Express's `หล` is translated through
the app's own unit map (`bsn_units`, ADR 0018), the same call the importer
makes. `tests/test_unit_writer_census.py` declares this script through it.

    python3 scripts/2026_09_21_fix_rr6700253_unit_1658.py --db PATH
    python3 scripts/2026_09_21_fix_rr6700253_unit_1658.py --db PATH --apply
"""
import argparse
import os
import sqlite3
import sys

PID = 1658
LABEL = 'เลื่อยลันดา Eagle One 26in'
DOC_NO = 'RR6700253'
BSN_CODE = '633ล5026'
EXPRESS_UNIT = 'หล'            # what the re-keyed Express line carries (TQUCOD)
OLD_UNIT = 'ปื้น'
QTY = 0.5
NET = 356.25
UNIT_PRICE = 1000.0
EXPECT_RATIO = 12.0
EXPECT_OPENING = 5.5
EXPECT_COST_BEFORE = 712.5
EXPECT_COST_AFTER = 59.375      # 356.25 / 6, typed as its own oracle
CUTOFF = '2026-03-03 23:59:59'
OPENING_NOTE = 'ยอดยกมา (back-solved)'
ACTOR = 'script:2026_09_21_fix_rr6700253_unit_1658'
REASON = ('Express re-key 2026-09-21: RR6700253 seq 3 อ่านได้ 0.5 หล (TFACTOR 12) '
          'จาก STCRD export 15:42 — Sendy ยังถือ 0.5 ปื้น เพราะ import รายวันย้อนแค่ 60 วัน')


class Refused(Exception):
    """A precondition failed. Nothing is committed."""


def _stock(conn, pid=PID):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0


def _ledger_sum(conn, upto=None, pid=PID):
    sql = "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?"
    args = [pid]
    if upto:
        sql += " AND created_at <= ?"
        args.append(upto)
    return conn.execute(sql, args).fetchone()[0]


def _line(conn):
    return conn.execute(
        "SELECT id, qty, unit, unit_price, net, synced_to_stock FROM purchase_transactions "
        "WHERE doc_no=? AND bsn_code=? AND product_id=?", (DOC_NO, BSN_CODE, PID)).fetchall()


def _legs(conn, pid=PID):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT reference_no, note, quantity_change FROM transactions "
        "WHERE product_id=? AND note LIKE 'BSN%'", (pid,)))


def _openings(conn, pid=PID):
    return [tuple(r) for r in conn.execute(
        "SELECT quantity_change, created_at FROM transactions WHERE product_id=? AND note=?",
        (pid, OPENING_NOTE))]


def _price_history(conn, pid=PID):
    return [tuple(r) for r in conn.execute(
        "SELECT field_name, old_value, new_value FROM product_price_history WHERE product_id=? "
        "ORDER BY id", (pid,))]


def min_running_balance(conn, pid=PID):
    """Lowest running stock in the order recalculate_product_wacc walks the ledger."""
    run, low = 0, 0
    for (q,) in conn.execute(
            "SELECT quantity_change FROM transactions WHERE product_id=? "
            "ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id", (pid,)):
        run += q
        low = min(low, run)
    return low


def preconditions(conn, new_unit):
    bad = []
    prod = conn.execute("SELECT unit_type, cost_price FROM products WHERE id=?", (PID,)).fetchone()
    if prod is None:
        return ["%s (pid %s) does not exist — wrong DB?" % (LABEL, PID)]
    if abs((prod[1] or 0) - EXPECT_COST_BEFORE) > 1e-9:
        bad.append("cost_price is %r, expected %g" % (prod[1], EXPECT_COST_BEFORE))

    rows = _line(conn)
    if len(rows) != 1:
        bad.append("%d line(s) for %s / %s, expected exactly 1" % (len(rows), DOC_NO, BSN_CODE))
    else:
        _id, qty, unit, price, net, synced = rows[0]
        if unit == new_unit:
            bad.append("the line is already %s — refusing to convert twice" % new_unit)
        elif unit != OLD_UNIT:
            bad.append("the line's unit is %r, expected %r" % (unit, OLD_UNIT))
        if (qty, price, net) != (QTY, UNIT_PRICE, NET):
            bad.append("the line reads qty %r @ %r net %r, expected %g @ %g net %g"
                       % (qty, price, net, QTY, UNIT_PRICE, NET))
        if not synced:
            bad.append("the line is unsynced — the ledger does not reflect it")

    ratios = dict(conn.execute(
        "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (PID,)).fetchall())
    if ratios.get(new_unit) != EXPECT_RATIO:
        bad.append("unit_conversions[%s] is %r, expected %g — the replay would skip the line"
                   % (new_unit, ratios.get(new_unit), EXPECT_RATIO))

    bill_units = {u for (u,) in conn.execute(
        "SELECT unit FROM sales_transactions WHERE product_id=? "
        "UNION SELECT unit FROM purchase_transactions WHERE product_id=?", (PID, PID))}
    unmapped = sorted(bill_units - set(ratios) - {prod[0]})
    if unmapped:
        bad.append("bills use %s with no unit_conversions row" % unmapped)

    n_unsynced = sum(conn.execute(
        "SELECT COUNT(*) FROM %s WHERE product_id=? AND COALESCE(synced_to_stock,0)=0" % t,
        (PID,)).fetchone()[0] for t in ('sales_transactions', 'purchase_transactions'))
    if n_unsynced:
        bad.append("%d unsynced bill row(s) — sync first" % n_unsynced)

    openings = _openings(conn)
    if len(openings) != 1 or abs(openings[0][0] - EXPECT_OPENING) > 1e-9:
        bad.append("opening plug %r, expected one row of %g — the count-pin arithmetic below "
                   "assumes it" % (openings, EXPECT_OPENING))
    return bad


def fix(conn, bsn_units, bsn_sync, wacc):
    """Re-unit the line, replay, absorb the difference into the opening plug.

    Returns (the word written, the plug's change). The word is the map's
    translation of Express's code, computed HERE, next to the write."""
    new_unit = bsn_units.normalize_unit(EXPRESS_UNIT, conn=conn)
    pinned = _ledger_sum(conn, CUTOFF)
    line_id = _line(conn)[0][0]
    cur = conn.execute(
        "UPDATE purchase_transactions SET unit=?, change_source='manual', change_actor=?, "
        "change_reason=?, change_token=? WHERE id=? AND unit=?",
        (new_unit, ACTOR, REASON, 'fix-rr6700253-%s' % line_id, line_id, OLD_UNIT))
    if cur.rowcount != 1:
        raise Refused("the line was not in %r when written" % OLD_UNIT)

    # The app's own Reconciliation Procedure, as update_unit_conversion_ratio
    # runs it: reset synced -> delete every ledger row a BSN sync wrote ->
    # re-sync -> every row synced before is synced again.
    synced_before = bsn_sync._synced_source_ids(conn, PID)
    for table in ('sales_transactions', 'purchase_transactions'):
        conn.execute("UPDATE %s SET synced_to_stock=0 WHERE product_id=?" % table, (PID,))
    conn.execute(
        "DELETE FROM transactions WHERE product_id=? AND ({})".format(
            " OR ".join("note LIKE ?" for _ in bsn_sync._BSN_LEDGER_NOTE_PATTERNS)),
        (PID, *bsn_sync._BSN_LEDGER_NOTE_PATTERNS))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(PID,))
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(PID,))
    lost = synced_before - bsn_sync._synced_source_ids(conn, PID)
    if lost:
        raise Refused("replay lost %d movement(s) %r" % (len(lost), sorted(lost)[:5]))

    # The plug was hiding exactly what the line failed to bring in, so it
    # shrinks by what the replay just added. Deleted rather than left at 0:
    # a zero-quantity ADJUST is noise on the product's ledger page.
    delta = pinned - _ledger_sum(conn, CUTOFF)
    opening = _openings(conn)[0][0]
    if abs(opening + delta) < 1e-9:
        conn.execute("DELETE FROM transactions WHERE product_id=? AND note=?", (PID, OPENING_NOTE))
    else:
        conn.execute("UPDATE transactions SET quantity_change=? WHERE product_id=? AND note=?",
                     (opening + delta, PID, OPENING_NOTE))
    wacc.recalculate_product_wacc(PID, conn)
    return new_unit, delta


def assert_invariants(conn, before, new_unit, delta, others_before):
    bad = []
    rows = _line(conn)
    if len(rows) != 1 or rows[0][2] != new_unit:
        bad.append("the line reads %r, expected unit %r" % (rows, new_unit))
    elif (rows[0][1], rows[0][3], rows[0][4]) != (QTY, UNIT_PRICE, NET):
        bad.append("qty/price/net moved: %r" % (rows[0],))

    legs = dict((r[0], r[2]) for r in _legs(conn))
    want_leg = QTY * EXPECT_RATIO
    if legs.get(DOC_NO) != want_leg:
        bad.append("%s posted %r, expected %g (%g %s x %g)"
                   % (DOC_NO, legs.get(DOC_NO), want_leg, QTY, new_unit, EXPECT_RATIO))
    other_before = {r[0]: r[2] for r in before['legs'] if r[0] != DOC_NO}
    if {k: v for k, v in legs.items() if k != DOC_NO} != other_before:
        bad.append("a leg outside %s changed" % DOC_NO)

    pinned = _ledger_sum(conn, CUTOFF)
    if pinned != before['pinned']:
        bad.append("count pin broken — ledger to %s is %g, was %g"
                   % (CUTOFF[:10], pinned, before['pinned']))
    stock, led = _stock(conn), _ledger_sum(conn)
    if stock != before['stock']:
        bad.append("stock moved %g -> %g; the count already absorbed this line"
                   % (before['stock'], stock))
    if stock != led:
        bad.append("stock %g != SUM(ledger) %g" % (stock, led))
    if abs(delta + (QTY * EXPECT_RATIO - QTY)) > 1e-9:
        bad.append("plug moved by %g, expected %g" % (delta, -(QTY * EXPECT_RATIO - QTY)))
    if _openings(conn):
        bad.append("opening plug still there: %r" % (_openings(conn),))

    cost = conn.execute("SELECT cost_price FROM products WHERE id=?", (PID,)).fetchone()[0]
    if abs(cost - EXPECT_COST_AFTER) > 1e-9:
        bad.append("cost_price %r, expected %g (%g / %g)"
                   % (cost, EXPECT_COST_AFTER, NET, QTY * EXPECT_RATIO))
    hist = _price_history(conn)
    new_rows = hist[len(before['hist']):]
    if hist[:len(before['hist'])] != before['hist']:
        bad.append("an existing price-history row changed")
    if [r[0] for r in new_rows] != ['cost_price']:
        bad.append("price-history rows written: %r, expected one cost_price row" % (new_rows,))

    # WACC must run AFTER the plug is gone: run before it, cost still lands on
    # 59.375 (first-purchase branch) but the ledger row reads stock_after 11.5.
    purch = conn.execute(
        "SELECT qty_change, stock_after, wacc_after FROM product_cost_ledger WHERE product_id=? "
        "AND event_type='PURCHASE' AND reference_no=?", (PID, DOC_NO)).fetchall()
    want = (QTY * EXPECT_RATIO, QTY * EXPECT_RATIO, EXPECT_COST_AFTER)
    if len(purch) != 1 or any(abs(a - b) > 1e-9 for a, b in zip(tuple(purch[0]), want)):
        bad.append("cost-ledger purchase row %r, expected qty/stock_after/wacc %r"
                   % ([tuple(r) for r in purch], want))

    low = min_running_balance(conn)
    if low < min(0, before['low']):
        bad.append("running balance now dips to %g" % low)
    if fingerprint_others(conn) != others_before:
        bad.append("a product OUTSIDE pid %s changed" % PID)
    return bad


def fingerprint_others(conn):
    q = lambda sql: [tuple(r) for r in conn.execute(sql, (PID,))]
    return (
        q("SELECT p.id, p.unit_type, p.cost_price, p.opening_cost, p.base_sell_price, "
          "COALESCE(s.quantity, 0) FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id "
          "WHERE p.id <> ? ORDER BY p.id"),
        q("SELECT product_id, bsn_unit, ratio FROM unit_conversions WHERE product_id <> ? ORDER BY 1, 2"),
        q("SELECT COUNT(*), SUM(quantity_change), SUM(id) FROM transactions WHERE product_id <> ?"),
        q("SELECT COUNT(*), SUM(qty_change * unit_cost), SUM(id) FROM product_cost_ledger "
          "WHERE product_id <> ?"),
        q("SELECT COUNT(*) FROM product_price_history WHERE product_id <> ?"),
    )


def snapshot(conn):
    return {'stock': _stock(conn), 'pinned': _ledger_sum(conn, CUTOFF), 'legs': _legs(conn),
            'hist': _price_history(conn), 'low': min_running_balance(conn),
            'cost': conn.execute("SELECT cost_price FROM products WHERE id=?",
                                 (PID,)).fetchone()[0]}


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
    import bsn_units
    import database
    from models import bsn_sync, wacc

    # The app's script channel (#590): ICT timestamps even over `railway ssh`
    # (which has no TZ), Row rows for the replay, and this script named as the
    # connection's actor.
    conn = database.script_connection(__file__, operator=ACTOR, reason=REASON, db_path=a.db)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Sendy's spelling for Express's code comes from the map, never from
        # this file (ADR 0018) — the same call models/imports.py makes.
        new_unit = bsn_units.normalize_unit(EXPRESS_UNIT, conn=conn)
        if new_unit == EXPRESS_UNIT:
            conn.rollback()
            print("REFUSED — the unit map does not translate %r; it would be stored raw"
                  % EXPRESS_UNIT)
            return 2

        problems = preconditions(conn, new_unit)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            return 2

        before = snapshot(conn)
        others_before = fingerprint_others(conn)
        print("BEFORE")
        print("  %s (pid %s)" % (LABEL, PID))
        print("    line %s / %s: %g %s @ %g, net %g" % (DOC_NO, BSN_CODE, QTY, OLD_UNIT,
                                                        UNIT_PRICE, NET))
        print("    ledger leg %r · stock %g · ledger to %s %g · opening %g · cost %g"
              % (dict((r[0], r[2]) for r in before['legs']), before['stock'], CUTOFF[:10],
                 before['pinned'], _openings(conn)[0][0], before['cost']))
        print("    Express says %r -> Sendy's map says %r (1 %s = %g %s)"
              % (EXPRESS_UNIT, new_unit, new_unit, EXPECT_RATIO, OLD_UNIT))

        written, delta = fix(conn, bsn_units, bsn_sync, wacc)
        bad = assert_invariants(conn, before, new_unit, delta, others_before)
        if written != new_unit:
            bad.append("fix wrote %r, the preconditions checked %r" % (written, new_unit))
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
    print("    line: %g %s · ledger leg %r · stock %g · ledger to %s %g · opening gone (%g) "
          "· cost %g" % (QTY, new_unit, dict((r[0], r[2]) for r in _legs(conn)), _stock(conn),
                         CUTOFF[:10], _ledger_sum(conn, CUTOFF), delta,
                         conn.execute("SELECT cost_price FROM products WHERE id=?",
                                      (PID,)).fetchone()[0]))

    if not a.apply:
        conn.rollback()
        conn.close()
        print("\nREHEARSAL — rolled back, nothing written.")
        return 0

    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    unit, qty = chk.execute("SELECT unit, qty FROM purchase_transactions WHERE doc_no=? AND "
                            "bsn_code=?", (DOC_NO, BSN_CODE)).fetchone()
    cost = chk.execute("SELECT cost_price FROM products WHERE id=?", (PID,)).fetchone()[0]
    stock = chk.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (PID,)).fetchone()[0]
    led = chk.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id=?",
                      (PID,)).fetchone()[0]
    pinned = chk.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id=? AND "
                         "created_at <= ?", (PID, CUTOFF)).fetchone()[0]
    chk.close()
    good = (unit == new_unit and qty == QTY and abs(cost - EXPECT_COST_AFTER) < 1e-9
            and stock == led == before['stock'] and pinned == before['pinned'])
    print("\nCOMMITTED — re-read on a new connection:")
    print("  %s %g %s · cost %g · stock %g · ledger %g · ledger to %s %g"
          % ('OK ' if good else 'BAD', qty, unit, cost, stock, led, CUTOFF[:10], pinned))
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
