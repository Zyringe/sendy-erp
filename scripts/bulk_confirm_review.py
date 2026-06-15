#!/usr/bin/env python
"""Bulk-confirm the 'sensible cleanup' pending rows in customer_contact_review.

A pending row is a SENSIBLE cleanup when its proposed_phone contains at least one valid Thai
phone number (a recovered 02/08X number, or a phone that was reorganized out of a people/fax/
contact mix). Those get confirmed: the proposal is written to `customers` (original frozen in
contact_orig_json — fully reversible), and the review row is marked 'confirmed'.

Rows whose proposed_phone has NO valid number (genuine typos / unparseable junk) are LEFT in
'pending' for a human.

Nickname is NOT written (advisory only — confirmed per-row in the UI). Name is written but never
nulled (falls back to the existing name when proposed_name is blank).

Usage:
    python scripts/bulk_confirm_review.py [--db PATH] [--apply] [--user NAME]
Default = dry run (counts + samples, writes nothing).
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, '..', 'inventory_app')
sys.path.insert(0, APP)
import sqlite3  # noqa: E402
from customer_contact_normalize import is_valid_thai_phone  # noqa: E402

DEFAULT_DB = os.path.join(APP, 'instance', 'inventory.db')


def _has_valid_phone(proposed_phone):
    if not proposed_phone:
        return False
    return any(is_valid_thai_phone(tok) for tok in proposed_phone.split(','))


def _digit_runs(text, minlen=7):
    return [re.sub(r'\D', '', t) for t in re.split(r'[\s,]+', text or '')
            if len(re.sub(r'\D', '', t)) >= minlen]


def _modernize(d):
    if len(d) == 9 and d.startswith('0') and d[1] in '1689':
        return '0' + '8' + d[1:]
    return None


def _lossless(orig, *fields):
    concat = re.sub(r'\D', '', ' '.join(f or '' for f in fields))
    for run in (_digit_runs(orig.get('name')) + _digit_runs(orig.get('phone'))
                + _digit_runs(orig.get('contact'))):
        if run in concat:
            continue
        m = _modernize(run)
        if m and m in concat:
            continue
        return False
    return True


def run(db_path, apply, user):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    rows = conn.execute(
        "SELECT * FROM customer_contact_review WHERE status='pending'").fetchall()

    apply_rows, leave = [], 0
    for r in rows:
        if _has_valid_phone(r['proposed_phone']):
            apply_rows.append(r)
        else:
            leave += 1

    print("pending total       : %d" % len(rows))
    print("  -> bulk-confirm    : %d" % len(apply_rows))
    print("  -> left for manual : %d (no usable number in the proposal)" % leave)
    print("\nSample confirmations (orig.phone -> phone | fax | contact | note):")
    for r in apply_rows[:10]:
        o = json.loads(r['original_json'])
        print("  [%s] %r -> %r | fax=%r | ct=%r | note=%r" % (
            r['customer_code'], o.get('phone', ''), r['proposed_phone'],
            r['proposed_fax'], r['proposed_contact'], r['proposed_note']))

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        conn.close()
        return

    try:
        conn.execute('BEGIN IMMEDIATE')
        for r in apply_rows:
            code = r['customer_code']
            existing = conn.execute(
                "SELECT name FROM customers WHERE code=?", (code,)).fetchone()
            if not existing:
                continue  # orphan (no master row) — skip, leave pending row as-is
            name = (r['proposed_name'] or '').strip() or existing['name']
            conn.execute(
                """UPDATE customers SET
                      name=?, phone=?, fax=?, contact=?, contact_note=?,
                      contact_orig_json=COALESCE(contact_orig_json, ?),
                      contact_normalized_at=datetime('now','localtime'),
                      contact_normalized_by=?
                   WHERE code=?""",
                (name, r['proposed_phone'] or None, r['proposed_fax'] or None,
                 r['proposed_contact'] or None, r['proposed_note'] or None,
                 r['original_json'], user, code))
            conn.execute(
                """UPDATE customer_contact_review
                   SET status='confirmed', reviewed_by=?, reviewed_at=datetime('now','localtime')
                   WHERE customer_code=?""", (user, code))

        # in-transaction lossless re-check (read back from customers)
        bad = []
        for r in apply_rows:
            code = r['customer_code']
            cust = conn.execute(
                "SELECT name,phone,fax,contact,contact_note FROM customers WHERE code=?",
                (code,)).fetchone()
            if not cust:
                continue
            if not _lossless(json.loads(r['original_json']), cust['phone'], cust['fax'],
                             cust['contact'], cust['contact_note'], cust['name']):
                bad.append(code)
        if bad:
            conn.execute('ROLLBACK')
            print("\n!! ABORTED — lossless failed: %s" % bad[:10])
            conn.close()
            sys.exit(1)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        conn.close()
        raise

    print("\nCONFIRMED %d rows to customers (all lossless); %d left pending."
          % (len(apply_rows), leave))
    conn.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--user', default='bulk_confirm')
    args = ap.parse_args()
    run(args.db, args.apply, args.user)
