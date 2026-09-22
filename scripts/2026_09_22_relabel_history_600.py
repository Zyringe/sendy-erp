"""2026-09-22 — relabel the bill lines the old unit map mis-read (#600, spec #595 · 5/7).

WHY. Until migration 190 (#599) Sendy's unit map read BSN5657's `กร` as ตัว, `ถง` as
ถุง and `บล` as แผง. Express's own unit list says กุรุส / ถัง / บล็อก (ADR 0018). 190
fixed the map and gave every affected product a conversion under the new word at the
ratio its old reading resolves to, but it left the rows already written: sales lines
stored as ตัว / ถุง / แผง whose Express line was กร / ถง / บล, and batch 37's history
purchases still holding the raw code `กร`. Until they are relabelled, every DBF
upload's drift scan reports them as `line:unit`, and #603 cannot rebase 1187/1188.

THE LIST. scripts/derive_600_relabel_history.py matched every stored line to its
BSN5657 stock-card line (STCRD) by document + line, and wrote
2026_09_22_relabel_history_600.json next to this file. A line it could not recover is
listed there with its reason, and this script never touches it.

LABEL ONLY. Quantity, synced_to_stock, the ledger, stock and cost are untouched. A
row is relabelled only when its new word resolves to exactly what its stored word
resolves to today (`bsn_sync._get_base_qty`, the ledger's own conversion); a row
where they differ REFUSES the whole run, because the next ledger rebuild would move
stock.

WHAT IT WRITES. One UPDATE of `unit` per row through the migration-173 declared-change
path: change_source 'manual', change_actor = this script, a reason naming the Express
document + line and the operator, and a fresh token per row. The word comes from the
unit map (`bsn_units.translate`, the call the importer makes), so re-importing the
same Express line reads `unchanged`; the data file's word is only compared against it.

RUNS. audit_log keeps every guarded UPDATE of these two tables forever, so it is this
script's run record. A forward run is refused while any row it relabelled is still
outstanding. --undo restores what that record says each row held, and refuses if a
relabelled row has been changed by anyone since.

THE EIGHT #603 LINES. #603's hasp rebase (1187/1188) refuses until none of their bills
reads ตัว. REQUIRED names them by doc_no + bsn_code (ids churn); a plan or a DB
without one of them refuses.

Modes:
  dry-run   everything inside one transaction, invariants asserted, then rolled back.
  rehearse  commits, to a COPY only: a file named inventory.db (every DB the app
            opens) is refused.
  live      needs --confirm-live 600, and takes the app's own refuse-on-failure
            backup after the preconditions and before the first write.

    python3 scripts/2026_09_22_relabel_history_600.py --db PATH --mode dry-run \\
        --operator put --reason "#600 relabel"
    python3 scripts/2026_09_22_relabel_history_600.py --db /data/inventory.db --mode live \\
        --confirm-live 600 --operator put --reason "#600 relabel"
    ... --undo    the same modes, reversing the outstanding run

⚠ Do not run in the same deploy window as #610, or as #603's hasp rebase (which runs
AFTER this, and whose precondition this satisfies).
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid

ACTOR = 'script:2026_09_22_relabel_history_600'
UNDO_ACTOR = ACTOR + ':undo'
BOOK = 'BSN5657'
TABLES = ('sales_transactions', 'purchase_transactions')
PLAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '2026_09_22_relabel_history_600.json')
APP_DB_NAME = 'inventory.db'
CONFIRM = '600'
BACKUP_REASON = 'pre-unit-relabel-600'

# #603's hasp sales (issue #600 comment, prod 2026-09-19): 1187 = 900ข5160, 1188 = 900ข5170.
REQUIRED = (
    ('sales_transactions', 'IV6701146-6', '900ข5160'),
    ('sales_transactions', 'IV6702591-3', '900ข5160'),
    ('sales_transactions', 'IV6702875-4', '900ข5160'),
    ('sales_transactions', 'IV6801764-11', '900ข5160'),
    ('sales_transactions', 'IV6700203-5', '900ข5170'),
    ('sales_transactions', 'IV6702875-3', '900ข5170'),
    ('sales_transactions', 'IV6802085-1', '900ข5170'),
    ('sales_transactions', 'IV6802850-5', '900ข5170'),
)


def load_plan(path):
    with open(path, encoding='utf-8') as f:
        plan = json.load(f)
    for e in plan['relabel']:
        if e['table'] not in TABLES or e['stored'] == e['new']:
            raise ValueError('malformed plan row %r' % (e,))
    return plan


def _find(conn, e):
    return conn.execute(
        f"SELECT id, product_id, qty, unit, change_actor FROM {e['table']} "
        f"WHERE doc_no = ? AND bsn_code = ?", (e['doc_no'], e['bsn_code'])).fetchall()


def outstanding(conn):
    """{(table, row_id): (old, new)} for every row this script relabelled and has
    not undone, read from audit_log (every guarded UPDATE of these tables is kept)."""
    out = {}
    for t, rid, user, fields in conn.execute(
            "SELECT table_name, row_id, user, changed_fields FROM audit_log "
            "WHERE action = 'UPDATE' AND table_name IN (?, ?) AND user IN (?, ?) ORDER BY id",
            (*TABLES, ACTOR, UNDO_ACTOR)):
        old, new = json.loads(fields)['unit']
        if user == ACTOR:
            out[(t, rid)] = (old, new)
        else:
            out.pop((t, rid), None)
    return out


def preconditions(conn, plan):
    """(rows to relabel, rows already reading the new word, problems)."""
    import bsn_units
    from models import bsn_sync
    problems, todo, already = [], [], []
    earlier = outstanding(conn)
    if earlier:
        return [], [], ['already relabelled: %d row(s) from an earlier run are outstanding '
                        '(--undo first to run again)' % len(earlier)]
    for code, w in plan['words'].items():
        word = bsn_units.translate(code, BOOK, conn=conn)
        if word != w['new']:
            problems.append('the unit map reads %s %r as %r, the plan says %r — is #599 '
                            '(migration 190) on this DB?' % (BOOK, code, word, w['new']))
    if problems:
        return [], [], problems

    in_plan = {(e['table'], e['doc_no'], e['bsn_code']) for e in plan['relabel']}
    for ident in REQUIRED:
        if ident not in in_plan:
            problems.append('#603 hasp line %s / %s is not in the plan' % ident[1:])
    for e in plan['relabel']:
        where = '%s %s / %s' % (e['table'].split('_')[0], e['doc_no'], e['bsn_code'])
        rows = _find(conn, e)
        if len(rows) != 1:
            problems.append('%s: %d row(s) in the DB, expected 1%s'
                            % (where, len(rows), ' (a #603 hasp line)' if
                               (e['table'], e['doc_no'], e['bsn_code']) in REQUIRED else ''))
            continue
        row = rows[0]
        if (row['product_id'], row['qty']) != (e['product_id'], e['qty']):
            problems.append('%s: product/qty is %r, the plan says %r — changed since the '
                            'derivation' % (where, (row['product_id'], row['qty']),
                                            (e['product_id'], e['qty'])))
            continue
        if row['unit'] == e['new']:
            already.append(e)
            continue
        if row['unit'] != e['stored']:
            problems.append('%s: unit is %r, the plan says %r' % (where, row['unit'], e['stored']))
            continue
        unit_type = conn.execute("SELECT unit_type FROM products WHERE id = ?",
                                 (row['product_id'],)).fetchone()[0] or ''
        q_old = bsn_sync._get_base_qty(conn, row['product_id'], unit_type, e['stored'], row['qty'])
        q_new = bsn_sync._get_base_qty(conn, row['product_id'], unit_type, e['new'], row['qty'])
        if q_old != q_new:
            problems.append('%s: pid %s — %g %s resolves to %r but %g %s to %r; relabelling would '
                            'move stock (conversion ratio differs)'
                            % (where, row['product_id'], row['qty'], e['stored'], q_old,
                               row['qty'], e['new'], q_new))
            continue
        todo.append(dict(e, row_id=row['id']))
    return todo, already, problems


def relabel(conn, todo, operator, reason):
    """One declared UPDATE per row; the word is the unit map's, computed here."""
    import bsn_units
    for e in todo:
        word = bsn_units.translate(e['express_code'], BOOK, conn=conn)
        why = ('#600: Express %s line %s wrote %s = %s (BSN5657 unit list); stored %s by the '
               'pre-#599 map — %s: %s' % (e['express_doc'], e['express_seq'], e['express_code'],
                                           word, e['stored'], operator, reason))
        cur = conn.execute(
            f"UPDATE {e['table']} SET unit = ?, change_source = 'manual', change_actor = ?, "
            f"change_reason = ?, change_token = ? WHERE id = ? AND unit = ?",
            (word, ACTOR, why, uuid.uuid4().hex, e['row_id'], e['stored']))
        if cur.rowcount != 1:
            raise RuntimeError('row %s / %s was not %r when written' % (e['table'], e['row_id'],
                                                                         e['stored']))


def undo_preconditions(conn):
    rows = outstanding(conn)
    if not rows:
        return [], ['nothing to undo: no outstanding row from a relabel run']
    todo, problems = [], []
    for (t, rid), (old, new) in sorted(rows.items()):
        row = conn.execute(f"SELECT doc_no, unit, change_actor, product_id FROM {t} WHERE id = ?",
                           (rid,)).fetchone()
        if row is None or row['unit'] != new or row['change_actor'] != ACTOR:
            problems.append('%s id %s (%s): changed since the relabel (%r) — reconcile it by hand'
                            % (t, rid, row['doc_no'] if row else 'deleted',
                               tuple(row)[1:] if row else None))
            continue
        todo.append({'table': t, 'row_id': rid, 'old': old, 'new': new, 'doc_no': row['doc_no'],
                     'product_id': row['product_id']})
    return todo, problems


def undo(conn, todo, operator, reason):
    """Restore the unit audit_log recorded for each row, declared the same way."""
    for e in todo:
        why = '#600 undo: restore %s from the relabel\'s audit row — %s: %s' % (
            e['old'], operator, reason)
        cur = conn.execute(
            f"UPDATE {e['table']} SET unit = ?, change_source = 'manual', change_actor = ?, "
            f"change_reason = ?, change_token = ? WHERE id = ? AND unit = ?",
            (e['old'], UNDO_ACTOR, why, uuid.uuid4().hex, e['row_id'], e['new']))
        if cur.rowcount != 1:
            raise RuntimeError('row %s / %s was not %r when restored' % (e['table'], e['row_id'],
                                                                          e['new']))


# ── invariants, asserted inside the transaction ─────────────────────────────

def _digest(conn, sql):
    h = hashlib.sha256()
    n = 0
    for r in conn.execute(sql):
        h.update(repr(tuple(r)).encode('utf-8'))
        n += 1
    return n, h.hexdigest()


def fingerprint(conn):
    """What a label change must never move, read fresh from the DB."""
    bills = {}
    for t in TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")
                if not r[1].startswith('change_')]
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM {t}"):
            bills[(t, r['id'])] = dict(zip(cols, tuple(r)))
    return {
        'bills': bills,
        'products': _digest(conn, "SELECT id, unit_type, printf('%.17g', cost_price), "
                                  "printf('%.17g', opening_cost), base_sell_price FROM products "
                                  "ORDER BY id"),
        'stock': _digest(conn, "SELECT product_id, printf('%.17g', quantity) FROM stock_levels "
                               "ORDER BY product_id"),
        'ledger': _digest(conn, "SELECT * FROM transactions ORDER BY id"),
        'cost_ledger': _digest(conn, "SELECT * FROM product_cost_ledger ORDER BY id"),
        'conversions': _digest(conn, "SELECT * FROM unit_conversions ORDER BY id"),
        'unit_map': _digest(conn, "SELECT * FROM unit_map ORDER BY id"),
        'audit_max': conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0],
    }


def assert_invariants(conn, before, changes, actor):
    """`changes` = {(table, row_id): (old, new)}. Returns a list of problems."""
    bad = []
    after = fingerprint(conn)
    for k in ('products', 'stock', 'ledger', 'cost_ledger', 'conversions', 'unit_map'):
        if after[k] != before[k]:
            bad.append('%s moved' % k)
    if set(after['bills']) != set(before['bills']):
        bad.append('bill rows were added or removed')
    for key, row in before['bills'].items():
        now = after['bills'].get(key)
        if now is None:
            continue
        want = dict(row, unit=changes[key][1]) if key in changes else row
        if now != want:
            bad.append('%s id %s: %r' % (key[0], key[1],
                                         {c: (row[c], now[c]) for c in row if row[c] != now[c]}))
    audit = conn.execute(
        "SELECT table_name, row_id, action, user, changed_fields FROM audit_log WHERE id > ?",
        (before['audit_max'],)).fetchall()
    got = {(a['table_name'], a['row_id']): json.loads(a['changed_fields'])['unit']
           for a in audit if a['action'] == 'UPDATE' and a['user'] == actor}
    if len(audit) != len(changes) or got != {k: list(v) for k, v in changes.items()}:
        bad.append('audit_log gained %d row(s), expected %d UPDATE(s) of unit by %s'
                   % (len(audit), len(changes), actor))
    return bad


def _app_dir():
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isdir(os.path.join(cand, 'models')):
            return cand
    return None


def _summary(rows, undo):
    by = {}
    for e in rows:
        k = (e['product_id'], e['table'].split('_')[0],
             *((e['new'], e['old']) if undo else (e['stored'], e['new'])))
        by[k] = by.get(k, 0) + 1
    for (pid, t, old, new), n in sorted(by.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1])):
        print('    pid %-5s %-8s %s -> %s  x%d' % (pid, t, old, new, n))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', required=True)
    ap.add_argument('--plan', default=PLAN_FILE)
    ap.add_argument('--mode', required=True, choices=['dry-run', 'rehearse', 'live'])
    ap.add_argument('--confirm-live', help='live only: %s' % CONFIRM)
    ap.add_argument('--undo', action='store_true', help='reverse the outstanding relabel run')
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

    plan = None if a.undo else load_plan(a.plan)
    conn = database.script_connection(__file__, operator=a.operator, reason=a.reason,
                                      db_path=a.db)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if a.undo:
            todo, problems = undo_preconditions(conn)
            already = []
        else:
            todo, already, problems = preconditions(conn, plan)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            return 2
        if not todo:
            conn.rollback()
            print("NOTHING TO DO — %d row(s) already read the new word" % len(already))
            return 0

        if a.mode == 'live':
            # After the preconditions, so a refused run leaves the backup rotation alone.
            import db_backup
            info = db_backup.guarded_backup(BACKUP_REASON, policy='refuse', db_path=a.db,
                                            backup_dir=db_backup.default_backup_dir(a.db))
            print("BACKUP", info)

        before = fingerprint(conn)
        if a.undo:
            changes = {(e['table'], e['row_id']): (e['new'], e['old']) for e in todo}
            undo(conn, todo, a.operator, a.reason)
            bad = assert_invariants(conn, before, changes, UNDO_ACTOR)
        else:
            changes = {(e['table'], e['row_id']): (e['stored'], e['new']) for e in todo}
            relabel(conn, todo, a.operator, a.reason)
            bad = assert_invariants(conn, before, changes, ACTOR)
    except Exception:
        conn.rollback()
        raise

    if bad:
        conn.rollback()
        print("ROLLED BACK — invariants failed:")
        for p in bad[:40]:
            print("  ✗", p)
        return 1

    verb = 'restored' if a.undo else 'relabelled'
    print("%s %d row(s)%s:" % (verb.upper(), len(todo),
                                '' if a.undo else ', %d already read the new word' % len(already)))
    _summary(todo, a.undo)
    print("  stock, cost, ledger, cost ledger, conversions, sync flags: unchanged (asserted)")
    if a.mode == 'dry-run':
        conn.rollback()
        conn.close()
        print("DRY RUN — rolled back, nothing written.")
        return 0

    conn.commit()
    conn.close()
    chk = sqlite3.connect(a.db)
    try:
        n_ok = sum(chk.execute(f"SELECT COUNT(*) FROM {t} WHERE id = ? AND unit = ?",
                               (rid, new)).fetchone()[0] for (t, rid), (_o, new) in changes.items())
    finally:
        chk.close()
    print("COMMITTED (%s) — re-read on a new connection: %d of %d row(s) read the unit written"
          % (a.mode, n_ok, len(changes)))
    return 0 if n_ok == len(changes) else 1


if __name__ == '__main__':
    sys.exit(main())
