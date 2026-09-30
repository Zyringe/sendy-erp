#!/usr/bin/env python
"""Invariant oracle for `cashbook_transactions` (card F, plan §6). Read-only.

    python scripts/audit_cashbook_invariants.py [--db PATH] [--json]

Each invariant lists its violators; the script exits 1 when any invariant has a
violator that `EXPECTED` does not pin, else 0. I1 is a digest of the balances
(per account, month, direction: SUM and COUNT) to compare before/after a deploy.

stdlib only (prod has no pandas and no sqlite3 CLI), so it runs over
`railway ssh` with `/opt/venv/bin/python` from anywhere, with no app modules on
the path. So the category names, the payout target algorithm, the mirror's
conflict predicate and the date regex are copied here;
tests/test_audit_cashbook_invariants.py pins each copy equal to the app's.

Opened `mode=ro`: a read-only connection cannot write or checkpoint.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

ADVANCE_CATEGORY = "เงินเดือน (เบิกล่วงหน้า)"
SALARY_CATEGORY = "เงินเดือน"
COMMISSION_CATEGORY = "จ่ายค่าคอมมิชชั่น"
PAYOUT_CATEGORY = "ยอดขายของ"
PAYOUT_CREATED_BY = "ระบบ"
MIN_DEPOSIT_DATE = "2026-01-01"
PLATFORM_ACCOUNT_CODE = {"lazada": "LEX", "shopee": "SPX"}
CONFLICT_WINDOW_DAYS = 2
ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
ISO_DATE_RE = re.compile(ISO_DATE_PATTERN)

# Manual `เงินเดือน` rows with no link: salary keyed by hand before ADR 0006
# (per-employee pay-event). Measured on PROD 2026-09-30; pinned so a NEW one
# goes red while the history stays visible as a count.
EXPECTED = {
    "I14": frozenset({297, 299, 300, 301, 323, 324, 325, 326, 330, 350, 351, 353, 388,
                      407, 408, 409, 410, 418, 419, 443, 444, 445, 475, 476, 477, 507,
                      508, 509, 510}),
}
# Where and when each pin was read. EXPECTED holds PROD ids: the dev copy
# happens to share them, which does not make the pin env-neutral. Printed with
# every report so a stale pin is visible (PR-1 review N3).
EXPECTED_READ = {"I14": {"env": "PROD", "read": "2026-09-30", "count": 29}}

_UNLINKED = ("t.payroll_item_id IS NULL AND t.salary_advance_id IS NULL "
             "AND t.commission_payout_id IS NULL AND t.payout_platform IS NULL")


def _default_db():
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "inventory.db")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "inventory_app", "instance", "inventory.db")


def connect_ro(path):
    if not os.path.exists(path):
        raise SystemExit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _ids(prefix, rows):
    return [f"{prefix}:{r[0]}" for r in rows]


def _platform_account(conn, platform):
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code = ? AND is_active = 1",
                       (PLATFORM_ACCOUNT_CODE[platform],)).fetchone()
    return row[0] if row else None


def payout_target(conn, platform):
    """cashbook_ledger.payout_target, copied (stdlib-only)."""
    seen, target = {}, {}
    for r in conn.execute(
        """SELECT deposit_date, amount, n_orders FROM marketplace_payouts
            WHERE platform = ? AND deposit_date >= ?
            ORDER BY deposit_date, id""",
        (platform, MIN_DEPOSIT_DATE),
    ):
        amt = round(r["amount"], 2)
        key2 = (r["deposit_date"], amt)
        occurrence = seen.get(key2, 0) + 1
        seen[key2] = occurrence
        target[(r["deposit_date"], amt, occurrence)] = r["n_orders"]
    return target


def conflicting_manual_row_exists(conn, account_id, deposit_date, amount):
    """cashbook_payout_mirror._conflicting_manual_row_exists, copied."""
    row = conn.execute(
        """SELECT 1 FROM cashbook_transactions
            WHERE account_id = ? AND direction = 'income' AND payout_platform IS NULL
              AND ROUND(amount, 2) = ROUND(?, 2)
              AND ABS(julianday(txn_date) - julianday(?)) <= ?
            LIMIT 1""",
        (account_id, amount, deposit_date, CONFLICT_WINDOW_DAYS),
    ).fetchone()
    return row is not None


# ── invariants: each returns (value text, [violators]) ──────────────────────

def i1_balances(conn):
    rows = conn.execute(
        """SELECT account_id, substr(txn_date, 1, 7) AS ym, direction,
                  COUNT(*) AS n, SUM(amount) AS s
             FROM cashbook_transactions GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""").fetchall()
    digest = hashlib.sha256(json.dumps(
        [[r["account_id"], r["ym"], r["direction"], r["n"], repr(r["s"])] for r in rows],
        ensure_ascii=False).encode()).hexdigest()
    total = sum(r["n"] for r in rows)
    return f"{total} rows in {len(rows)} groups, sha256 {digest[:16]}", []


def i2_one_link(conn):
    rows = conn.execute(
        """SELECT id FROM cashbook_transactions
            WHERE (payroll_item_id IS NOT NULL) + (salary_advance_id IS NOT NULL)
                + (commission_payout_id IS NOT NULL) + (payout_platform IS NOT NULL) > 1
            ORDER BY id""").fetchall()
    return str(len(rows)), _ids("txn", rows)


def i3_foreign_keys(conn):
    out = []
    for table in ("cashbook_transactions", "salary_advances", "commission_payouts"):
        for r in conn.execute(f"PRAGMA foreign_key_check({table})").fetchall():
            out.append(f"{r[0]}:{r[1]}->{r[2]}")
    return str(len(out)), out


def i4_advance_pairs(conn):
    orphan_adv = conn.execute(
        """SELECT sa.id FROM salary_advances sa
            WHERE NOT EXISTS (SELECT 1 FROM cashbook_transactions t
                               WHERE t.salary_advance_id = sa.id) ORDER BY sa.id""").fetchall()
    orphan_txn = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
            WHERE t.salary_advance_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM salary_advances sa WHERE sa.id = t.salary_advance_id)
            ORDER BY t.id""").fetchall()
    return (f"{len(orphan_adv)} / {len(orphan_txn)}",
            _ids("advance", orphan_adv) + _ids("txn", orphan_txn))


def i5_salary_rows(conn):
    rows = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
             LEFT JOIN payroll_items pi ON pi.id = t.payroll_item_id
             LEFT JOIN payroll_runs r ON r.id = t.payroll_run_id
            WHERE t.payroll_item_id IS NOT NULL
              AND (pi.id IS NULL OR r.id IS NULL OR t.payroll_run_id IS NOT pi.run_id
                   OR r.status IS NOT 'finalized')
            ORDER BY t.id""").fetchall()
    return str(len(rows)), _ids("txn", rows)


def i6_mirror_drift(conn):
    parts, out = [], []
    for platform in sorted(PLATFORM_ACCOUNT_CODE):
        account_id = _platform_account(conn, platform)
        target = payout_target(conn, platform)
        if account_id is None:
            parts.append(f"{platform}: no active account")
            if target:
                out.append(f"{platform}:no-account")
            continue
        existing = {}
        for r in conn.execute(
            """SELECT id, payout_deposit_date, payout_amount, payout_occurrence
                 FROM cashbook_transactions WHERE account_id = ? AND payout_platform = ?""",
            (account_id, platform)):
            existing[(r[1], round(r[2], 2), r[3])] = r[0]
        skipped = [k for k in target if k not in existing
                   and conflicting_manual_row_exists(conn, account_id, k[0], k[1])]
        expected = len(target) - len(skipped)
        parts.append(f"{platform} {len(existing)} = {expected}")
        for k in target:
            if k not in existing and k not in skipped:
                out.append(f"{platform}:missing:{k[0]}/{k[1]}/{k[2]}")
        for k, txn in existing.items():
            if k not in target:
                out.append(f"txn:{txn}")
    return ", ".join(parts), out


def i6b_payouts_positive(conn):
    rows = conn.execute("SELECT id FROM marketplace_payouts WHERE amount <= 0 ORDER BY id").fetchall()
    return str(len(rows)), _ids("marketplace_payout", rows)


def i7_commission_links(conn):
    total = conn.execute("SELECT COUNT(*) FROM cashbook_transactions "
                         "WHERE commission_payout_id IS NOT NULL").fetchone()[0]
    dangling = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
            WHERE t.commission_payout_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM commission_payouts cp WHERE cp.id = t.commission_payout_id)
            ORDER BY t.id""").fetchall()
    dup = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
            WHERE t.commission_payout_id IN (
                  SELECT commission_payout_id FROM cashbook_transactions
                   WHERE commission_payout_id IS NOT NULL
                   GROUP BY commission_payout_id HAVING COUNT(*) > 1)
            ORDER BY t.id""").fetchall()
    return f"{total - len(dangling)} / {total}", _ids("txn", dangling) + _ids("txn", dup)


def i8_shape(conn):
    bad_date, bad_amount = [], []
    for r in conn.execute("SELECT id, txn_date, amount FROM cashbook_transactions ORDER BY id"):
        if not (isinstance(r[1], str) and ISO_DATE_RE.match(r[1])):
            bad_date.append(r[0])
        amount = r[2]
        if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            bad_amount.append(r[0])
    ids = sorted(set(bad_date) | set(bad_amount))
    return f"{len(bad_date)} / {len(bad_amount)}", [f"txn:{i}" for i in ids]


def i9_category_row(conn):
    rows = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
            WHERE NOT EXISTS (SELECT 1 FROM cashbook_categories c
                               WHERE c.name = t.category AND c.direction = t.direction)
            ORDER BY t.id""").fetchall()
    return str(len(rows)), _ids("txn", rows)


def i10_salary_equality(conn):
    rows = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
             JOIN payroll_items pi ON pi.id = t.payroll_item_id
            WHERE t.amount != pi.net_pay OR t.category IS NOT ? OR t.direction != 'expense'
            ORDER BY t.id""", (SALARY_CATEGORY,)).fetchall()
    return str(len(rows)), _ids("txn", rows)


def i11_advance_equality(conn):
    rows = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
             JOIN salary_advances sa ON sa.id = t.salary_advance_id
            WHERE t.amount != sa.amount OR t.txn_date != sa.advance_date
               OR t.account_id IS NOT sa.from_account_id
               OR t.category IS NOT ? OR t.direction != 'expense'
            ORDER BY t.id""", (ADVANCE_CATEGORY,)).fetchall()
    return str(len(rows)), _ids("txn", rows)


def i12_commission_equality(conn):
    rows = conn.execute(
        """SELECT t.id FROM cashbook_transactions t
             JOIN commission_payouts cp ON cp.id = t.commission_payout_id
            WHERE t.amount != cp.amount_paid OR t.txn_date != cp.paid_date
               OR t.category IS NOT ? OR t.direction != 'expense'
            ORDER BY t.id""", (COMMISSION_CATEGORY,)).fetchall()
    return str(len(rows)), _ids("txn", rows)


def i13_payout_equality(conn):
    targets, accounts, out = {}, {}, []
    for r in conn.execute(
        """SELECT id, account_id, txn_date, direction, category, amount, created_by,
                  payout_platform, payout_deposit_date, payout_amount, payout_occurrence
             FROM cashbook_transactions WHERE payout_platform IS NOT NULL ORDER BY id"""):
        platform = r["payout_platform"]
        if platform not in PLATFORM_ACCOUNT_CODE:
            out.append(f"txn:{r['id']}")
            continue
        if platform not in targets:
            targets[platform] = payout_target(conn, platform)
            accounts[platform] = _platform_account(conn, platform)
        key = (r["payout_deposit_date"], round(r["payout_amount"], 2), r["payout_occurrence"])
        if (r["txn_date"] != r["payout_deposit_date"] or r["amount"] != r["payout_amount"]
                or r["account_id"] != accounts[platform] or r["category"] != PAYOUT_CATEGORY
                or r["direction"] != "income" or r["created_by"] != PAYOUT_CREATED_BY
                or key not in targets[platform]):
            out.append(f"txn:{r['id']}")
    return str(len(out)), out


def i14_manual_purity(conn):
    adv = conn.execute(
        f"SELECT t.id FROM cashbook_transactions t WHERE {_UNLINKED} AND t.category = ? "
        "ORDER BY t.id", (ADVANCE_CATEGORY,)).fetchall()
    sal = [r[0] for r in conn.execute(
        f"SELECT t.id FROM cashbook_transactions t WHERE {_UNLINKED} AND t.category = ? "
        "ORDER BY t.id", (SALARY_CATEGORY,))]
    pinned = EXPECTED["I14"]
    return (f"{len(adv)} / {len([i for i in sal if i in pinned])} of {len(pinned)} pinned"
            f" + {len([i for i in sal if i not in pinned])} unpinned",
            _ids("txn", adv) + [f"txn:{i}" for i in sal])


def i15_commission_resubmit(conn):
    groups = {}
    for r in conn.execute(
        """SELECT cp.id, cp.salesperson_code, cp.invoice_no, cp.year_month, cp.amount_paid,
                  cp.paid_date
             FROM commission_payouts cp
            WHERE EXISTS (SELECT 1 FROM cashbook_transactions t
                           WHERE t.commission_payout_id = cp.id)
            ORDER BY cp.id"""):
        groups.setdefault(tuple(r[1:]), []).append(r[0])
    out = [f"commission_payout:{i}" for ids in groups.values() if len(ids) > 1 for i in ids]
    return str(len([g for g in groups.values() if len(g) > 1])), out


INVARIANTS = (
    ("I1", "balances (digest)", i1_balances),
    ("I2", "one link", i2_one_link),
    ("I3", "foreign keys", i3_foreign_keys),
    ("I4", "advance pairs (orphan advances / orphan rows)", i4_advance_pairs),
    ("I5", "salary rows on their finalized run", i5_salary_rows),
    ("I6", "mirror drift (rows = expected)", i6_mirror_drift),
    ("I6b", "payouts > 0", i6b_payouts_positive),
    ("I7", "commission links (resolved / total)", i7_commission_links),
    ("I8", "shape (bad date / bad amount)", i8_shape),
    ("I9", "category row exists", i9_category_row),
    ("I10", "salary equality", i10_salary_equality),
    ("I11", "advance equality", i11_advance_equality),
    ("I12", "commission equality", i12_commission_equality),
    ("I13", "payout equality", i13_payout_equality),
    ("I14", "manual purity (advance-category / salary pinned + unpinned)", i14_manual_purity),
    ("I15", "commission resubmit groups", i15_commission_resubmit),
)


def audit(conn):
    results = []
    for key, name, fn in INVARIANTS:
        value, violators = fn(conn)
        pinned = EXPECTED.get(key, frozenset())
        unexpected = [v for v in violators
                      if not (v.startswith("txn:") and int(v[4:]) in pinned)]
        results.append({"id": key, "name": name, "value": value,
                        "violators": violators, "unexpected": unexpected,
                        "ok": not unexpected})
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=_default_db())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    conn = connect_ro(args.db)
    try:
        mig = conn.execute("SELECT MAX(filename), COUNT(*) FROM applied_migrations").fetchone()
        results = audit(conn)
    finally:
        conn.close()
    report = {"db": os.path.abspath(args.db),
              "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "migrations": {"max": mig[0], "count": mig[1]},
              "pins": EXPECTED_READ,
              "invariants": results}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"db {report['db']} · read {report['read_at_utc']} · "
              f"migrations {mig[0]} ({mig[1]})")
        for key, pin in EXPECTED_READ.items():
            print(f"pin {key}: {pin['count']} ids read on {pin['env']} {pin['read']}")
        for r in results:
            flag = "ok " if r["ok"] else "BAD"
            extra = f"  unexpected: {' '.join(r['unexpected'][:20])}" if r["unexpected"] else ""
            print(f"{flag} {r['id']:<4} {r['name']:<62} {r['value']}{extra}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
