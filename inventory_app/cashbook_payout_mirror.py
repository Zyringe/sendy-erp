"""Mirror marketplace_payouts into locked cashbook income rows (issue #533).

marketplace_payouts is DELETED and REBUILT on every reconcile (see
marketplace_reconcile.py), so its `id` is not a stable identity a cashbook
row can carry as a foreign key. mirror_platform() instead identifies a
payout by VALUE — platform + deposit_date + amount, plus an occurrence
number for the (rare but real — see
data/migrations/108_marketplace_payouts_drop_unique.sql) case where two
payouts share all three — and stores that natural key directly on the
cashbook row (payout_platform / payout_deposit_date / payout_amount /
payout_occurrence, migration 182). A row is "payout-sourced" (locked,
mirror-owned) iff payout_platform IS NOT NULL.

Steady-state only: insert what's missing, delete what's gone, update a
matched row whose n_orders drifted, touch nothing else. Manual rows
(payout_platform IS NULL) are NEVER read, written or deleted here —
including a manual row whose amount and date happen to coincide with a
real payout. The one-time conversion of the pre-existing
hand-keyed LEX/SPX rows that DO represent real payouts is a separate,
one-time operation (scripts/convert_legacy_cashbook_payout_rows.py), never
repeated by this function.

Call after marketplace_reconcile.reconcile_payouts(conn, platform) has
committed. Idempotent — calling it again with no payout change is a no-op.
"""

PLATFORM_ACCOUNT_CODE = {'lazada': 'LEX', 'shopee': 'SPX'}
PLATFORM_LABEL_TH = {'lazada': 'Lazada', 'shopee': 'Shopee'}
PAYOUT_CATEGORY = 'ยอดขายของ'
PAYOUT_CREATED_BY = 'ระบบ'
MIN_DEPOSIT_DATE = '2026-01-01'

# Matches scripts/convert_legacy_cashbook_payout_rows.py's own match window —
# that script imports this constant rather than keeping its own copy, so the
# two can never drift apart (review finding: they used to be independent
# numbers with no code tying them together).
CONFLICT_WINDOW_DAYS = 2


class CashbookPayoutMirrorError(Exception):
    """The platform has no active destination cashbook account (unknown
    platform, or the account is missing/inactive). Raised instead of
    silently skipping — issue #533: "show a visible error and never skip
    silently"."""


def _target_payouts(conn, platform):
    """{(deposit_date, amount, occurrence): n_orders} for every
    marketplace_payouts row of `platform` on/after MIN_DEPOSIT_DATE.

    occurrence numbers duplicates sharing (deposit_date, amount) in the
    payouts table's own row order (deposit_date, id ASC) — stable across a
    rebuild that reproduces the same rows in the same order, which is what
    makes this idempotent.
    """
    seen = {}
    target = {}
    for r in conn.execute(
        """SELECT deposit_date, amount, n_orders FROM marketplace_payouts
            WHERE platform = ? AND deposit_date >= ?
            ORDER BY deposit_date, id""",
        (platform, MIN_DEPOSIT_DATE),
    ):
        amt = round(r['amount'], 2)
        key2 = (r['deposit_date'], amt)
        occurrence = seen.get(key2, 0) + 1
        seen[key2] = occurrence
        target[(r['deposit_date'], amt, occurrence)] = r['n_orders']
    return target


def _existing_mirrored_rows(conn, account_id, platform):
    """{(deposit_date, amount, occurrence): {'id':, 'description':}} for the
    payout-sourced rows already on `account_id`. `description` is carried so
    mirror_platform can detect a natural key that still matches but whose
    n_orders changed underneath it (the natural key has no room for
    n_orders — see mirror_platform's docstring)."""
    return {
        (r['payout_deposit_date'], round(r['payout_amount'], 2), r['payout_occurrence']):
            {'id': r['id'], 'description': r['description']}
        for r in conn.execute(
            """SELECT id, payout_deposit_date, payout_amount, payout_occurrence, description
                 FROM cashbook_transactions
                WHERE account_id = ? AND payout_platform = ?""",
            (account_id, platform),
        )
    }


def _conflicting_manual_row_exists(conn, account_id, deposit_date, amount):
    """True if an UN-LINKED manual income row already sits on this account
    whose amount matches (rounded 2dp, same algorithm as the conversion
    script) and whose txn_date is within CONFLICT_WINDOW_DAYS of
    `deposit_date` — i.e. this payout most likely already has a hand-keyed
    row that scripts/convert_legacy_cashbook_payout_rows.py has not yet
    turned into a payout-sourced row. Guards against the deploy-then-import
    ordering hazard (review finding): inserting here BEFORE that one-time
    conversion runs would silently double-book the payout, and the
    conversion script's own matcher would then never detect the resulting
    duplicate (it only looks at rows still un-linked)."""
    row = conn.execute(
        """SELECT 1 FROM cashbook_transactions
            WHERE account_id = ? AND direction = 'income' AND payout_platform IS NULL
              AND ROUND(amount, 2) = ROUND(?, 2)
              AND ABS(julianday(txn_date) - julianday(?)) <= ?
            LIMIT 1""",
        (account_id, amount, deposit_date, CONFLICT_WINDOW_DAYS),
    ).fetchone()
    return row is not None


def _resolve_account_id(conn, platform):
    account_code = PLATFORM_ACCOUNT_CODE.get(platform)
    if account_code is None:
        raise CashbookPayoutMirrorError(
            f"ไม่รู้จักแพลตฟอร์ม '{platform}' — ไม่มีบัญชีปลายทางที่กำหนดไว้สำหรับยอดโอน"
        )
    account = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code = ? AND is_active = 1",
        (account_code,),
    ).fetchone()
    if account is None:
        label = PLATFORM_LABEL_TH.get(platform, platform)
        raise CashbookPayoutMirrorError(
            f"ไม่พบบัญชี {account_code} (หรือถูกปิดใช้งาน) — ยอดโอนของ {label} "
            "ยังไม่ถูกบันทึกลงบัญชีรับ-จ่าย"
        )
    return account['id']


def mirror_platform(conn, platform):
    """Insert/delete/update cashbook_transactions on `platform`'s account so
    its payout-sourced rows equal marketplace_payouts (platform, on/after
    2026-01-01) exactly — description included. Manual rows are never
    inspected for a match and never touched.

    A key present in both target and existing is normally left alone, BUT
    its description is re-derived and UPDATEd in place when n_orders
    changed underneath an unchanged (deposit_date, amount, occurrence) —
    real case: a Lazada statement's settled amount is authoritative and
    fixed once banked, while its order membership is read fresh from
    marketplace_wallet_txns every rebuild and can grow if more order rows
    for that already-settled statement arrive later. This also keeps a
    shrinking duplicate-amount group correct: when one payout in a group
    drops out, the survivors' occurrence numbers can shift, and without
    this update pass a surviving row could be left describing a payout
    that no longer exists at that occurrence slot.

    A key that would INSERT is instead skipped (counted in
    'skipped_conflicts', never silently applied) when an un-linked manual
    row already matches it within CONFLICT_WINDOW_DAYS — the deploy-then-
    import ordering hazard above. The caller should surface a visible
    warning when this is non-zero, pointing at running the conversion
    script; the skipped payout is picked up automatically on the next
    mirror run once that manual row is converted or cleared.

    Returns {'inserted': int, 'deleted': int, 'updated': int,
    'skipped_conflicts': int, 'unchanged': int}. Every write here happens
    inside one failure boundary: any
    exception rolls back everything THIS call wrote (not anything a prior
    commit — e.g. reconcile_payouts' own — already made durable) before
    re-raising, so a mid-loop failure can never be silently flushed by a
    later, unrelated commit sharing this connection (get_connection() sets
    no isolation_level, so writes stay pending until an explicit commit or
    rollback). Raises CashbookPayoutMirrorError (nothing written, nothing
    committed — raised before any write) if the destination account is
    missing/inactive or the platform is unrecognized.
    """
    account_id = _resolve_account_id(conn, platform)

    target = _target_payouts(conn, platform)
    existing = _existing_mirrored_rows(conn, account_id, platform)
    label = PLATFORM_LABEL_TH.get(platform, platform)

    # The conflict check is by (deposit_date, amount) only, not occurrence —
    # if two payouts share a key and only one has a matching manual row, both
    # candidates get skipped rather than guessing which one the manual row
    # is for. Over-cautious in that rare duplicate-group case, but it can
    # only ever DEFER a real insert, never risk a double-book, which is the
    # direction that matters here.
    candidate_insert = [key for key in target if key not in existing]
    to_insert, skipped_conflicts = [], []
    for (deposit_date, amount, occurrence) in candidate_insert:
        if _conflicting_manual_row_exists(conn, account_id, deposit_date, amount):
            skipped_conflicts.append((deposit_date, amount, occurrence))
        else:
            to_insert.append((deposit_date, amount, occurrence))
    to_delete = [existing[key]['id'] for key in existing if key not in target]
    to_update = []
    for key in target:
        if key not in existing:
            continue
        expected_desc = f"{label} โอนเงิน ({target[key]} ออเดอร์)"
        if existing[key]['description'] != expected_desc:
            to_update.append((existing[key]['id'], expected_desc))

    try:
        for (deposit_date, amount, occurrence) in to_insert:
            n_orders = target[(deposit_date, amount, occurrence)]
            conn.execute(
                """INSERT INTO cashbook_transactions
                     (account_id, txn_date, direction, category, amount,
                      description, created_by,
                      payout_platform, payout_deposit_date, payout_amount, payout_occurrence)
                   VALUES (?, ?, 'income', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, deposit_date, PAYOUT_CATEGORY, amount,
                 f"{label} โอนเงิน ({n_orders} ออเดอร์)", PAYOUT_CREATED_BY,
                 platform, deposit_date, amount, occurrence),
            )
        for txn_id in to_delete:
            conn.execute("DELETE FROM cashbook_transactions WHERE id = ?", (txn_id,))
        for txn_id, new_desc in to_update:
            conn.execute(
                "UPDATE cashbook_transactions SET description = ? WHERE id = ?",
                (new_desc, txn_id),
            )
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return {
        'inserted': len(to_insert),
        'deleted': len(to_delete),
        'updated': len(to_update),
        'skipped_conflicts': len(skipped_conflicts),
        'unchanged': len(target) - len(candidate_insert) - len(to_update),
    }
