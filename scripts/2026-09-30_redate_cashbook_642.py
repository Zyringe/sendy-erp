#!/usr/bin/env python3
"""ONE-OFF PROD data fix — Card F PR-0 (decisions/log.md 2026-09-30, Q7 = B).

Re-dates cashbook_transactions 642 (txn_date 2026-06-01, 3000, keyed 2026-07-03)
to 2026-06-30 so it equals its source salary_advances 26 (advance_date
2026-06-30). Both dates are June 2026: no month total, balance or payroll figure
moves; only the source-equality oracle (I11) goes from 1 to 0.

Stdlib only (prod has no pandas and no sqlite3 CLI). Not a cost write, so no
database.script_connection signing.

Usage:
    python redate_cashbook_642.py --operator NAME [--db PATH]           # rehearse
    python redate_cashbook_642.py --operator NAME [--db PATH] --apply   # write

Default is REHEARSE: the same statements run, the before/after row is printed,
then ROLLBACK. --apply COMMITs, then re-reads on a FRESH connection.
Exit codes: 0 ok, 2 a precondition refused, 3 the write or an invariant failed.
A second --apply refuses at the txn_date precondition (idempotent by refusal).
"""
import argparse
import json
import sqlite3
import sys

ROW_ID = 642
ADVANCE_ID = 26
OLD_DATE = '2026-06-01'
NEW_DATE = '2026-06-30'
AMOUNT = 3000.0
RUN_ID = 6
REASON = 'Q7 B: match salary_advances 26'


class Refused(Exception):
    """A precondition failed (exit 2)."""


class Failed(Exception):
    """The write or an invariant failed (exit 3)."""


def _connect(db):
    conn = sqlite3.connect(db, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    return conn


def _month_totals(conn, account_id, direction):
    r = conn.execute(
        "SELECT ROUND(COALESCE(SUM(amount), 0), 2) AS s, COUNT(*) AS n"
        " FROM cashbook_transactions"
        " WHERE account_id = ? AND direction = ? AND strftime('%Y-%m', txn_date) = '2026-06'",
        (account_id, direction)).fetchone()
    return (r['s'], r['n'])


def _check_preconditions(conn):
    row = conn.execute("SELECT * FROM cashbook_transactions WHERE id = ?", (ROW_ID,)).fetchone()
    if row is None:
        raise Refused(f'cashbook_transactions {ROW_ID} does not exist')
    if row['salary_advance_id'] != ADVANCE_ID:
        raise Refused(f'row {ROW_ID}: salary_advance_id is {row["salary_advance_id"]!r}, expected {ADVANCE_ID}')
    if row['txn_date'] != OLD_DATE:
        raise Refused(f'row {ROW_ID}: txn_date is {row["txn_date"]!r}, expected {OLD_DATE!r}'
                      ' (already re-dated, or not the row this fix was written for)')
    if row['amount'] != AMOUNT:
        raise Refused(f'row {ROW_ID}: amount is {row["amount"]!r}, expected {AMOUNT}')
    adv = conn.execute("SELECT * FROM salary_advances WHERE id = ?", (ADVANCE_ID,)).fetchone()
    if adv is None:
        raise Refused(f'salary_advances {ADVANCE_ID} does not exist')
    if adv['advance_date'] != NEW_DATE:
        raise Refused(f'salary_advances {ADVANCE_ID}: advance_date is {adv["advance_date"]!r}, expected {NEW_DATE!r}')
    if adv['amount'] != AMOUNT:
        raise Refused(f'salary_advances {ADVANCE_ID}: amount is {adv["amount"]!r}, expected {AMOUNT}')
    if adv['deducted_in_run_id'] != RUN_ID:
        raise Refused(f'salary_advances {ADVANCE_ID}: deducted_in_run_id is {adv["deducted_in_run_id"]!r}, expected {RUN_ID}')
    n = conn.execute("SELECT COUNT(*) FROM cashbook_transactions WHERE salary_advance_id = ?",
                     (ADVANCE_ID,)).fetchone()[0]
    if n != 1:
        raise Refused(f'{n} cashbook rows link to salary_advances {ADVANCE_ID}, expected exactly 1')
    return row


def _row_line(conn):
    r = conn.execute("SELECT id, txn_date, amount, account_id, direction, salary_advance_id"
                     " FROM cashbook_transactions WHERE id = ?", (ROW_ID,)).fetchone()
    return dict(r)


def run(db, operator, apply):
    conn = _connect(db)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = _check_preconditions(conn)
            before = _row_line(conn)
            totals_before = _month_totals(conn, row['account_id'], row['direction'])
            cur = conn.execute(
                "UPDATE cashbook_transactions SET txn_date = ? WHERE id = ? AND txn_date = ?",
                (NEW_DATE, ROW_ID, OLD_DATE))
            if cur.rowcount != 1:
                raise Failed(f'UPDATE touched {cur.rowcount} rows, expected 1')
            conn.execute(
                "INSERT INTO audit_log (table_name, row_id, action, changed_fields, user)"
                " VALUES ('cashbook_transactions', ?, 'UPDATE', ?, ?)",
                (ROW_ID, json.dumps({'txn_date': [OLD_DATE, NEW_DATE], 'reason': REASON},
                                    ensure_ascii=False), operator))
            totals_after = _month_totals(conn, row['account_id'], row['direction'])
            if totals_after != totals_before:
                raise Failed(f'June-2026 SUM/COUNT moved: {totals_before} -> {totals_after}')
            after = _row_line(conn)
            print('before:', before)
            print('after: ', after)
            print(f'June-2026 (account {row["account_id"]}, {row["direction"]}) SUM/COUNT unchanged: {totals_after}')
            if not apply:
                conn.execute('ROLLBACK')
                print('REHEARSE: rolled back, nothing written. Re-run with --apply to write.')
            else:
                conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()

    fresh = _connect(db)  # independent of the write connection
    try:
        want = NEW_DATE if apply else OLD_DATE
        got = fresh.execute("SELECT txn_date FROM cashbook_transactions WHERE id = ?", (ROW_ID,)).fetchone()[0]
        if got != want:
            raise Failed(f're-read: txn_date is {got!r}, expected {want!r}')
        mismatched = fresh.execute(
            "SELECT COUNT(*) FROM cashbook_transactions t JOIN salary_advances sa"
            " ON sa.id = t.salary_advance_id WHERE t.txn_date != sa.advance_date").fetchone()[0]
        if apply and mismatched != 0:
            raise Failed(f're-read: {mismatched} advance rows still differ from advance_date, expected 0')
        n_audit = fresh.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='cashbook_transactions' AND row_id=?"
            " AND action='UPDATE' AND user=? AND changed_fields LIKE ?",
            (ROW_ID, operator, f'%{REASON}%')).fetchone()[0]
        if apply and n_audit < 1:
            raise Failed('re-read: explicit audit_log row missing')
        print(f're-read (fresh connection): txn_date={got}, advance rows with date != advance_date={mismatched}'
              if apply else f're-read (fresh connection): txn_date={got} (unchanged)')
    finally:
        fresh.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', default='/data/inventory.db')
    ap.add_argument('--operator', required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args(argv)
    try:
        run(args.db, args.operator, args.apply)
    except Refused as e:
        print(f'REFUSED: {e}', file=sys.stderr)
        return 2
    except Failed as e:
        print(f'FAILED: {e}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
