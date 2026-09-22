"""2026-09-22 — sync pid 436's two `ช3` Shopee lines at ชุด3 (Put, decisions/log.md 2026-09-22).

WHY. pid 436 (อะไหล่ลูกกลิ้งทาสีหนา 13mm Microfiber Sendai 1in, unit_type อัน) sold two
3-packs on Shopee (customer หน้าร้านS): IV6901461-2 (2026-08-31) and IV6901500-2
(2026-09-04), 1 each. Express keyed the unit `ช3`; the unit map did not know that
code, so the lines were stored as raw `ช3`, never found the `ชุด3` = 3 conversion
(created 2026-09-15) and never deducted stock. The 6 pieces sold are still on hand
in Sendy: prod stock 12,187 should be 12,181.

ORDER. Runs only AFTER #610, which teaches the map `ช3` -> ชุด3. The word written is
`bsn_units.translate('ช3', 'BSN5657')`, the importer's own call, and the script
refuses unless that returns ชุด3, so it cannot run before #610 is live. With #610
live a re-import compares the stored line through the same map (`bsn_line._unit_same`),
so it reads these lines as `unchanged` both before and after this script: the
importer will never sync them by itself. Kept out of #610's migration on purpose, so
that migration can prove it moves no stock.

WHAT IT WRITES, in one BEGIN IMMEDIATE transaction taken before the first read:
  1. `unit` ช3 -> ชุด3 on the two lines, through the migration-173 declared-change
     path (change_source 'manual', this script as actor, a reason naming the operator,
     a fresh token per line). Found by doc_no + bsn_code, never by id.
  2. `bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(436,))`,
     the app's own sync, on the same signed connection: two 'BSN ขาย' OUT rows of −3,
     the mig-080 trigger moves stock_levels, the lines flip to synced_to_stock = 1.
     The ledger and stock_levels are never written by hand.
  3. Checks, still inside the transaction, rolled back on any difference: only pid 436's
     stock moves, by exactly −6; exactly those 2 OUT rows are new; only those 2 lines
     change, and only in unit + synced_to_stock; every product's cost, the cost ledger,
     price history, conversions, the unit map and platform stock are unchanged; audit_log
     gains exactly the 2 declared unit changes and the 2 ledger inserts.
  Cost is NOT recomputed: `_sync_bsn_to_stock` does not, and pid 436 has no purchase or
  conversion IN on or after the first sale (checked), so a later WACC rebuild gives the
  same cost_price too.

REFUSES (exit 2, nothing written) when: the map does not read `ช3` as ชุด3 (#610 not
live); either line is missing, not pid 436, not qty 1, not `ช3`, or already synced; the
ชุด3 conversion is not exactly 3; pid 436 has any other pending line (the scoped sync
would take it too); a costing IN follows the sales; this script's run is outstanding.

UNDO. `--undo` reverses the outstanding run the way the app itself unwinds a BSN sync
(delete the two 'BSN ขาย' rows, the delete trigger returns the 6 to stock, reset
synced_to_stock = 0) and restores `ช3` through the same declared path. It refuses if
either line was changed since, or if ANY other ledger row has been posted on pid 436
after the sync's two: a count or a replay may already have absorbed these 6 pieces,
and handing them back would then invent stock. That is the one case where the undo
cannot be exact, so it is left to a human.

Modes:
  dry-run   everything inside one transaction, checks asserted, then rolled back.
  rehearse  commits, to a COPY only: a file named inventory.db (every DB the app opens)
            is refused.
  live      needs --confirm-live 436, and takes the app's own refuse-on-failure backup
            after the preconditions and before the first write.

    python3 scripts/2026_09_22_sync_436_ch3.py --db PATH --mode dry-run \\
        --operator put --reason "pid 436 ช3 sync"
    python3 scripts/2026_09_22_sync_436_ch3.py --db /data/inventory.db --mode live \\
        --confirm-live 436 --operator put --reason "pid 436 ช3 sync"
    ... --undo    the same modes, reversing the outstanding run
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid

PID = 436
UNIT_TYPE = 'อัน'
BOOK = 'BSN5657'
EXPRESS_CODE = 'ช3'
EXPECT_WORD = 'ชุด3'
EXPECT_RATIO = 3.0
BSN_CODE = '999อ1501'
LINES = ('IV6901461-2', 'IV6901500-2')     # doc_no; bsn_code above
EXPECT_QTY = 1.0
EXPECT_LEG = -3.0                           # per line: 1 ชุด3 = 3 อัน, typed as its own oracle
EXPECT_STOCK_DELTA = -6.0
SALE_NOTE = 'BSN ขาย'
ACTOR = 'script:2026_09_22_sync_436_ch3'
UNDO_ACTOR = ACTOR + ':undo'
APP_DB_NAME = 'inventory.db'
CONFIRM = '436'
BACKUP_REASON = 'pre-sync-436-ch3'


def outstanding(conn):
    """{sales row id: (old, new)} for every line this script relabelled and has not
    undone, read from audit_log (every declared UPDATE of the table is kept)."""
    out = {}
    for rid, user, fields in conn.execute(
            "SELECT row_id, user, changed_fields FROM audit_log WHERE action = 'UPDATE'"
            " AND table_name = 'sales_transactions' AND user IN (?, ?) ORDER BY id",
            (ACTOR, UNDO_ACTOR)):
        old, new = json.loads(fields)['unit']
        if user == ACTOR:
            out[rid] = (old, new)
        else:
            out.pop(rid, None)
    return out


def _find(conn, doc_no):
    return conn.execute(
        "SELECT id, doc_no, product_id, qty, unit, synced_to_stock, date_iso"
        " FROM sales_transactions WHERE doc_no = ? AND bsn_code = ?",
        (doc_no, BSN_CODE)).fetchall()


def _legs(conn, doc_no):
    return conn.execute(
        "SELECT id, quantity_change FROM transactions"
        " WHERE product_id = ? AND reference_no = ? AND note = ?",
        (PID, doc_no, SALE_NOTE)).fetchall()


def preconditions(conn):
    """(the two sales rows, problems)."""
    import bsn_units
    if outstanding(conn):
        return [], ['already synced: this script\'s run is outstanding (--undo first to run again)']
    word = bsn_units.translate(EXPRESS_CODE, BOOK, conn=conn)
    if word != EXPECT_WORD:
        return [], ['the unit map reads %s %r as %r, not %r — #610 is not live on this DB'
                    % (BOOK, EXPRESS_CODE, word, EXPECT_WORD)]

    problems, rows = [], []
    prod = conn.execute("SELECT unit_type FROM products WHERE id = ?", (PID,)).fetchone()
    if prod is None or prod[0] != UNIT_TYPE:
        problems.append('pid %s unit_type is %r, expected %r' % (PID, prod and prod[0], UNIT_TYPE))
    ratio = conn.execute("SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
                         (PID, word)).fetchone()
    if ratio is None or ratio[0] != EXPECT_RATIO:
        problems.append('pid %s has no %s conversion at ratio %g (found %r)'
                        % (PID, word, EXPECT_RATIO, ratio and ratio[0]))
    for doc_no in LINES:
        found = _find(conn, doc_no)
        if len(found) != 1:
            problems.append('%s / %s: %d row(s), expected 1' % (doc_no, BSN_CODE, len(found)))
            continue
        r = found[0]
        if r['unit'] == word and r['synced_to_stock']:
            problems.append('%s: already %s and synced — nothing to do' % (doc_no, word))
        elif (r['product_id'], r['qty'], r['unit'], r['synced_to_stock']) != \
                (PID, EXPECT_QTY, EXPRESS_CODE, 0):
            problems.append('%s: pid/qty/unit/synced is %r, expected %r' % (
                doc_no, (r['product_id'], r['qty'], r['unit'], r['synced_to_stock']),
                (PID, EXPECT_QTY, EXPRESS_CODE, 0)))
        elif _legs(conn, doc_no):
            problems.append('%s: the ledger already holds a %s row for it' % (doc_no, SALE_NOTE))
        else:
            rows.append(r)
    ids = [r['id'] for r in rows]
    for t in ('sales_transactions', 'purchase_transactions'):
        for other in conn.execute(
                f"SELECT doc_no FROM {t} WHERE product_id = ? AND synced_to_stock = 0"
                f" AND id NOT IN ({','.join('?' * len(ids)) or 'NULL'})", (PID, *ids)):
            problems.append('%s %s: another pending line on pid %s would sync with these'
                            % (t.split('_')[0], other[0], PID))
    if rows:
        first = min(r['date_iso'] for r in rows)
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE product_id = ? AND txn_type = 'IN'"
            " AND (note = 'BSN ซื้อ' OR note LIKE 'แปลง:%') AND created_at >= ?",
            (PID, first)).fetchone()[0]
        if n:
            problems.append('%d purchase/conversion IN(s) on pid %s from %s on: the OUTs would '
                            'move the WACC a later rebuild computes' % (n, PID, first))
    return (rows if not problems else []), problems


def relabel(conn, rows, operator, reason):
    """One declared UPDATE per line; the word is the unit map's, computed here."""
    import bsn_units
    word = bsn_units.translate(EXPRESS_CODE, BOOK, conn=conn)
    for r in rows:
        why = ('Express %s is %s (1 ชุด = 3 %s); stored raw before #610 taught the map — '
               '%s: %s' % (EXPRESS_CODE, word, UNIT_TYPE, operator, reason))
        cur = conn.execute(
            "UPDATE sales_transactions SET unit = ?, change_source = 'manual', change_actor = ?,"
            " change_reason = ?, change_token = ? WHERE id = ? AND unit = ? AND synced_to_stock = 0",
            (word, ACTOR, why, uuid.uuid4().hex, r['id'], EXPRESS_CODE))
        if cur.rowcount != 1:
            raise RuntimeError('sales row %s was not %r when written' % (r['id'], EXPRESS_CODE))
    return word


def sync(conn):
    """The app's own sync engine, scoped to pid 436, on this connection."""
    from models import bsn_sync
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(PID,))


def undo_preconditions(conn):
    rows = outstanding(conn)
    if not rows:
        return [], ['nothing to undo: no outstanding run of this script']
    todo, problems, leg_ids = [], [], set()
    for rid, (old, new) in sorted(rows.items()):
        r = conn.execute("SELECT doc_no, unit, synced_to_stock, change_actor FROM sales_transactions"
                         " WHERE id = ?", (rid,)).fetchone()
        if r is None or (r['unit'], r['synced_to_stock'], r['change_actor']) != (new, 1, ACTOR):
            problems.append('sales id %s (%s): changed since the sync (%r) — reconcile it by hand'
                            % (rid, r['doc_no'] if r else 'deleted', tuple(r)[1:] if r else None))
            continue
        legs = _legs(conn, r['doc_no'])
        if len(legs) != 1 or legs[0]['quantity_change'] != EXPECT_LEG:
            problems.append('%s: ledger holds %r, expected one %s row of %g'
                            % (r['doc_no'], [tuple(x) for x in legs], SALE_NOTE, EXPECT_LEG))
            continue
        leg_ids.add(legs[0]['id'])
        todo.append({'row_id': rid, 'doc_no': r['doc_no'], 'old': old, 'new': new,
                     'leg_id': legs[0]['id']})
    if leg_ids:
        later = conn.execute(
            "SELECT id, txn_type, quantity_change, note FROM transactions WHERE product_id = ?"
            " AND id > ? AND id NOT IN (%s)" % ','.join('?' * len(leg_ids)),
            (PID, min(leg_ids), *leg_ids)).fetchall()
        if later:
            problems.append('pid %s ledger moved after the sync (%d row(s), e.g. %r): a count or '
                            'replay may already hold these 6 pieces — reconcile by hand'
                            % (PID, len(later), tuple(later[0])))
    return todo, problems


def undo(conn, todo, operator, reason):
    """The app's unwind of a BSN sync, for exactly these two lines: delete the leg (the
    mig-080 trigger returns it to stock), reset the flag, restore the audited unit."""
    for e in todo:
        cur = conn.execute("DELETE FROM transactions WHERE id = ? AND product_id = ? AND note = ?",
                           (e['leg_id'], PID, SALE_NOTE))
        if cur.rowcount != 1:
            raise RuntimeError('ledger row %s was gone when deleted' % e['leg_id'])
        why = 'undo of the pid %s %s sync: restore %s from its audit row — %s: %s' % (
            PID, EXPECT_WORD, e['old'], operator, reason)
        cur = conn.execute(
            "UPDATE sales_transactions SET unit = ?, synced_to_stock = 0, change_source = 'manual',"
            " change_actor = ?, change_reason = ?, change_token = ?"
            " WHERE id = ? AND unit = ? AND synced_to_stock = 1",
            (e['old'], UNDO_ACTOR, why, uuid.uuid4().hex, e['row_id'], e['new']))
        if cur.rowcount != 1:
            raise RuntimeError('sales row %s was not %r when restored' % (e['row_id'], e['new']))


# ── checks, asserted inside the transaction ─────────────────────────────────

def _digest(conn, sql):
    h = hashlib.sha256()
    n = 0
    for r in conn.execute(sql):
        h.update(repr(tuple(r)).encode('utf-8'))
        n += 1
    return n, h.hexdigest()


def fingerprint(conn):
    """What the run may and may not move, read fresh from the DB."""
    bills = {}
    for t in ('sales_transactions', 'purchase_transactions'):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")
                if not r[1].startswith('change_')]
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM {t}"):
            bills[(t, r['id'])] = dict(zip(cols, tuple(r)))
    ledger_cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
    return {
        'bills': bills,
        'ledger': {r['id']: dict(zip(ledger_cols, tuple(r)))
                   for r in conn.execute("SELECT * FROM transactions")},
        'stock': dict(conn.execute("SELECT product_id, quantity FROM stock_levels").fetchall()),
        'products': _digest(conn, "SELECT id, unit_type, printf('%.17g', cost_price), "
                                  "printf('%.17g', opening_cost), base_sell_price FROM products "
                                  "ORDER BY id"),
        'cost_ledger': _digest(conn, "SELECT * FROM product_cost_ledger ORDER BY id"),
        'price_history': _digest(conn, "SELECT * FROM product_price_history ORDER BY id"),
        'conversions': _digest(conn, "SELECT * FROM unit_conversions ORDER BY id"),
        'unit_map': _digest(conn, "SELECT * FROM unit_map ORDER BY id"),
        'platform': _digest(conn, "SELECT id, stock FROM platform_skus ORDER BY id"),
        'platform_deductions': _digest(conn, "SELECT * FROM platform_stock_deductions ORDER BY "
                                             "source_table, source_id, platform_sku_id"),
        'audit_max': conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0],
    }


def check(conn, before, changes, *, actor, stock_delta, removed_legs=None):
    """`changes` = {sales row id: (doc_no, unit before, unit after, synced after)}.
    Forward: removed_legs is None and exactly one new leg per line is expected.
    Undo: removed_legs is the set of leg ids that must be gone. Returns problems."""
    bad = []
    after = fingerprint(conn)
    for k in ('products', 'cost_ledger', 'price_history', 'conversions', 'unit_map', 'platform',
              'platform_deductions'):
        if after[k] != before[k]:
            bad.append('%s moved' % k)

    moved = sorted(p for p in set(before['stock']) | set(after['stock'])
                   if before['stock'].get(p) != after['stock'].get(p))
    if [p for p in moved if p != PID]:
        bad.append('stock moved outside pid %s: %s' % (
            PID, ', '.join('pid %s' % p for p in moved if p != PID)[:300]))
    delta = after['stock'].get(PID, 0) - before['stock'].get(PID, 0)
    if delta != stock_delta:
        bad.append('pid %s stock moved by %r, expected %g' % (PID, delta, stock_delta))
    led = conn.execute("SELECT SUM(quantity_change) FROM transactions WHERE product_id = ?",
                       (PID,)).fetchone()[0]
    if after['stock'].get(PID) != led:
        bad.append('pid %s stock %r != SUM(ledger) %r' % (PID, after['stock'].get(PID), led))

    b, a = before['ledger'], after['ledger']
    edited = sorted(i for i in set(a) & set(b) if a[i] != b[i])
    if edited:
        bad.append('existing ledger rows edited: %r' % edited[:10])
    new, gone = sorted(set(a) - set(b)), set(b) - set(a)
    docs = {doc for doc, *_ in changes.values()}
    if removed_legs is None:
        got = sorted((a[i]['product_id'], a[i]['txn_type'], a[i]['quantity_change'],
                      a[i]['note'], a[i]['reference_no']) for i in new)
        want = sorted((PID, 'OUT', EXPECT_LEG, SALE_NOTE, d) for d in docs)
        if gone or got != want:
            bad.append('ledger gained %r and lost %d row(s); expected exactly %r'
                       % ([('pid %s' % g[0],) + g[1:] for g in got][:10], len(gone), want))
    elif new or gone != set(removed_legs):
        bad.append('ledger gained %d row(s) and lost %r; expected to lose exactly %r'
                   % (len(new), sorted(gone), sorted(removed_legs)))

    if set(after['bills']) != set(before['bills']):
        bad.append('bill rows were added or removed')
    for key, row in before['bills'].items():
        now = after['bills'].get(key)
        if now is None:
            continue
        c = changes.get(key[1]) if key[0] == 'sales_transactions' else None
        want = dict(row, unit=c[2], synced_to_stock=c[3]) if c else row
        if c and row['unit'] != c[1]:
            bad.append('%s read %r before the write, expected %r' % (c[0], row['unit'], c[1]))
        if now != want:
            bad.append('%s id %s: %r' % (key[0], key[1],
                                         {k: (row[k], now[k]) for k in row if row[k] != now[k]}))

    audit = conn.execute("SELECT table_name, row_id, action, user, changed_fields FROM audit_log"
                         " WHERE id > ?", (before['audit_max'],)).fetchall()
    units = sorted((x['row_id'], x['user'], x['changed_fields']) for x in audit
                   if x['table_name'] == 'sales_transactions')
    want_units = sorted((rid, actor, json.dumps({'unit': [c[1], c[2]]}, ensure_ascii=False))
                        for rid, c in changes.items())
    ledger_audit = sorted((x['row_id'], x['action']) for x in audit if x['table_name'] == 'transactions')
    want_ledger = sorted((i, 'INSERT') for i in new) if removed_legs is None else \
        sorted((i, 'DELETE') for i in removed_legs)
    units_json = [(r, u, json.dumps(json.loads(f), ensure_ascii=False)) for r, u, f in units]
    if units_json != want_units or ledger_audit != want_ledger or \
            len(audit) != len(units) + len(ledger_audit):
        bad.append('audit_log gained %r, expected the %d declared unit change(s) by %s and %r'
                   % ([(x['table_name'], x['row_id'], x['action'], x['user']) for x in audit][:10],
                      len(changes), actor, want_ledger))
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
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--mode', required=True, choices=['dry-run', 'rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: %s' % CONFIRM)
    ap.add_argument('--undo', action='store_true', help='reverse the outstanding run')
    ap.add_argument('--operator', required=True, help='who is running this')
    ap.add_argument('--reason', required=True, help='why')
    a = ap.parse_args(argv)

    if a.mode == 'rehearse' and os.path.basename(a.db) == APP_DB_NAME:
        print("REFUSED — rehearse commits to a named COPY; %s is a file the app opens" % a.db)
        return 2
    if a.mode == 'live' and a.confirm_live != CONFIRM:
        print("REFUSED — live needs --confirm-live %s" % CONFIRM)
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

    conn = database.script_connection(__file__, operator=a.operator, reason=a.reason,
                                      db_path=a.db)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if a.undo:
            todo, problems = undo_preconditions(conn)
        else:
            rows, problems = preconditions(conn)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            return 2

        if a.mode == 'live':
            # After the preconditions, so a refused run leaves the backup rotation alone.
            import db_backup
            try:
                info = db_backup.guarded_backup(BACKUP_REASON, policy='refuse', db_path=a.db,
                                                backup_dir=db_backup.default_backup_dir(a.db))
            except db_backup.BackupRefused as exc:
                conn.rollback()
                print("REFUSED — the pre-write backup failed, nothing written: %s" % exc)
                return 2
            print("BACKUP", info)

        before = fingerprint(conn)
        stock_before = before['stock'].get(PID)
        if a.undo:
            changes = {e['row_id']: (e['doc_no'], e['new'], e['old'], 0) for e in todo}
            undo(conn, todo, a.operator, a.reason)
            bad = check(conn, before, changes, actor=UNDO_ACTOR, stock_delta=-EXPECT_STOCK_DELTA,
                        removed_legs={e['leg_id'] for e in todo})
        else:
            word = relabel(conn, rows, a.operator, a.reason)
            changes = {r['id']: (r['doc_no'], EXPRESS_CODE, word, 1) for r in rows}
            sync(conn)
            bad = check(conn, before, changes, actor=ACTOR, stock_delta=EXPECT_STOCK_DELTA)
        stock_after = conn.execute("SELECT quantity FROM stock_levels WHERE product_id = ?",
                                   (PID,)).fetchone()[0]
    except Exception:
        conn.rollback()
        raise

    if bad:
        conn.rollback()
        print("ROLLED BACK — checks failed:")
        for p in bad[:40]:
            print("  ✗", p)
        return 1

    for rid, (doc, old, new, synced) in sorted(changes.items()):
        print("  %s  %s -> %s  synced_to_stock -> %d" % (doc, old, new, synced))
    print("%s pid %s stock %g -> %g; cost, cost ledger, conversions, other products: unchanged "
          "(asserted)" % ('RESTORED' if a.undo else 'SYNCED', PID, stock_before, stock_after))
    if a.mode == 'dry-run':
        conn.rollback()
        conn.close()
        print("DRY RUN — rolled back, nothing written.")
        return 0

    conn.commit()
    conn.close()
    chk = sqlite3.connect(a.db)
    try:
        lines = chk.execute("SELECT id, unit, synced_to_stock FROM sales_transactions WHERE id IN (%s)"
                            % ','.join('?' * len(changes)), tuple(changes)).fetchall()
        stock = chk.execute("SELECT quantity FROM stock_levels WHERE product_id = ?",
                            (PID,)).fetchone()[0]
        legs = chk.execute("SELECT COUNT(*) FROM transactions WHERE product_id = ? AND note = ?"
                           " AND reference_no IN (%s)" % ','.join('?' * len(LINES)),
                           (PID, SALE_NOTE, *LINES)).fetchone()[0]
    finally:
        chk.close()
    good = (sorted(lines) == sorted((rid, c[2], c[3]) for rid, c in changes.items())
            and stock == stock_after and legs == (0 if a.undo else len(changes)))
    print("COMMITTED (%s) — re-read on a new connection: %s lines %r · stock %g · legs %d"
          % (a.mode, 'OK' if good else 'BAD', sorted(lines), stock, legs))
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
