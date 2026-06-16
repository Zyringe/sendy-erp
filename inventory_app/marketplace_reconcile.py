"""Segment the wallet ledger into bank-deposit cycles.

A bank deposit is a ``withdrawal`` row with a NEGATIVE amount (money leaving the
wallet for the bank). It closes a cycle: the deposit equals the sum of every
income + adjustment + withdrawal-reversal since the previous deposit. We write
one ``marketplace_payouts`` row per deposit and link its orders via
``marketplace_orders.payout_id``.

Two real-world wrinkles this handles (both seen in 2025 prod data):

  * **Withdrawal reversals.** Shopee sometimes reverses a failed transfer — a
    ``withdrawal``-type row with a POSITIVE amount (money returning to the
    wallet). That is an inflow, NOT a deposit, so it adds to the current cycle
    and does not close it. Only a negative-amount withdrawal is a real deposit.
  * **Imbalanced cycles.** A mid-history export can start partway through a cycle
    (the first deposit includes income from before the file → the cycle is
    short), and income credited right on a deposit boundary can land in the
    adjacent cycle (a small over/under split). Such a cycle is still recorded but
    flagged ``status='unbalanced'`` for review — it does NOT abort the whole
    reconcile. A clean cycle is ``status='reconciled'``.

Rebuilds from scratch each run (idempotent). A trailing open cycle (income after
the last deposit, not withdrawn yet) is skipped.
"""
_TOL = 0.01


class ReconcileError(Exception):
    """Kept for callers that still catch it; reconcile no longer raises it for
    ordinary imbalance (those are flagged per-cycle instead)."""
    pass


def reconcile_payouts(conn, platform='shopee'):
    """Rebuild marketplace_payouts + order links from marketplace_wallet_txns.

    Returns {'payouts': int, 'orders_linked': int, 'unbalanced': int}.
    Resilient: an imbalanced cycle is flagged status='unbalanced', never raised,
    so one odd cycle can't wipe out every other deposit's reconciliation.
    """
    conn.execute("UPDATE marketplace_orders SET payout_id = NULL WHERE platform = ?", (platform,))
    conn.execute("DELETE FROM marketplace_payouts WHERE platform = ?", (platform,))

    rows = conn.execute(
        """SELECT txn_time, txn_type, order_sn, amount FROM marketplace_wallet_txns
           WHERE platform = ? ORDER BY txn_time ASC, id ASC""", (platform,)).fetchall()

    n_payouts = n_linked = n_unbalanced = 0
    cur_orders = []      # order_sns accumulated in the open cycle
    cur_income = 0.0     # income + adjustment + reversal total in the open cycle
    for r in rows:
        amount = r['amount'] or 0
        # A deposit (cycle close) is ONLY a withdrawal that takes money OUT.
        # A positive-amount withdrawal is a reversal → treat as an inflow.
        if r['txn_type'] == 'withdrawal' and amount < 0:
            wd = round(-amount, 2)
            diff = round(cur_income - wd, 2)
            status = 'reconciled' if abs(diff) <= _TOL else 'unbalanced'
            if status == 'unbalanced':
                n_unbalanced += 1
            pid = conn.execute(
                """INSERT INTO marketplace_payouts
                     (platform, deposit_date, amount, n_orders, status)
                   VALUES (?,?,?,?,?)""",
                (platform, str(r['txn_time'])[:10], wd, len(cur_orders), status)).lastrowid
            if cur_orders:
                qs = ','.join('?' * len(cur_orders))
                conn.execute(
                    f"""UPDATE marketplace_orders SET payout_id = ?
                        WHERE platform = ? AND order_sn IN ({qs})""",
                    [pid, platform, *cur_orders])
                n_linked += len(cur_orders)
            n_payouts += 1
            cur_orders, cur_income = [], 0.0
        else:
            # income | adjustment | withdrawal-reversal(+) → inflow into the cycle
            cur_income += amount
            sn = r['order_sn']
            if sn:
                cur_orders.append(sn)
    conn.commit()
    return {'payouts': n_payouts, 'orders_linked': n_linked, 'unbalanced': n_unbalanced}
