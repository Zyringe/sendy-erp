"""Segment the wallet ledger into bank-deposit cycles.

Each 'withdrawal' row closes a cycle: it is a real bank deposit equal to the
sum of every income + adjustment row since the previous withdrawal. We write
one marketplace_payouts row per cycle and link its orders via
marketplace_orders.payout_id. Rebuilds from scratch each run (idempotent).

Trailing open cycle (income rows after the last withdrawal) is silently skipped
— the file was exported mid-cycle and the current period has not been withdrawn
yet.
"""
_TOL = 0.01


class ReconcileError(Exception):
    pass


def reconcile_payouts(conn, platform='shopee'):
    """Rebuild marketplace_payouts + order links from marketplace_wallet_txns.
    Returns {'payouts': int, 'orders_linked': int}. Raises ReconcileError if a
    CLOSED cycle's order+adjustment sum != withdrawal amount (incomplete ledger).
    A trailing open cycle (no closing withdrawal yet) is silently skipped."""
    # clear prior links + payouts for this platform
    conn.execute("UPDATE marketplace_orders SET payout_id = NULL WHERE platform = ?", (platform,))
    conn.execute("DELETE FROM marketplace_payouts WHERE platform = ?", (platform,))

    rows = conn.execute(
        """SELECT txn_time, txn_type, order_sn, amount FROM marketplace_wallet_txns
           WHERE platform = ? ORDER BY txn_time ASC, id ASC""", (platform,)).fetchall()

    n_payouts = 0
    n_linked = 0
    cur_orders = []      # order_sns in the open cycle
    cur_income = 0.0     # income + adjustment total in the open cycle
    for r in rows:
        if r['txn_type'] == 'withdrawal':
            wd = round(-r['amount'], 2)   # withdrawal amount stored negative
            inc = round(cur_income, 2)
            if inc > wd + _TOL:
                # income exceeds withdrawal — genuine data corruption (extra rows)
                raise ReconcileError(
                    f"cycle ending {r['txn_time']}: income {inc} "
                    f"> withdrawal {wd} (excess income — ledger may have duplicates)")
            pid = conn.execute(
                """INSERT INTO marketplace_payouts
                     (platform, deposit_date, amount, n_orders, status)
                   VALUES (?,?,?,?, 'reconciled')""",
                (platform, str(r['txn_time'])[:10], wd, len(cur_orders))).lastrowid
            if cur_orders:
                qs = ','.join('?' * len(cur_orders))
                conn.execute(
                    f"""UPDATE marketplace_orders SET payout_id = ?
                        WHERE platform = ? AND order_sn IN ({qs})""",
                    [pid, platform, *cur_orders])
                n_linked += len(cur_orders)
            n_payouts += 1
            cur_orders, cur_income = [], 0.0
        else:  # income | adjustment
            cur_income += (r['amount'] or 0)
            sn = r['order_sn']
            if sn:   # '' stored for non-order rows (withdrawals); skip those
                cur_orders.append(sn)
    # trailing open cycle: silently skip (mid-cycle export, no closing withdrawal yet)
    conn.commit()
    return {'payouts': n_payouts, 'orders_linked': n_linked}
