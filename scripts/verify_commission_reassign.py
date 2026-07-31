#!/usr/bin/env python
"""Live-data checks for `commission_customer_reassign` (migration 143).

Not a unit test — the suite's coverage is hermetic on purpose so it does not
drift as payments get imported. This is the thing to run against the REAL DB
after adding or re-dating a rule, and before merging any change to the
attribution logic.

    ~/.virtualenvs/erp/bin/python scripts/verify_commission_reassign.py

Checks, in order of how much they would cost if wrong:

  1. CONSERVATION — for every month, total commission base with rules applied
     equals the total without them. A rule may only MOVE net between reps.
     A rise means a join fan-out (a line counted twice); a fall means a NULL
     leaked out of the COALESCE and lines vanished from every rep.

  2. NO RESTATED PAYOUTS — no active rule moves base out of a (rep, cycle) that
     already has a `commission_payouts` row. Reaching into a paid cycle turns it
     into an overpayment; that can be a deliberate choice, but never an accident.

  3. IMPACT — per (month, rep), the before/after split, so the money actually
     moved is visible rather than asserted.

Exits non-zero when 1 or 2 fail.
"""
from __future__ import annotations

import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
os.environ.setdefault("SECRET_KEY", "verify-script")
os.environ.setdefault("ADMIN_PASSWORD", "verify-script")

import commission  # noqa: E402
import commission_attribution  # noqa: E402
from config import DATABASE_PATH  # noqa: E402


def _months(conn):
    rows = conn.execute("""
        SELECT DISTINCT substr(date_iso, 1, 7) AS ym
          FROM received_payments
         WHERE cancelled = 0 AND date_iso >= '2026-01-01'
         ORDER BY ym
    """).fetchall()
    return [r[0] for r in rows]


def _base_by_rep(conn, ym, resolved):
    """Commission base per rep for one month, computed straight from SQL so it
    does not reuse the engine's own aggregation (independent of commission.py).

    `resolved=False` reproduces the pre-143 attribution by reading the raw
    Express-stamped code — that is the comparison baseline.
    """
    sp_expr = (commission_attribution.resolved_salesperson(
        "es.customer_code", "es.date_iso", "rcv.salesperson_code")
        if resolved else "rcv.salesperson_code")
    rows = conn.execute("""
        SELECT {sp} AS sp, ROUND(SUM(es.net), 2) AS net, COUNT(*) AS lines
          FROM (SELECT rp.salesperson AS salesperson_code, pi.doc_no AS invoice_no
                  FROM received_payments rp
                  JOIN paid_invoices pi ON pi.re_id = rp.id
                 WHERE rp.cancelled = 0 AND pi.doc_kind = 'IV'
                   AND pi.amount IS NOT NULL AND pi.amount <> 0
                   AND substr(rp.date_iso, 1, 7) = ?
                   AND COALESCE(rp.salesperson, '') <> ''
                 GROUP BY rp.salesperson, pi.doc_no) rcv
          JOIN sales_transactions es ON es.doc_base = rcv.invoice_no
         GROUP BY sp
    """.format(sp=sp_expr), (ym,)).fetchall()
    return {r["sp"]: (r["net"] or 0.0, r["lines"]) for r in rows}


def main():
    conn = sqlite3.connect("file:{}?mode=ro".format(DATABASE_PATH), uri=True)
    conn.row_factory = sqlite3.Row
    failures = []

    rules = conn.execute("""
        SELECT customer_code, to_salesperson, effective_from, is_active
          FROM commission_customer_reassign ORDER BY customer_code, effective_from
    """).fetchall()
    print("Active rules ({} total):".format(len(rules)))
    for r in rules:
        flag = "" if r["is_active"] else "  [INACTIVE]"
        print("  {:10} -> {:4} from {}{}".format(
            r["customer_code"], r["to_salesperson"], r["effective_from"], flag))
    if not rules:
        print("  (none — nothing to verify)")
        return 0

    # 1 / 3 — conservation + impact
    print("\n{:9}{:>14}{:>14}  {}".format("month", "net(before)", "net(after)", "moves"))
    for ym in _months(conn):
        before = _base_by_rep(conn, ym, resolved=False)
        after = _base_by_rep(conn, ym, resolved=True)
        tb = round(sum(v[0] for v in before.values()), 2)
        ta = round(sum(v[0] for v in after.values()), 2)
        lb = sum(v[1] for v in before.values())
        la = sum(v[1] for v in after.values())
        moves = []
        for rep in sorted(set(before) | set(after)):
            d = round(after.get(rep, (0.0, 0))[0] - before.get(rep, (0.0, 0))[0], 2)
            if abs(d) >= 0.005:
                moves.append("{}{:+.2f}".format(rep, d))
        print("{:9}{:14.2f}{:14.2f}  {}".format(ym, tb, ta, " ".join(moves) or "-"))
        if abs(tb - ta) >= 0.005:
            failures.append("{}: net not conserved {} -> {}".format(ym, tb, ta))
        if lb != la:
            failures.append("{}: line count {} -> {} (join fan-out)".format(ym, lb, la))

    # 2 — no rule restates an already-paid cycle
    print("\nPaid cycles touched by a rule:")
    # commission_payouts holds one row PER INVOICE, so joining it directly
    # reports the same cycle once per payout (2026-04 appeared twice, 224.64 +
    # 124.31, instead of once at 348.95). Collapse to distinct cycles first,
    # then total each — same fix as models.paid_cycles_affected_by_reassign.
    touched = conn.execute("""
        SELECT a.ym, a.losing_rep,
               (SELECT ROUND(SUM(cp.amount_paid), 2) FROM commission_payouts cp
                 WHERE cp.salesperson_code = a.losing_rep
                   AND cp.year_month = a.ym) AS amount_paid
          FROM (SELECT DISTINCT substr(rp.date_iso, 1, 7) AS ym,
                       rp.salesperson                     AS losing_rep
                  FROM commission_customer_reassign r
                  JOIN sales_transactions st ON st.customer_code = r.customer_code
                                            AND st.date_iso >= r.effective_from
                  JOIN paid_invoices pi ON pi.doc_no = st.doc_base
                                       AND pi.doc_kind = 'IV'
                                       AND pi.amount IS NOT NULL AND pi.amount <> 0
                  JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
                 WHERE r.is_active = 1
                   AND rp.salesperson <> r.to_salesperson
                   AND EXISTS (SELECT 1 FROM commission_payouts cp2
                                WHERE cp2.salesperson_code = rp.salesperson
                                  AND cp2.year_month = substr(rp.date_iso, 1, 7))
               ) a
         ORDER BY a.ym DESC
    """).fetchall()
    if touched:
        for t in touched:
            msg = ("{} rep {} already has a payout of {:.2f} but a rule moves base "
                   "out of that cycle".format(t["ym"], t["losing_rep"], t["amount_paid"]))
            print("  !! " + msg)
            failures.append(msg)
    else:
        print("  none — no active rule reaches into a cycle that was already paid")

    conn.close()
    print()
    if failures:
        print("FAILED ({} problem(s)):".format(len(failures)))
        for f in failures:
            print("  - " + f)
        return 1
    print("OK — base conserved, no already-paid cycle restated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
