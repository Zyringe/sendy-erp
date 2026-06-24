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

    n_payouts = n_linked = n_unbalanced = 0
    cur_orders = []      # order_sns accumulated in the open cycle
    cur_income = 0.0     # inflow total in the open cycle
    for sort_time, is_withdrawal, amount, order_sns in _cycle_events(conn, platform):
        if is_withdrawal:
            wd = round(-amount, 2)
            diff = round(cur_income - wd, 2)
            status = 'reconciled' if abs(diff) <= _TOL else 'unbalanced'
            if status == 'unbalanced':
                n_unbalanced += 1
            pid = conn.execute(
                """INSERT INTO marketplace_payouts
                     (platform, deposit_date, amount, n_orders, status)
                   VALUES (?,?,?,?,?)""",
                (platform, str(sort_time)[:10], wd, len(cur_orders), status)).lastrowid
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
            cur_income += amount
            cur_orders.extend(order_sns)
    conn.commit()
    return {'payouts': n_payouts, 'orders_linked': n_linked, 'unbalanced': n_unbalanced}


def _cycle_events(conn, platform):
    """Ordered cycle events: (sort_time, is_withdrawal, amount, order_sns).
    A negative withdrawal closes a cycle; everything else is an inflow.

    Shopee: inflows are the per-order wallet income rows — their amount totals the
    cycle, and each carries its own order to link. (A positive-amount withdrawal is
    a reversal → an inflow.)

    Lazada: inflows are the per-statement (รอบบิล) SETTLEMENT amounts from the wallet
    Deposit rows — authoritative, equal to the bank withdrawal to the satang. The
    statement file's per-order income carries refund-timing noise vs the wallet, so
    it is used ONLY to map orders to their รอบบิล for linking, never to total the
    cycle. Orders therefore link to the deposit that swept their รอบบิล's settlement.
    """
    if platform == 'lazada':
        # Group the statement income by รอบบิล: orders to link, plus a fallback
        # income sum + earliest income time used only when that รอบบิล's wallet
        # settlement hasn't been imported yet (e.g. statement uploaded before the
        # wallet file). One inflow event per รอบบิล.
        stmt_orders, stmt_income, stmt_time = {}, {}, {}
        for r in conn.execute(
            """SELECT description AS stmt, order_sn, amount, txn_time FROM marketplace_wallet_txns
               WHERE platform='lazada' AND txn_type='income'"""):
            st = r['stmt']
            if r['order_sn']:
                stmt_orders.setdefault(st, []).append(r['order_sn'])
            stmt_income[st] = round(stmt_income.get(st, 0.0) + (r['amount'] or 0), 2)
            if st not in stmt_time or r['txn_time'] < stmt_time[st]:
                stmt_time[st] = r['txn_time']
        settle = {r['statement']: (r['settled_at'], r['amount']) for r in conn.execute(
            "SELECT statement, settled_at, amount FROM lazada_statement_settlement")}
        events = []
        for st in set(stmt_income) | set(settle):
            if st in settle:
                t, amt = settle[st]                      # authoritative wallet settlement
            else:
                t, amt = stmt_time[st], stmt_income[st]  # fallback: statement income
            events.append((t, False, amt, stmt_orders.get(st, [])))
        for r in conn.execute(
            """SELECT txn_time, amount FROM marketplace_wallet_txns
               WHERE platform='lazada' AND txn_type='withdrawal'"""):
            amt = r['amount'] or 0
            events.append((r['txn_time'], amt < 0, amt, []))
        # Settlement (~02:00) before withdrawal (~10:00) on a same-instant tie:
        # False(0) sorts before True(1). Python sort is stable otherwise.
        events.sort(key=lambda e: (e[0], e[1]))
        return events
    # Shopee: single time-ordered stream (id tie-break), original semantics.
    events = []
    for r in conn.execute(
        """SELECT txn_time, txn_type, order_sn, amount FROM marketplace_wallet_txns
           WHERE platform = ? ORDER BY txn_time ASC, id ASC""", (platform,)):
        amt = r['amount'] or 0
        is_wd = (r['txn_type'] == 'withdrawal' and amt < 0)
        sns = [r['order_sn']] if (not is_wd and r['order_sn']) else []
        events.append((r['txn_time'], is_wd, amt, sns))
    return events
