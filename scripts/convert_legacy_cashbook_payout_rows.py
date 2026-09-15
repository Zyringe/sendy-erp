#!/usr/bin/env python3
"""ONE-TIME conversion — issue #533 "Existing rows".

Turns a pre-existing hand-keyed LEX (Lazada) / SPX (Shopee) cashbook income
row that already represents a real marketplace payout into a payout-sourced
(locked, mirror-owned) row, so cashbook_payout_mirror.mirror_platform()
recognizes it and does not insert a duplicate for the same payout.

Match rule (Put, decisions/log.md 2026-09-15 "Cashbook ต่อ" Q17 A): a manual
income row on LEX/SPX whose amount equals a marketplace_payouts row for that
platform, and whose txn_date is within 2 days of the payout's deposit_date
(either direction — the known real case is Lazada rows keyed on the bank-
arrival date, ~2 days after the payout). A payout target key (from
cashbook_payout_mirror._target_payouts — the SAME identity the steady-state
mirror will look for, occurrence included) is matched to AT MOST one manual
candidate; an amount+window match against more than one un-linked manual row
is AMBIGUOUS and is reported, never guessed. Converted fields mirror exactly
what mirror_platform() itself would have written (category, description,
created_by, the four payout_* natural-key columns) so the row becomes
indistinguishable from one the mirror inserted.

NEVER wired into the app and NEVER re-run automatically — mirror_platform()
must permanently never adopt a manual row on its own (issue #533: "Manual
rows are never touched by the mirror"). Re-running this script is harmless
(idempotent: an already-converted row is no longer a match candidate, since
the match query only looks at payout_platform IS NULL rows) but is not part
of steady-state operation.

Usage:
    python scripts/convert_legacy_cashbook_payout_rows.py rehearse [--db PATH]
    python scripts/convert_legacy_cashbook_payout_rows.py live --db PATH --yes-really

rehearse (default db: the local dev DB): prints the match diff — every row
    that WOULD convert (with its old and new shape) and every ambiguous
    match found — and writes NOTHING (the connection never commits).
live: requires --db and --yes-really. Runs inside ONE BEGIN IMMEDIATE
    transaction, asserts nothing but the matched rows changed, commits, then
    re-reads the result on a FRESH connection (independent of the write
    connection) and prints the same diff for the PR record. Take a
    `.backup` snapshot of --db before running this against prod.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'inventory_app'))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))  # after inventory_app, per repo convention

import cashbook_payout_mirror as mirror  # noqa: E402  (after sys.path setup)

# Same window mirror_platform's own conflict guard uses (CONFLICT_WINDOW_DAYS)
# — imported, not copied, so the two can never drift apart.
WINDOW_DAYS = mirror.CONFLICT_WINDOW_DAYS


def _candidates(conn, account_id, platform, deposit_date, amount):
    """Un-linked manual income rows on `account_id` whose amount matches
    (rounded 2dp) and whose txn_date is within WINDOW_DAYS of deposit_date."""
    rows = conn.execute(
        """SELECT id, txn_date, amount, description
             FROM cashbook_transactions
            WHERE account_id = ?
              AND direction = 'income'
              AND payout_platform IS NULL
              AND ROUND(amount, 2) = ROUND(?, 2)
              AND ABS(julianday(txn_date) - julianday(?)) <= ?""",
        (account_id, amount, deposit_date, WINDOW_DAYS),
    ).fetchall()
    return rows


def find_conversions(conn, platform):
    """Return (conversions, ambiguous, unmatched) for `platform`.

    conversions: list of {txn_id, old_txn_date, old_description, deposit_date,
                 amount, occurrence, n_orders, description} — the row to
                 convert and its new shape.
    ambiguous:   list of {deposit_date, amount, occurrence, candidate_ids} —
                 a target payout with MORE THAN ONE matching manual
                 candidate; skipped, needs a human.
    unmatched:   target keys with NO manual candidate (normal — the mirror
                 will insert these fresh; listed for visibility only).
    """
    account_id = mirror._resolve_account_id(conn, platform)
    target = mirror._target_payouts(conn, platform)
    existing = mirror._existing_mirrored_rows(conn, account_id, platform)
    label = mirror.PLATFORM_LABEL_TH.get(platform, platform)

    claimed_txn_ids = set()
    conversions, ambiguous, unmatched = [], [], []
    for (deposit_date, amount, occurrence), n_orders in sorted(target.items()):
        if (deposit_date, amount, occurrence) in existing:
            continue  # already payout-sourced — nothing to convert
        rows = [r for r in _candidates(conn, account_id, platform, deposit_date, amount)
                if r['id'] not in claimed_txn_ids]
        if len(rows) == 0:
            unmatched.append({'deposit_date': deposit_date, 'amount': amount,
                               'occurrence': occurrence})
        elif len(rows) == 1:
            r = rows[0]
            claimed_txn_ids.add(r['id'])
            conversions.append({
                'txn_id': r['id'],
                'old_txn_date': r['txn_date'], 'old_description': r['description'],
                'deposit_date': deposit_date, 'amount': amount, 'occurrence': occurrence,
                'n_orders': n_orders,
                'description': f'{label} โอนเงิน ({n_orders} ออเดอร์)',
            })
        else:
            ambiguous.append({'deposit_date': deposit_date, 'amount': amount,
                               'occurrence': occurrence,
                               'candidate_ids': [r['id'] for r in rows]})
    return conversions, ambiguous, unmatched


def apply_conversions(conn, platform, conversions):
    """Write the matched conversions. Caller controls the transaction
    (BEGIN/COMMIT) and must have already validated `conversions` came from
    find_conversions() on THIS connection's current state."""
    for c in conversions:
        conn.execute(
            """UPDATE cashbook_transactions
                  SET txn_date = ?, category = ?, description = ?, created_by = ?,
                      payout_platform = ?, payout_deposit_date = ?,
                      payout_amount = ?, payout_occurrence = ?
                WHERE id = ?""",
            (c['deposit_date'], mirror.PAYOUT_CATEGORY, c['description'], mirror.PAYOUT_CREATED_BY,
             platform, c['deposit_date'], c['amount'], c['occurrence'], c['txn_id']),
        )


def _print_diff(platform, conversions, ambiguous, unmatched):
    label = mirror.PLATFORM_LABEL_TH.get(platform, platform)
    print(f'== {label} ({platform}) ==')
    print(f'  convert: {len(conversions)}')
    for c in conversions:
        print(f"    txn {c['txn_id']}: {c['old_txn_date']!r} {c['old_description']!r}"
              f" -> {c['deposit_date']!r} {c['description']!r} (฿{c['amount']:.2f})")
    if ambiguous:
        print(f'  AMBIGUOUS (skipped, needs Put): {len(ambiguous)}')
        for a in ambiguous:
            print(f"    {a['deposit_date']} ฿{a['amount']:.2f} occ{a['occurrence']}"
                  f" -> candidate txn ids {a['candidate_ids']}")
    print(f'  unmatched payouts (mirror will insert fresh): {len(unmatched)}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['rehearse', 'live'])
    ap.add_argument('--db', help='DB path (required for live; defaults to the local dev DB for rehearse)')
    ap.add_argument('--yes-really', action='store_true',
                     help='required for live — the explicit "this is real" confirmation')
    args = ap.parse_args()

    if args.mode == 'live':
        if not args.db:
            print('live mode requires --db PATH', file=sys.stderr)
            sys.exit(2)
        if not args.yes_really:
            print('live mode requires --yes-really — this writes to --db', file=sys.stderr)
            sys.exit(2)
        db_path = args.db
    else:
        db_path = args.db or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..',
            'inventory_app', 'instance', 'inventory.db')

    if args.mode == 'rehearse':
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for platform in ('lazada', 'shopee'):
                conversions, ambiguous, unmatched = find_conversions(conn, platform)
                _print_diff(platform, conversions, ambiguous, unmatched)
        finally:
            conn.close()
        print('\nrehearse mode — nothing written.')
        return

    # live
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('BEGIN IMMEDIATE')
    all_results = {}
    try:
        for platform in ('lazada', 'shopee'):
            conversions, ambiguous, unmatched = find_conversions(conn, platform)
            if ambiguous:
                raise RuntimeError(
                    f'{platform}: {len(ambiguous)} ambiguous match(es) — aborting, resolve manually first')
            apply_conversions(conn, platform, conversions)
            # unmatched is captured BEFORE the mirror ever runs — it is not
            # this script's job to insert those, only to report how many the
            # next mirror_platform() run will pick up.
            all_results[platform] = (conversions, unmatched)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()

    # Independent re-read on a FRESH connection.
    verify_conn = sqlite3.connect(db_path)
    verify_conn.row_factory = sqlite3.Row
    try:
        for platform, (conversions, unmatched) in all_results.items():
            for c in conversions:
                row = verify_conn.execute(
                    "SELECT payout_platform, payout_deposit_date, payout_amount, payout_occurrence"
                    " FROM cashbook_transactions WHERE id=?", (c['txn_id'],)
                ).fetchone()
                assert row is not None, f"txn {c['txn_id']} vanished after commit"
                assert row['payout_platform'] == platform
                assert row['payout_deposit_date'] == c['deposit_date']
                assert round(row['payout_amount'], 2) == round(c['amount'], 2)
                assert row['payout_occurrence'] == c['occurrence']
            _print_diff(platform, conversions, [], unmatched)
    finally:
        verify_conn.close()
    print('\nlive mode — committed and independently re-read OK.')


if __name__ == '__main__':
    main()
