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

Steady-state only: insert what's missing, delete what's gone, touch nothing
else. Manual rows (payout_platform IS NULL) are NEVER read, written or
deleted here — including a manual row whose amount and date happen to
coincide with a real payout. The one-time conversion of the pre-existing
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
    """{(deposit_date, amount, occurrence): txn_id} for the payout-sourced
    rows already on `account_id`."""
    return {
        (r['payout_deposit_date'], round(r['payout_amount'], 2), r['payout_occurrence']): r['id']
        for r in conn.execute(
            """SELECT id, payout_deposit_date, payout_amount, payout_occurrence
                 FROM cashbook_transactions
                WHERE account_id = ? AND payout_platform = ?""",
            (account_id, platform),
        )
    }


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
    """Insert/delete cashbook_transactions on `platform`'s account so its
    payout-sourced rows equal marketplace_payouts (platform, on/after
    2026-01-01) exactly. Manual rows are never inspected for a match and
    never touched.

    Returns {'inserted': int, 'deleted': int, 'unchanged': int}. Commits on
    success. Raises CashbookPayoutMirrorError (nothing written, nothing
    committed) if the destination account is missing/inactive or the
    platform is unrecognized.
    """
    account_id = _resolve_account_id(conn, platform)

    target = _target_payouts(conn, platform)
    existing = _existing_mirrored_rows(conn, account_id, platform)

    to_insert = [key for key in target if key not in existing]
    to_delete = [existing[key] for key in existing if key not in target]

    label = PLATFORM_LABEL_TH.get(platform, platform)
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

    conn.commit()
    return {
        'inserted': len(to_insert),
        'deleted': len(to_delete),
        'unchanged': len(target) - len(to_insert),
    }
