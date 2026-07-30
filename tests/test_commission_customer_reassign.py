"""Customer-level commission reassignment (migration 143).

Commission is attributed from `received_payments.salesperson` — the code
Express stamps on the RE document. When a rep stops servicing a customer,
Express keeps stamping the old code, so the departed rep keeps earning.
`commission_customer_reassign` overrides that per customer, keyed on the
INVOICE date ("he sold it, he earns it") so no already-paid cycle is restated.

Every test here is HERMETIC — it seeds its own scenario into `empty_db` rather
than reading live data, so these stay valid as real payments get imported.
The live-data reconciliation lives in
`scripts/verify_commission_reassign.py`, not in the suite.

Hand-verified scenario (`_seed`), one flat 5%/5% tier so commission == 5% of
net and the threshold math cannot mask a mistake:

  customer CUST-A ("ย้าย")  — rule: from 2026-05-10 -> rep '00'
    IV-A-EARLY  sold 2026-05-01, collected 2026-06-05, net 1000  -> stays '31'
    IV-A-LATE   sold 2026-05-20, collected 2026-06-06, net 2000  -> moves '00'
  customer CUST-B ("อยู่")   — no rule
    IV-B-1      sold 2026-05-15, collected 2026-06-07, net  500  -> stays '31'

  June cycle: rep 31 = (1000 + 500) * 5% = 75.00
              rep 00 = 2000 * 5%          =  0.00  (no tier assignment)
"""
from __future__ import annotations

import ast
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(REPO, "inventory_app")
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, APP)


def _sp(rows, code):
    return round(sum(r["total_commission"] for r in rows
                     if r["salesperson_code"] == code), 2)


def _net(rows, code):
    return round(sum(r["total_net"] for r in rows
                     if r["salesperson_code"] == code), 2)


def _seed(conn, rules=()):
    conn.execute(
        "INSERT INTO brands (id, code, name, is_own_brand) VALUES (1, 'T_OWN', 'ทดสอบ', 1)"
    )
    conn.execute("INSERT INTO products (id, product_name, brand_id) VALUES (1, 'สินค้าทดสอบ', 1)")
    conn.execute(
        "INSERT INTO salespersons (code, name) VALUES ('31', 'ทดสอบ 31'), ('00', 'บริษัท')"
    )
    conn.execute(
        "INSERT INTO commission_tiers (id, code, name_th, rate_own_pct, rate_third_pct, "
        "threshold_amount) VALUES (1, 'T_FLAT', 'เทียร์ทดสอบ', 5.0, 5.0, NULL)"
    )
    # Only rep 31 gets a tier. Rep 00 is deliberately left unassigned — that is
    # how "reassign to the company" yields zero commission with no special case.
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES ('31', 1, '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO customers (code, name, salesperson) VALUES "
        "('CUST-A', 'ลูกค้าย้าย', '31'), ('CUST-B', 'ลูกค้าอยู่', '31')"
    )
    conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) "
        "VALUES (1, 'RE-T-001', '2026-06-05', 'ลูกค้าย้าย', '31', 0), "
        "       (2, 'RE-T-002', '2026-06-06', 'ลูกค้าย้าย', '31', 0), "
        "       (3, 'RE-T-003', '2026-06-07', 'ลูกค้าอยู่', '31', 0)"
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES "
        "(1, 'IV-A-EARLY', 'IV', 1000.00), "
        "(2, 'IV-A-LATE',  'IV', 2000.00), "
        "(3, 'IV-B-1',     'IV',  500.00)"
    )
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit_price, net, total) VALUES "
        "('2026-05-01', 'IV-A-EARLY-1', 'IV-A-EARLY', 1, 'ลูกค้าย้าย', 'CUST-A', 10, 100.0, 1000.0, 1000.0), "
        "('2026-05-20', 'IV-A-LATE-1',  'IV-A-LATE',  1, 'ลูกค้าย้าย', 'CUST-A', 20, 100.0, 2000.0, 2000.0), "
        "('2026-05-15', 'IV-B-1-1',     'IV-B-1',     1, 'ลูกค้าอยู่', 'CUST-B',  5, 100.0,  500.0,  500.0)"
    )
    for cc, to_sp, eff, active in rules:
        conn.execute(
            "INSERT INTO commission_customer_reassign "
            "(customer_code, to_salesperson, effective_from, is_active) VALUES (?,?,?,?)",
            (cc, to_sp, eff, active),
        )
    conn.commit()


RULE_A = ("CUST-A", "00", "2026-05-10", 1)


# ── Baseline: without a rule nothing changes ────────────────────────────────
def test_without_rule_all_commission_stays_with_stamped_rep(empty_db, empty_db_conn):
    _seed(empty_db_conn)
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    assert _net(rows, "31") == 3500.00
    assert _sp(rows, "31") == 175.00, "1000+2000+500 @5% = 175.00"
    assert _net(rows, "00") == 0.00, "no rule seeded — nothing should move"


# ── Core behaviour ──────────────────────────────────────────────────────────
def test_rule_moves_only_orders_sold_on_or_after_effective_from(empty_db, empty_db_conn):
    """The load-bearing semantic: keyed on the INVOICE date, not the receipt.

    IV-A-EARLY was SOLD before the cut (2026-05-01 < 2026-05-10) but COLLECTED
    after it. It must stay with rep 31 — he sold it, he earns it. If this
    regresses to receipt-date keying, both A invoices move and rep 31 reads
    25.00 instead of 75.00.
    """
    _seed(empty_db_conn, rules=[RULE_A])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    assert _net(rows, "00") == 2000.00, "only IV-A-LATE (sold 2026-05-20) moves"
    assert _net(rows, "31") == 1500.00, "IV-A-EARLY (1000) + IV-B-1 (500) stay"
    assert _sp(rows, "31") == 75.00


def test_reassigned_revenue_earns_zero_commission(empty_db, empty_db_conn):
    """Rep 00 has no commission_assignments row, so the moved revenue is
    visible but earns nothing. No special-casing of '00' anywhere."""
    _seed(empty_db_conn, rules=[RULE_A])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    row00 = [r for r in rows if r["salesperson_code"] == "00"]
    assert row00, "reassigned revenue must still appear under the target rep"
    assert row00[0]["tier_code"] == "?"
    assert row00[0]["total_commission"] == 0.00


def test_effective_from_boundary_is_inclusive(empty_db, empty_db_conn):
    """A rule dated exactly on the invoice date applies to it."""
    _seed(empty_db_conn, rules=[("CUST-A", "00", "2026-05-01", 1)])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    assert _net(rows, "00") == 3000.00, "both A invoices move (2026-05-01 >= 2026-05-01)"


def test_inactive_rule_is_ignored(empty_db, empty_db_conn):
    _seed(empty_db_conn, rules=[("CUST-A", "00", "2026-05-10", 0)])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    assert _net(rows, "00") == 0.00
    assert _sp(rows, "31") == 175.00


def test_latest_applicable_rule_wins(empty_db, empty_db_conn):
    """Two rules on one customer: each order follows the latest rule at or
    before its own sale date, so a customer can change hands twice."""
    empty_db_conn.execute("INSERT INTO salespersons (code, name) VALUES ('02', 'ทดสอบ 02')")
    _seed(empty_db_conn, rules=[("CUST-A", "00", "2026-05-10", 1),
                                ("CUST-A", "02", "2026-05-18", 1)])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    assert _net(rows, "31") == 1500.00, "IV-A-EARLY sold before either rule"
    assert _net(rows, "02") == 2000.00, "IV-A-LATE sold 05-20 -> the 05-18 rule, not the 05-10 one"
    assert _net(rows, "00") == 0.00


def test_rule_for_other_customer_does_not_leak(empty_db, empty_db_conn):
    _seed(empty_db_conn, rules=[("CUST-B", "00", "2026-01-01", 1)])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    assert _net(rows, "00") == 500.00, "only CUST-B's invoice"
    assert _net(rows, "31") == 3000.00


def test_blank_customer_code_falls_through_to_stamped_rep(empty_db, empty_db_conn):
    """3 live sales rows carry an empty customer_code. They must keep the
    Express-stamped rep, never vanish into a NULL salesperson_code."""
    _seed(empty_db_conn, rules=[RULE_A])
    empty_db_conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) "
        "VALUES (9, 'RE-T-009', '2026-06-20', 'ไม่มีรหัส', '31', 0)"
    )
    empty_db_conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (9, 'IV-NOCODE', 'IV', 700.0)"
    )
    empty_db_conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit_price, net, total) VALUES "
        "('2026-06-01', 'IV-NOCODE-1', 'IV-NOCODE', 1, 'ไม่มีรหัส', '', 7, 100.0, 700.0, 700.0)"
    )
    empty_db_conn.commit()
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    codes = {r["salesperson_code"] for r in rows}
    assert None not in codes, "a NULL salesperson_code means COALESCE fell through"
    assert _net(rows, "31") == 2200.00, "1500 + the 700 blank-code invoice"


# ── The four sites that do NOT share _BASE_QUERY ────────────────────────────
def test_drilldown_lines_agree_with_dashboard_totals(empty_db, empty_db_conn):
    """`get_lines_for_salesperson` appends its own salesperson filter. If that
    filter reads the ORIGINAL code while the SELECT emits the resolved one, the
    dashboard and the drilldown disagree — the exact two-screens-one-click-apart
    failure this feature exists to avoid."""
    _seed(empty_db_conn, rules=[RULE_A])
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    for code in ("31", "00"):
        lines = commission.get_lines_for_salesperson("2026-06", code, db_path=empty_db)
        drill = round(sum(ln["line_net"] for ln in lines), 2)
        assert drill == _net(rows, code), (
            f"rep {code}: drilldown net {drill} != dashboard net {_net(rows, code)}"
        )
    moved = commission.get_lines_for_salesperson("2026-06", "00", db_path=empty_db)
    assert {ln["invoice_no"] for ln in moved} == {"IV-A-LATE"}
    stayed = commission.get_lines_for_salesperson("2026-06", "31", db_path=empty_db)
    assert {ln["invoice_no"] for ln in stayed} == {"IV-A-EARLY", "IV-B-1"}


def test_invoice_cycle_month_follows_the_reassignment(empty_db, empty_db_conn):
    """`get_invoice_cycle_month` runs its own query and decides which cycle a
    payout belongs to. After a remap it must answer for the NEW rep and stop
    answering for the old one, or recording a payout picks the wrong month."""
    _seed(empty_db_conn, rules=[RULE_A])
    import commission

    assert commission.get_invoice_cycle_month("00", "IV-A-LATE", db_path=empty_db) == "2026-06"
    assert commission.get_invoice_cycle_month("31", "IV-A-LATE", db_path=empty_db) is None
    assert commission.get_invoice_cycle_month("31", "IV-A-EARLY", db_path=empty_db) == "2026-06"


def test_line_breakdown_resolves_for_the_new_rep(empty_db, empty_db_conn):
    """`get_invoice_line_breakdown` looks the receipt up by `rp.salesperson`.
    Unpatched, drilling into a moved invoice as rep 00 finds no receipt."""
    _seed(empty_db_conn, rules=[RULE_A])
    import commission

    header, lines = commission.get_invoice_line_breakdown(
        "2026-06", "00", "IV-A-LATE", db_path=empty_db)
    assert header["receipt_no"] == "RE-T-002", (
        f"receipt must resolve for the reassigned rep, got {header['receipt_no']!r} "
        f"(empty means the rp.salesperson lookup was not resolved)"
    )
    assert header["customer_name"] == "ลูกค้าย้าย"
    assert round(header["total_net"], 2) == 2000.00
    # Rep 00 has no tier, so the moved revenue shows but earns nothing.
    assert header["tier_code"] == "?"
    assert header["total_commission"] == 0.00
    assert lines, "line detail must still render"


def test_all_invoices_view_follows_the_reassignment(empty_db, empty_db_conn):
    """`get_invoices_for_salesperson` is a display view, but it sits in
    commission.py and is reachable from the same screen flow — it must not
    contradict the commission pages."""
    _seed(empty_db_conn, rules=[RULE_A])
    import commission

    for_31 = commission.get_invoices_for_salesperson("2026-05", "31", db_path=empty_db)
    for_00 = commission.get_invoices_for_salesperson("2026-05", "00", db_path=empty_db)
    nos_31 = {r["doc_no"] for r in for_31}
    nos_00 = {r["doc_no"] for r in for_00}
    assert "IV-A-LATE" in nos_00, "moved invoice must show under the company"
    assert "IV-A-LATE" not in nos_31, "moved invoice must not also show under rep 31"
    assert "IV-A-EARLY" in nos_31, "pre-cut invoice stays with rep 31"


def test_reassignment_moves_net_without_creating_or_destroying_it(empty_db, empty_db_conn):
    """Conservation invariant: a rule may only MOVE commission base between
    reps. If total net across all reps changes, the resolution is duplicating
    lines (a join fan-out) or dropping them (a NULL leak) — both silent."""
    import commission

    _seed(empty_db_conn)
    before = commission.get_commission_for_month("2026-06", db_path=empty_db)
    total_before = round(sum(r["total_net"] for r in before), 2)

    empty_db_conn.execute(
        "INSERT INTO commission_customer_reassign "
        "(customer_code, to_salesperson, effective_from) VALUES ('CUST-A', '00', '2026-05-10')"
    )
    empty_db_conn.commit()
    after = commission.get_commission_for_month("2026-06", db_path=empty_db)
    total_after = round(sum(r["total_net"] for r in after), 2)

    assert total_after == total_before == 3500.00, (
        f"net not conserved: {total_before} -> {total_after}"
    )
    lines_before = sum(r["lines_attributed"] for r in before)
    lines_after = sum(r["lines_attributed"] for r in after)
    assert lines_after == lines_before, (
        f"line count changed {lines_before} -> {lines_after} — join fan-out"
    )


# ── AR page: per-receipt badge (blueprints/accounting.py) ───────────────────
def _receipt_badges(conn, customer_name):
    """Run the same resolution the AR page's recent-payments query uses."""
    import commission_attribution as ca

    sql = ("SELECT rp.re_no, " +
           ca.resolved_salesperson_for_receipt('rp.id', 'rp.salesperson') +
           " AS salesperson_code FROM received_payments rp "
           "WHERE rp.cancelled = 0 AND rp.customer = ? ORDER BY rp.re_no")
    return {r[0]: r[1] for r in conn.execute(sql, (customer_name,)).fetchall()}


def test_ar_receipt_badge_shows_the_company_after_reassignment(empty_db, empty_db_conn):
    """Express keeps stamping the departed rep's route code on the receipt, but
    the company is the one collecting now — the badge must say so."""
    _seed(empty_db_conn, rules=[RULE_A])
    badges = _receipt_badges(empty_db_conn, "ลูกค้าย้าย")
    assert badges["RE-T-002"] == "00", "receipt settling the moved invoice"
    assert badges["RE-T-001"] == "31", "receipt settling the pre-cut invoice stays with 31"
    assert _receipt_badges(empty_db_conn, "ลูกค้าอยู่")["RE-T-003"] == "31"


def test_ar_receipt_badge_falls_back_when_a_receipt_straddles_the_cut(empty_db, empty_db_conn):
    """One receipt settling an old AND a new invoice is ambiguous. It must fall
    back to the Express-stamped code, not silently pick a side."""
    _seed(empty_db_conn, rules=[RULE_A])
    empty_db_conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) "
        "VALUES (8, 'RE-T-008', '2026-06-25', 'ลูกค้าย้าย', '31', 0)"
    )
    empty_db_conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES "
        "(8, 'IV-STRADDLE-OLD', 'IV', 100.0), (8, 'IV-STRADDLE-NEW', 'IV', 100.0)"
    )
    empty_db_conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit_price, net, total) VALUES "
        "('2026-05-02', 'IV-STRADDLE-OLD-1', 'IV-STRADDLE-OLD', 1, 'ลูกค้าย้าย', 'CUST-A', 1, 100.0, 100.0, 100.0), "
        "('2026-05-25', 'IV-STRADDLE-NEW-1', 'IV-STRADDLE-NEW', 1, 'ลูกค้าย้าย', 'CUST-A', 1, 100.0, 100.0, 100.0)"
    )
    empty_db_conn.commit()

    assert _receipt_badges(empty_db_conn, "ลูกค้าย้าย")["RE-T-008"] == "31", (
        "an ambiguous receipt must fall back to the stamped rep"
    )


# ── Guard: creating a rule that restates an already-paid cycle ──────────────
def test_rule_reaching_into_a_paid_cycle_is_reported(empty_db, empty_db_conn):
    """A reassignment never rewrites `commission_payouts`, so back-dating a rule
    into a cycle that was already paid turns it into an overpayment. That may be
    deliberate, so it warns rather than blocks — but it must never be silent."""
    import models

    _seed(empty_db_conn)
    # Payouts are recorded PER INVOICE, so one cycle has many rows. The helper
    # must collapse them to one warning carrying the cycle total, not spray one
    # warning per payout row.
    empty_db_conn.execute(
        "INSERT INTO commission_payouts (year_month, salesperson_code, amount_paid, paid_date) "
        "VALUES ('2026-06', '31', 100.00, '2026-07-01'), "
        "       ('2026-06', '31',  50.00, '2026-07-01'), "
        "       ('2026-06', '31',  25.00, '2026-07-01')"
    )
    empty_db_conn.commit()

    hits = models.paid_cycles_affected_by_reassign(
        empty_db_conn, "CUST-A", "2026-05-01", "00")
    assert len(hits) == 1, (
        f"expected ONE warning for the paid June cycle, got {len(hits)}: {hits}"
    )
    assert hits[0]["year_month"] == "2026-06"
    assert hits[0]["losing_rep"] == "31"
    assert hits[0]["amount_paid"] == 175.00, "must be the cycle TOTAL (100+50+25)"

    # A rule dated after everything collected touches no paid cycle.
    assert models.paid_cycles_affected_by_reassign(
        empty_db_conn, "CUST-A", "2027-01-01", "00") == []


def test_effective_from_must_be_a_real_date(empty_db, empty_db_conn):
    """`effective_from` is string-compared against `date_iso`, so a well-shaped
    but impossible date would never match any invoice — the rule would sit in
    the list looking active while changing nothing."""
    import models

    _seed(empty_db_conn)
    for bad in ("2026-13-45", "2026-99-99", "2026-02-30", "not-a-date", ""):
        result = models.create_customer_reassignment(
            {"customer_code": "CUST-A", "to_salesperson": "00", "effective_from": bad})
        assert not result["ok"], f"{bad!r} should be rejected, got {result}"

    ok = models.create_customer_reassignment(
        {"customer_code": "CUST-A", "to_salesperson": "00", "effective_from": "2026-05-10"})
    assert ok["ok"], ok


# ── Cross-cutting coverage sweep ────────────────────────────────────────────
# Mechanical completeness is a build-time concern, not a review-time one. This
# fails the suite when a NEW site reads the Express-stamped rep code without
# either resolving it or being exempted WITH A WRITTEN REASON.
#
# An allowlist entry is a decision on the record. A silently-unguarded site is
# an oversight.
RAW_REP_READ = re.compile(r"rp\.salesperson\b|rcv\.salesperson_code\b")

# Every file in the app that reads the Express-stamped rep column. Adding a
# read anywhere else is exactly what this sweep is for — but a sweep only sees
# the files it is pointed at, so this list IS the coverage. It was found
# incomplete once already (models/commission.py shipped unscanned in the same
# PR that added the sweep), which is why the whole-repo assertion below exists.
SCANNED = (
    "commission.py",
    "blueprints/accounting.py",
    "models/commission.py",
)

# Keyed on the REPO-RELATIVE path, never the basename: `commission.py` and
# `models/commission.py` share a basename, so a basename key would let an
# exemption for one silently excuse a same-named function in the other.
EXEMPT = {
    ("commission.py", "_BASE_QUERY"):
        "Defines the resolution — the inner subquery legitimately reads the raw "
        "rp.salesperson before the outer SELECT resolves it.",
    ("blueprints/accounting.py", "express_ar_customer"):
        "AR page HEADER badge reads express_ar_outstanding, a re-stamped Express "
        "snapshot, and is deliberately out of scope (option C, Put 2026-07-30). "
        "The per-receipt badge in the same view IS resolved.",
    ("models/commission.py", "paid_cycles_affected_by_reassign"):
        "Must read the RAW stamped code on purpose: it answers 'which rep is "
        "currently credited and would LOSE base if this rule were created'. "
        "Resolving it would compare the rule against its own result and the "
        "already-paid-cycle warning would never fire.",
}


def _sites_reading_raw_rep(path):
    """Map {enclosing def-or-module-level name: source} for every top-level
    construct whose source mentions the raw Express-stamped rep column."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign)):
            continue
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not targets:
                continue
            name = targets[0]
        else:
            name = node.name
        seg = ast.get_source_segment(src, node) or ""
        if RAW_REP_READ.search(seg):
            out[name] = seg
    return out


def test_every_site_reading_the_raw_rep_code_is_resolved_or_exempt():
    for rel in SCANNED:
        path = os.path.join(APP, rel)
        assert os.path.exists(path), path
        for name, seg in _sites_reading_raw_rep(path).items():
            key = (rel, name)
            if key in EXEMPT:
                assert EXEMPT[key].strip(), f"{key} exempted without a reason"
                continue
            assert "commission_attribution" in seg or "resolved_salesperson" in seg, (
                f"{rel}::{name} reads the Express-stamped rep code without "
                f"resolving it through commission_attribution, and is not in EXEMPT. "
                f"Either resolve it, or add it to EXEMPT with a written reason."
            )


def test_scanned_list_covers_every_file_in_the_app_that_reads_the_raw_rep_code():
    """The sweep above only sees the files SCANNED points at. This walks the
    WHOLE app so a new module reading the Express-stamped rep code cannot slip
    in unnoticed — the failure mode that shipped `models/commission.py`
    unscanned in the very PR that introduced the sweep."""
    found = set()
    for root, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "instance", "static")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, APP)
            if rel == "commission_attribution.py":
                continue  # defines the resolution; its docstring names the column
            try:
                src = open(path, encoding="utf-8").read()
            except OSError:
                continue
            if RAW_REP_READ.search(src):
                found.add(rel)

    missing = found - set(SCANNED)
    assert not missing, (
        f"these files read the Express-stamped rep code but are NOT in SCANNED, "
        f"so the sweep never inspects them: {sorted(missing)}. Add them to "
        f"SCANNED (and exempt any legitimate raw read with a reason)."
    )


def test_exempt_keys_are_relative_paths_not_basenames():
    """`commission.py` and `models/commission.py` share a basename. Keying
    EXEMPT on the basename would let an exemption for one silently excuse a
    same-named function in the other."""
    for rel, _name in EXEMPT:
        assert rel in SCANNED, f"EXEMPT key {rel!r} is not a scanned relative path"
    basenames = [os.path.basename(r) for r in SCANNED]
    assert len(set(basenames)) < len(basenames), (
        "SCANNED no longer contains a basename collision — if that is "
        "deliberate this test can go, but until then it documents why the keys "
        "are relative paths."
    )


def test_sweep_detects_an_unguarded_site():
    """Proves the sweep above can actually fail — a guard that cannot go red is
    not a guard. Feeds it a synthetic module with an unresolved raw read."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
        fh.write('def leaky():\n    return "SELECT rp.salesperson FROM received_payments rp"\n')
        tmp = fh.name
    try:
        found = _sites_reading_raw_rep(tmp)
        assert "leaky" in found, "sweep failed to spot an unguarded raw read"
        assert "commission_attribution" not in found["leaky"]
    finally:
        os.unlink(tmp)
