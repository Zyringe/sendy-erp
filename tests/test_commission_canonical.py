"""Commission engine anchored to the CANONICAL tables (sales_transactions +
received_payments + paid_invoices) instead of the stale express_* mirror.

express_sales / express_payments_in froze at 2026-04-30; the canonical tables
carry live data through the current month, so the engine reads canonical. The
original express↔canonical parity (April == ฿10,098.07) held only at migration
time — canonical legitimately diverges as more payments are imported, so that
frozen-parity assertion is retired in favour of the live anchors below.

⚠️ LIVE-DATA anchors: the tmp_db fixture copies the real inventory.db, so these
totals MOVE when commission-affecting data changes (payments imported,
commission_overrides edited). They are NOT frozen oracles. When one fails, first
confirm it is data drift (not an engine regression), then re-verify and update
the constant — or pin the suite to a frozen fixture DB (the durable fix).

Last re-verified 2026-06-03 against live data:
  - April 2026 total commission ฿12,814.19 — known-good anchor; catches an
    engine regression that moves April's total between data refreshes.
  - sp 06 April ฿6,636.76 — guards the load-bearing NULL-amount filter on
    paid_invoices: without it, 06-L's 'listed-but-unallocated' invoices leak in
    and inflate sp 06 well above this value (06-L is correctly a separate line).
  - sp 06 May ฿4,935.32 = ฿5,940.32 (tier-only) − ฿1,005.00, the ฿5/piece
    cutting-disc overrides (commission_overrides id 1/7/8/9 = pid 394/395/396/398,
    added 2026-06-02). Confirms May surfaces from canonical AND that the
    cutting-disc override rule is applied.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


def _total(rows):
    return round(sum(r["total_commission"] for r in rows), 2)


def _sp(rows, code):
    return round(sum(r["total_commission"] for r in rows
                     if r["salesperson_code"] == code), 2)


def test_april_total_anchor(tmp_db):
    """Known-good anchor on April's total commission (live-data — see module
    docstring). Catches an engine regression that moves April's total; after a
    legitimate data/override change this will mismatch — re-verify and refresh."""
    import commission

    rows = commission.get_commission_for_month("2026-04", db_path=tmp_db)
    assert abs(_total(rows) - 12814.19) < 0.10, (
        f"April total commission = {_total(rows)} (anchor 12814.19; if the "
        f"underlying data legitimately changed, re-verify and update this)"
    )


def test_april_sp06_excludes_null_amount_phantom_links(tmp_db):
    """Load-bearing guard: paid_invoices NULL-amount rows must be filtered.
    Without the filter, 06-L's 'listed-but-unallocated' invoices leak into sp 06
    and inflate it well above this anchor. The filter logic is what this guards;
    the absolute value is live-data (see module docstring)."""
    import commission

    rows = commission.get_commission_for_month("2026-04", db_path=tmp_db)
    assert abs(_sp(rows, "06") - 6636.76) < 0.05, (
        f"sp 06 April = {_sp(rows, '06')} (anchor 6636.76; a large jump means "
        f"the NULL-amount paid_invoices rows were not filtered)"
    )


def test_may_commission_computes_from_canonical(tmp_db):
    """May must surface from canonical (was 0 on the frozen express feed) AND
    reflect the ฿5/piece cutting-disc overrides: ฿4,935.32 = ฿5,940.32 tier-only
    − ฿1,005.00 (overrides id 1/7/8/9). See module docstring (live-data)."""
    import commission

    rows = commission.get_commission_for_month("2026-05", db_path=tmp_db)
    assert _sp(rows, "06") > 0, "sp 06 May commission must compute from canonical"
    assert abs(_sp(rows, "06") - 4935.32) < 0.05, (
        f"sp 06 May = {_sp(rows, '06')} (anchor 4935.32 = 5940.32 tier-only "
        f"− 1005.00 cutting-disc override; see module docstring)"
    )


def test_may_month_appears_in_payment_activity(tmp_db, monkeypatch):
    """The /commission month dropdown is built from
    _months_with_payment_activity, which must also read canonical receipts so
    May/June surface as selectable months."""
    import config
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_db)
    import importlib
    import blueprints.commission_bp as commission_bp_mod
    importlib.reload(commission_bp_mod)

    months = commission_bp_mod._months_with_payment_activity()
    assert "2026-05" in months, "May must appear in the commission month dropdown"
    assert "2026-06" in months, "June must appear too"
