#!/usr/bin/env python
"""Backfill customer_contact_review from the normalizer + auto-apply the safe rows.

Populates the staging table `customer_contact_review` (one row per customer that has a
proposed change or needs review) and, with --apply, writes the high-confidence
"auto" rows through to `customers` — but ONLY the lossless, unambiguous fax-split /
field-tidy changes (see customer_contact_normalize for the auto rule).

Usage:
    python scripts/backfill_customer_review.py [--db PATH] [--apply] [--user NAME]

Default (no --apply): DRY RUN. Computes everything, prints the plan, writes nothing.
--apply: in ONE transaction — upsert the review table + apply the auto-with-change rows.
         Before committing it re-derives a lossless invariant on every applied row
         (every >=7-digit number in the frozen original must survive in the new row);
         any failure ROLLS BACK and aborts. Re-runnable: never clobbers review rows that
         are already 'confirmed' or 'skipped', and skips customers already 'applied'
         (so cleaned data is never re-normalized).

Idempotent + safe: the original {name,phone,contact,address} is frozen into
customers.contact_orig_json (COALESCE, written once) so every change is reversible.
"""
import argparse
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, '..', 'inventory_app')
sys.path.insert(0, APP)
from customer_contact_normalize import normalize_customer  # noqa: E402

DEFAULT_DB = os.path.join(APP, 'instance', 'inventory.db')
ORIG_KEYS = ('name', 'phone', 'contact', 'address')
# Fields the auto-apply writes back to customers (address is intentionally NOT auto-written —
# only fax/phone/contact/nickname; name never changes on an auto row).
DONE_STATUSES = ('applied', 'confirmed', 'skipped')


def _orig_dict(row):
    return {k: (row[k] if row[k] is not None else '') for k in ORIG_KEYS}


def _norm_ws(s):
    return re.sub(r'\s+', ' ', (s or '').strip())


def _auto_has_change(orig, prop):
    """An auto row is worth applying iff one of the AUTO-WRITTEN fields meaningfully changes
    (ignoring pure whitespace). Nickname is deliberately excluded: it is an advisory guess
    (per the plan, never auto-overwritten) — stored in the review table for P3, not
    auto-applied. A row whose only difference is a nickname guess or whitespace is a no-op."""
    return (
        _norm_ws(prop['phone']) != _norm_ws(orig['phone'])
        or _norm_ws(prop['fax']) != ''
        or _norm_ws(prop['contact']) != _norm_ws(orig['contact'])
    )


def _digit_runs(text, minlen=7):
    runs = []
    for tok in re.split(r'[\s,]+', text or ''):
        d = re.sub(r'\D', '', tok)
        if len(d) >= minlen:
            runs.append(d)
    return runs


def _lossless_survives(orig, new_phone, new_fax, new_contact, new_name, new_nick):
    """Every >=7-digit number in the frozen original must survive in the new customer row."""
    from collections import Counter
    have = (Counter(_digit_runs(orig.get('name'))) + Counter(_digit_runs(orig.get('phone')))
            + Counter(_digit_runs(orig.get('contact'))))
    got = (Counter(_digit_runs(new_phone)) + Counter(_digit_runs(new_fax))
           + Counter(_digit_runs(new_contact)) + Counter(_digit_runs(new_name))
           + Counter(_digit_runs(new_nick)))
    return not (have - got)


def run(db_path, apply, user):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')

    customers = conn.execute(
        "SELECT code, name, phone, contact, address FROM customers ORDER BY code"
    ).fetchall()
    existing = {
        r['customer_code']: r['status']
        for r in conn.execute("SELECT customer_code, status FROM customer_contact_review")
    }

    plan = {'total': len(customers), 'skipped_done': 0, 'noop': 0,
            'review': 0, 'auto_apply': 0, 'lossless_fail': []}
    review_rows = []   # (code, orig, prop, confidence, issues, status)
    apply_rows = []    # (code, orig, prop)

    for r in customers:
        code = r['code']
        if existing.get(code) in DONE_STATUSES:
            plan['skipped_done'] += 1
            continue
        orig = _orig_dict(r)
        res = normalize_customer(orig)
        prop, conf, issues = res['proposed'], res['confidence'], res['issues']

        if conf == 'auto':
            if not _auto_has_change(orig, prop):
                plan['noop'] += 1          # already clean — no review row, nothing to do
                continue
            # lossless self-check on the exact values we will write
            if not _lossless_survives(orig, prop['phone'], prop['fax'],
                                      prop['contact'], orig['name'], prop['nickname'] or ''):
                plan['lossless_fail'].append(code)
                conf, issues = 'review', issues + ['lossless_risk']
                review_rows.append((code, orig, prop, conf, issues, 'pending'))
                plan['review'] += 1
                continue
            apply_rows.append((code, orig, prop))
            review_rows.append((code, orig, prop, 'auto', issues, 'applied'))
            plan['auto_apply'] += 1
        else:
            review_rows.append((code, orig, prop, conf, issues, 'pending'))
            plan['review'] += 1

    _print_plan(plan, apply_rows)

    if not apply and not review_rows:
        conn.close()
        return plan
    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        conn.close()
        return plan

    # ---- WRITE (single transaction) ----
    try:
        conn.execute('BEGIN IMMEDIATE')
        for code, orig, prop, conf, issues, status in review_rows:
            reviewed = (status == 'applied')
            conn.execute(
                """INSERT INTO customer_contact_review
                     (customer_code, original_json, proposed_name, proposed_nickname,
                      proposed_phone, proposed_fax, proposed_contact, proposed_address,
                      proposed_region, confidence, issues_json, status, reviewed_by, reviewed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,
                           CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END)
                   ON CONFLICT(customer_code) DO UPDATE SET
                      original_json=excluded.original_json,
                      proposed_name=excluded.proposed_name,
                      proposed_nickname=excluded.proposed_nickname,
                      proposed_phone=excluded.proposed_phone,
                      proposed_fax=excluded.proposed_fax,
                      proposed_contact=excluded.proposed_contact,
                      proposed_address=excluded.proposed_address,
                      proposed_region=excluded.proposed_region,
                      confidence=excluded.confidence,
                      issues_json=excluded.issues_json,
                      status=excluded.status,
                      reviewed_by=excluded.reviewed_by,
                      reviewed_at=excluded.reviewed_at
                   WHERE customer_contact_review.status NOT IN ('confirmed','skipped')""",
                (code, json.dumps(orig, ensure_ascii=False), prop['name'], prop['nickname'],
                 prop['phone'], prop['fax'], prop['contact'], prop['address'],
                 prop['region'], conf, json.dumps(issues, ensure_ascii=False), status,
                 user if reviewed else None, reviewed),
            )
        for code, orig, prop in apply_rows:
            # NOTE: nickname is intentionally NOT written here (advisory only — confirmed in P3).
            conn.execute(
                """UPDATE customers SET
                      phone=?, fax=?, contact=?,
                      contact_orig_json=COALESCE(contact_orig_json, ?),
                      contact_normalized_at=datetime('now','localtime'),
                      contact_normalized_by=?
                   WHERE code=?""",
                (prop['phone'] or None, prop['fax'] or None, prop['contact'] or None,
                 json.dumps(orig, ensure_ascii=False), user, code),
            )

        # ---- in-transaction invariant re-check (independent: read back from customers) ----
        bad = []
        for code, orig, prop in apply_rows:
            row = conn.execute(
                "SELECT name, phone, fax, contact, nickname FROM customers WHERE code=?",
                (code,)).fetchone()
            if not _lossless_survives(orig, row['phone'] or '', row['fax'] or '',
                                      row['contact'] or '', row['name'] or '',
                                      row['nickname'] or ''):
                bad.append(code)
        if bad:
            conn.execute('ROLLBACK')
            print("\n!! ABORTED — lossless invariant failed on applied rows: %s" % bad[:10])
            conn.close()
            sys.exit(1)

        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        conn.close()
        raise

    print("\nAPPLIED: %d review rows upserted, %d auto rows written to customers (all lossless)."
          % (len(review_rows), len(apply_rows)))
    conn.close()
    return plan


def _print_plan(plan, apply_rows):
    print("customers total      : %d" % plan['total'])
    print("  already done (skip) : %d" % plan['skipped_done'])
    print("  already clean (noop): %d" % plan['noop'])
    print("  -> review (queued)  : %d" % plan['review'])
    print("  -> auto-apply        : %d" % plan['auto_apply'])
    if plan['lossless_fail']:
        print("  !! lossless-forced-to-review: %s" % plan['lossless_fail'][:10])
    print("\nSample auto-apply changes (orig.phone -> phone | fax | contact):")
    for code, orig, prop in apply_rows[:10]:
        print("  [%s] %r -> %r | fax=%r | ct=%r" %
              (code, orig['phone'], prop['phone'], prop['fax'], prop['contact']))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--apply', action='store_true', help='write (default is dry-run)')
    ap.add_argument('--user', default='backfill', help='contact_normalized_by value')
    args = ap.parse_args()
    run(args.db, args.apply, args.user)
