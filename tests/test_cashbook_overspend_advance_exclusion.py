"""Phase 2 — the advance category is excluded from the dashboard overspend
flags (plan.md finding #5). Advances are lumpy (a big one month, none the next),
so a month-over-month jump is normal, not an operating overspend. They still
count in the P&L / category summary — only the FLAG is suppressed.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from datetime import date

from blueprints.cashbook import _overspend_flags, ADVANCE_CATEGORY


def test_advance_category_never_overspend_flagged(tmp_db_conn):
    conn = tmp_db_conn
    acct = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0 ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    # prev month ฿100, this month ฿5,000 — would trip the flag (>=+20% and >=฿1,000)
    conn.execute(
        "INSERT INTO cashbook_transactions (account_id, txn_date, direction, category, amount)"
        " VALUES (?, '2026-04-15', 'expense', ?, 100)", (acct, ADVANCE_CATEGORY))
    conn.execute(
        "INSERT INTO cashbook_transactions (account_id, txn_date, direction, category, amount)"
        " VALUES (?, '2026-05-15', 'expense', ?, 5000)", (acct, ADVANCE_CATEGORY))
    conn.commit()

    flags = _overspend_flags(conn, "2026-05", today=date(2026, 7, 1))  # 2026-05 fully past
    cats = [f["category"] for f in flags]
    assert ADVANCE_CATEGORY not in cats, "advance category must not appear in overspend flags"
