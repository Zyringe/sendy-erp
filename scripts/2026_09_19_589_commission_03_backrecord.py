"""2026-09-19 — ทวีเกียรติ (salesperson 03) onto the commission engine: record the
months Put paid by hand, and move cashbook row 845 onto the payout path (#589).

Run AFTER migration 189 (03 → Tier A) is live. Put's rulings, 2026-09-19:
  * back-record what was ACTUALLY paid, from the cashbook rows that paid it, with
    account_id=None: the cash already sits in the cashbook, so no second
    cashbook row may be posted.
      May  IV6900744  ฿85.65   cashbook #345 (engine owes ฿87.40: ฿1.75 stays owed)
      Jun  IV6900441  ฿874.80  cashbook #331
      Jul  IV6900531  ฿117.00  cashbook #639
  * row 845 (฿400 to ทวีเกียรติ, 'คอมมิชชั่น', filed under อื่นๆ because the
    จ่ายค่าคอมมิชชั่น block refused it) is deleted and re-entered through the
    real payout path on the same account (1) and date (2026-09-04), which posts
    the locked จ่ายค่าคอมมิชชั่น row for it.
      Sep  IV6901059  ฿400.00  (engine owes ฿250.00: override id 9 pays ฿5/แผ่น on
                                pid 396 at ≤ ฿84.99, 50 แผ่น. ฿400 is what left the
                                bank, so September reads overpaid by ฿150.00.)

The invoice for each month was re-derived from the engine, not from the cashbook
text: each of May-Sep 2026 holds exactly one 03 invoice (prod snapshot
2026-09-19 15:36Z). The cashbook row for Jun (#331) is dated 2026-05-05, a month
before that invoice's receipt (2026-06-05); it is recorded with the cashbook's
date as paid_date and the receipt's month as year_month, as the engine bins it.

Everything runs in ONE transaction on ONE connection. commission.py's own
functions do the writes (record_payout) and the reads (the engine), handed this
connection through a wrapper whose commit()/close() do nothing, so the engine
sees the uncommitted writes and nothing commits early. Every invariant is
asserted before COMMIT; any failure rolls the whole thing back. After COMMIT the
state is re-read on a fresh connection with the engine unpatched.

    # rehearse: any copy, never the prod path; commits to that copy
    python3 scripts/2026_09_19_589_commission_03_backrecord.py rehearse --db /path/copy.db
    # live: Put's approval is the confirm string
    python3 scripts/2026_09_19_589_commission_03_backrecord.py live --db /data/inventory.db \\
        --confirm 589-put-approved

Refuses to run twice: any existing 03 payout, or a missing/changed row 845, stops it.
"""
import argparse
import json
import os
import sqlite3
import sys

PROD_DB = '/data/inventory.db'
CONFIRM = '589-put-approved'
SP = '03'
ACCOUNT_ID = 1
ACTOR = 'Put (#589 script)'
COMMISSION_CATEGORY = 'จ่ายค่าคอมมิชชั่น'

# (cashbook id, year_month, invoice, amount paid, paid_date, engine due)
BACKRECORDS = (
    (345, '2026-05', 'IV6900744', 85.65, '2026-05-26', 87.40),
    (331, '2026-06', 'IV6900441', 874.80, '2026-05-05', 874.80),
    (639, '2026-07', 'IV6900531', 117.00, '2026-07-02', 117.00),
)
MOVE = (845, '2026-09', 'IV6901059', 400.00, '2026-09-04', 250.00)
ROW_845 = {'account_id': ACCOUNT_ID, 'txn_date': '2026-09-04', 'direction': 'expense',
           'category': 'อื่นๆ', 'user_category': 'ทวีเกียรติ', 'amount': 400.0,
           'description': 'คอมมิชชั่น'}
LINK_COLUMNS = ('payroll_run_id', 'payroll_item_id', 'salary_advance_id',
                'commission_payout_id', 'payout_platform')
MONTHS = ('2026-05', '2026-06', '2026-07', '2026-08', '2026-09')


class Refused(Exception):
    pass


class _OneTxn:
    """This script's connection as commission.py sees it: its reads see our
    uncommitted writes, and its commit()/close() cannot end our transaction."""

    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def commit(self):
        pass

    def close(self):
        pass


def _app_dir():
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isfile(os.path.join(cand, 'commission.py')):
            return cand
    return None


def _cents(x):
    return int(round(x * 100))


def _invoice(cm, ym, inv):
    rows = [r for r in cm.get_invoice_commission_for_sp(ym, SP) if r['invoice_no'] == inv]
    if len(rows) != 1:
        raise Refused('%s: engine returns %d rows for %s in %s' % (inv, len(rows), SP, ym))
    return rows[0]


def _month_view(cm):
    out = {}
    for ym in MONTHS:
        invs = cm.get_invoice_commission_for_sp(ym, SP)
        out[ym] = {
            'payable': round(sum(i['commission_due'] for i in invs
                                 if i['paid_status'] != 'settled'), 2),
            'paid_this_month': round(sum(i['paid_amount'] for i in invs), 2),
            'remaining_this_month': round(sum(i['remaining'] for i in invs), 2),
            'owed_through_month': round(sum(
                i['remaining'] for i in cm.get_invoice_commission_for_sp(
                    ym, SP, through_month=True, only_unpaid=True)), 2),
            'invoices': sorted(i['invoice_no'] for i in invs),
        }
    return out


def _account_totals(conn):
    r = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN direction='income' THEN amount ELSE -amount END) "
        "FROM cashbook_transactions WHERE account_id = ?", (ACCOUNT_ID,)).fetchone()
    return r[0], _cents(r[1] or 0)


def _september_by_category(conn):
    return {r[0]: _cents(r[1]) for r in conn.execute(
        "SELECT category, SUM(amount) FROM cashbook_transactions WHERE account_id = ? "
        "AND direction = 'expense' AND substr(txn_date, 1, 7) = '2026-09' GROUP BY category",
        (ACCOUNT_ID,))}


def _fingerprint(conn, sql, args=()):
    return tuple(conn.execute(sql, args).fetchone())


def preconditions(conn, cm):
    bad = []
    tier = conn.execute(
        "SELECT t.code FROM commission_assignments a JOIN commission_tiers t ON t.id = a.tier_id "
        "WHERE a.salesperson_code = ?", (SP,)).fetchone()
    if tier is None or tier[0] != 'A':
        bad.append('salesperson 03 is on tier %r, expected A — is migration 189 live?'
                   % (tier[0] if tier else None))
    n = conn.execute("SELECT COUNT(*) FROM commission_payouts WHERE salesperson_code = ?",
                     (SP,)).fetchone()[0]
    if n:
        bad.append('03 already holds %d commission_payouts row(s) — refusing to run twice' % n)
    acct = conn.execute("SELECT is_active, is_transfer FROM cashbook_accounts WHERE id = ?",
                        (ACCOUNT_ID,)).fetchone()
    if acct is None or tuple(acct) != (1, 0):
        bad.append('cashbook account %d is not an active non-transfer account' % ACCOUNT_ID)

    for cb_id, ym, inv, amount, paid_date, _ in BACKRECORDS:
        row = conn.execute("SELECT * FROM cashbook_transactions WHERE id = ?", (cb_id,)).fetchone()
        want = (ACCOUNT_ID, paid_date, 'expense', COMMISSION_CATEGORY, 'ทวีเกียรติ', _cents(amount))
        got = row and (row['account_id'], row['txn_date'], row['direction'], row['category'],
                       row['user_category'], _cents(row['amount']))
        if got != want:
            bad.append('cashbook #%d is %r, expected %r' % (cb_id, got, want))

    row = conn.execute("SELECT * FROM cashbook_transactions WHERE id = ?", (MOVE[0],)).fetchone()
    if row is None:
        bad.append('cashbook #845 is gone — already moved?')
    else:
        for k, v in ROW_845.items():
            if (row[k] if k != 'amount' else _cents(row[k])) != (v if k != 'amount' else _cents(v)):
                bad.append('cashbook #845 %s is %r, expected %r' % (k, row[k], v))
        for k in LINK_COLUMNS:
            if row[k] is not None:
                bad.append('cashbook #845 is linked (%s=%r) — not a hand row' % (k, row[k]))

    for _, ym, inv, _, _, due in BACKRECORDS + (MOVE,):
        cycle = cm.get_invoice_cycle_month(SP, inv)
        if cycle != ym:
            bad.append('%s: commission cycle is %r, expected %s' % (inv, cycle, ym))
            continue
        r = _invoice(cm, ym, inv)
        if (_cents(r['commission_due']), r['paid_status'], _cents(r['paid_amount'])) != (_cents(due), 'pending', 0):
            bad.append('%s: engine says due %.2f / %s / paid %.2f, expected due %.2f / pending / 0'
                       % (inv, r['commission_due'], r['paid_status'], r['paid_amount'], due))
    return bad


def apply(conn, cm):
    txn = _OneTxn(conn)
    ids = {}
    for cb_id, ym, inv, amount, paid_date, _ in BACKRECORDS:
        ids[inv] = cm.record_payout(
            ym, SP, amount, paid_date, paid_by=ACTOR, invoice_no=inv, account_id=None,
            note='ย้อนบันทึกจากสมุดรับ-จ่าย #%d (จ่ายมือ %s) เงินออกไปแล้ว ไม่ลงสมุดซ้ำ (#589)'
                 % (cb_id, paid_date),
            conn=txn)

    cb_id, ym, inv, amount, paid_date, _ = MOVE
    row = conn.execute("SELECT * FROM cashbook_transactions WHERE id = ?", (cb_id,)).fetchone()
    # The same two writes as cashbook.txn_delete: the mig-076 BEFORE DELETE
    # trigger logs the row (user NULL), then an actor-attributed audit row.
    conn.execute("DELETE FROM cashbook_transactions WHERE id = ?", (cb_id,))
    conn.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_fields, user) "
        "VALUES ('cashbook_transactions', ?, 'DELETE', ?, ?)",
        (cb_id, json.dumps({'account_id': row['account_id'], 'txn_date': row['txn_date'],
                            'direction': row['direction'], 'category': row['category'],
                            'amount': row['amount'], 'moved_to': 'commission_payouts #589'},
                           ensure_ascii=False), ACTOR))
    ids[inv] = cm.record_payout(
        ym, SP, amount, paid_date, paid_by=ACTOR, invoice_no=inv, account_id=ACCOUNT_ID,
        note='ย้ายจากสมุดรับ-จ่าย #845 ที่ลงหมวด อื่นๆ (#589)', conn=txn)
    return ids


EXPECTED_REMAINING = {'IV6900744': 1.75, 'IV6900441': 0.0, 'IV6900531': 0.0, 'IV6901059': -150.0}
EXPECTED_STATUS = {'IV6900744': 'partial', 'IV6900441': 'paid', 'IV6900531': 'paid',
                   'IV6901059': 'paid'}


def invariants(conn, cm, before, ids):
    bad = []
    plan = {inv: (ym, amount) for _, ym, inv, amount, _, _ in BACKRECORDS + (MOVE,)}
    got = {r['invoice_no']: (r['year_month'], _cents(r['amount_paid']), r['id'])
           for r in conn.execute("SELECT * FROM commission_payouts WHERE salesperson_code = ?", (SP,))}
    if len(got) != 4 or {k: v[:2] for k, v in got.items()} != {
            k: (v[0], _cents(v[1])) for k, v in plan.items()}:
        bad.append('03 payouts are %r' % got)

    linked = {r[0]: r for r in conn.execute(
        "SELECT commission_payout_id, id, account_id, txn_date, direction, category, amount "
        "FROM cashbook_transactions WHERE commission_payout_id IN (%s)" % ','.join('?' * len(ids)),
        list(ids.values()))}
    for _, _, inv, _, _, _ in BACKRECORDS:
        if ids[inv] in linked:
            bad.append('%s: a back-record posted a cashbook row — the cash would count twice' % inv)
    sep = linked.get(ids[MOVE[2]])
    if sep is None or (sep[2], sep[3], sep[4], sep[5], _cents(sep[6])) != (
            ACCOUNT_ID, MOVE[4], 'expense', COMMISSION_CATEGORY, _cents(MOVE[3])):
        bad.append('September payout cashbook row is %r' % (tuple(sep) if sep else None,))
    n_sep = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE account_id = ? AND category = ? "
        "AND substr(txn_date, 1, 7) = '2026-09' AND commission_payout_id IS NOT NULL "
        "AND user_category = (SELECT name FROM salespersons WHERE code = ?)",
        (ACCOUNT_ID, COMMISSION_CATEGORY, SP)).fetchone()[0]
    if n_sep != 1:
        bad.append('%d September จ่ายค่าคอมมิชชั่น rows for 03 carry a payout id, expected 1' % n_sep)
    if conn.execute("SELECT 1 FROM cashbook_transactions WHERE id = ?", (MOVE[0],)).fetchone():
        bad.append('cashbook #845 still exists')

    if _account_totals(conn) != before['account']:
        bad.append('account %d changed: %r -> %r' % (ACCOUNT_ID, before['account'], _account_totals(conn)))
    cats = _september_by_category(conn)
    want = dict(before['sep_categories'])
    want['อื่นๆ'] = want.get('อื่นๆ', 0) - _cents(MOVE[3])
    want[COMMISSION_CATEGORY] = want.get(COMMISSION_CATEGORY, 0) + _cents(MOVE[3])
    if {k: v for k, v in cats.items() if v} != {k: v for k, v in want.items() if v}:
        bad.append('September categories %r, expected %r' % (cats, want))
    others = _fingerprint(conn, "SELECT COUNT(*), SUM(amount), SUM(id), MAX(id) FROM "
                          "cashbook_transactions WHERE id <> ? AND COALESCE(commission_payout_id, -1) <> ?",
                          (MOVE[0], ids[MOVE[2]]))
    if others != before['other_rows']:
        bad.append('other cashbook rows changed: %r -> %r' % (before['other_rows'], others))
    other_payouts = _fingerprint(conn, "SELECT COUNT(*), SUM(amount_paid), MAX(id) FROM "
                                 "commission_payouts WHERE salesperson_code <> ?", (SP,))
    if other_payouts != before['other_payouts']:
        bad.append('other reps\' payouts changed: %r -> %r' % (before['other_payouts'], other_payouts))

    for _, ym, inv, _, _, _ in BACKRECORDS + (MOVE,):
        r = _invoice(cm, ym, inv)
        if (_cents(r['remaining']), r['paid_status']) != (_cents(EXPECTED_REMAINING[inv]), EXPECTED_STATUS[inv]):
            bad.append('%s: remaining %.2f / %s, expected %.2f / %s'
                       % (inv, r['remaining'], r['paid_status'], EXPECTED_REMAINING[inv], EXPECTED_STATUS[inv]))
    return bad


def _print_view(title, view, account, sep_cats):
    print(title)
    print('  %-8s %10s %10s %12s %14s  invoices' % ('month', 'payable', 'paid', 'remaining', 'owed-through'))
    for ym, v in view.items():
        print('  %-8s %10.2f %10.2f %12.2f %14.2f  %s' % (
            ym, v['payable'], v['paid_this_month'], v['remaining_this_month'],
            v['owed_through_month'], ','.join(v['invoices']) or '-'))
    print('  account %d: %d rows, balance %.2f' % (ACCOUNT_ID, account[0], account[1] / 100.0))
    print('  account %d September expense by category: %s' % (
        ACCOUNT_ID, ', '.join('%s %.2f' % (k, v / 100.0) for k, v in sorted(sep_cats.items()))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=('rehearse', 'live'))
    ap.add_argument('--db', required=True)
    ap.add_argument('--confirm', default='')
    a = ap.parse_args(argv)

    if not os.path.isfile(a.db):
        print('REFUSED — %s does not exist (sqlite would create an empty one)' % a.db)
        return 2
    if a.mode == 'rehearse' and os.path.realpath(a.db) == PROD_DB:
        print('REFUSED — rehearse never touches the prod path %s' % PROD_DB)
        return 2
    if a.mode == 'live' and a.confirm != CONFIRM:
        print('REFUSED — live needs --confirm %s (Put\'s approval of this run)' % CONFIRM)
        return 2
    if a.mode == 'rehearse':
        # config.py demands these; a rehearsal on a copy needs no real secret.
        os.environ.setdefault('SECRET_KEY', 'rehearsal-only')
        os.environ.setdefault('ADMIN_PASSWORD', 'rehearsal-only')

    app_dir = _app_dir()
    if app_dir is None:
        print('REFUSED — cannot locate inventory_app; set SENDY_APP_DIR')
        return 2
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import commission as cm

    conn = sqlite3.connect(a.db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('PRAGMA foreign_keys=ON')
    real_connect = cm._connect
    cm._connect = lambda db_path=None: _OneTxn(conn)
    conn.execute('BEGIN IMMEDIATE')
    try:
        problems = preconditions(conn, cm)
        if problems:
            conn.rollback()
            print('REFUSED — preconditions not met:')
            for p in problems:
                print('  ✗', p)
            return 2
        before = {
            'account': _account_totals(conn),
            'sep_categories': _september_by_category(conn),
            'other_rows': _fingerprint(conn, "SELECT COUNT(*), SUM(amount), SUM(id), MAX(id) FROM "
                                       "cashbook_transactions WHERE id <> ?", (MOVE[0],)),
            'other_payouts': _fingerprint(conn, "SELECT COUNT(*), SUM(amount_paid), MAX(id) FROM "
                                          "commission_payouts WHERE salesperson_code <> ?", (SP,)),
        }
        _print_view('BEFORE (%s, in-transaction)' % a.db, _month_view(cm), before['account'],
                    before['sep_categories'])

        ids = apply(conn, cm)
        bad = invariants(conn, cm, before, ids)
        if bad:
            conn.rollback()
            print('\nROLLED BACK — invariants failed:')
            for p in bad:
                print('  ✗', p)
            return 1
        _print_view('\nAFTER (in-transaction, before COMMIT)', _month_view(cm), _account_totals(conn),
                    _september_by_category(conn))
        conn.commit()
    except Refused as exc:
        conn.rollback()
        print('REFUSED —', exc)
        return 2
    except Exception:
        conn.rollback()
        raise
    finally:
        cm._connect = real_connect
        conn.close()

    # Independent re-read: a fresh connection and the engine unpatched.
    chk = sqlite3.connect(a.db)
    chk.row_factory = sqlite3.Row
    payouts = [dict(r) for r in chk.execute(
        "SELECT id, year_month, invoice_no, amount_paid, paid_date FROM commission_payouts "
        "WHERE salesperson_code = ? ORDER BY year_month", (SP,))]
    cb = [dict(r) for r in chk.execute(
        "SELECT id, txn_date, category, user_category, amount, commission_payout_id "
        "FROM cashbook_transactions WHERE id = ? OR commission_payout_id IN (%s)"
        % ','.join('?' * len(ids)), [MOVE[0]] + list(ids.values()))]
    chk.close()
    fresh = {inv: cm.get_invoice_commission_for_sp(ym, SP, db_path=a.db)
             for _, ym, inv, _, _, _ in BACKRECORDS + (MOVE,)}
    recheck = {inv: [(r['remaining'], r['paid_status']) for r in rows if r['invoice_no'] == inv]
               for inv, rows in fresh.items()}
    ok = (len(payouts) == 4 and len(cb) == 1 and cb[0]['id'] != MOVE[0]
          and all(recheck[inv] == [(EXPECTED_REMAINING[inv], EXPECTED_STATUS[inv])] for inv in recheck))
    print('\nCOMMITTED (%s) — re-read on a new connection:' % a.mode)
    for p in payouts:
        print('  payout', p)
    for r in cb:
        print('  cashbook', r)
    for inv, v in recheck.items():
        print('  %s remaining/status %r' % (inv, v))
    print('RE-READ OK' if ok else 'RE-READ MISMATCH — investigate before anything else')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
