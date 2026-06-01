"""Commission engine re-pointed to CANONICAL tables (sales_transactions +
received_payments + paid_invoices) instead of the stale express_* mirror.

express_sales / express_payments_in froze at 2026-04-30, so the engine could
not compute May-2026 commission. The canonical tables carry data through June.
A prior read-only audit proved them equivalent (express_sales ≡
sales_transactions 100% doc overlap; express_payments_in ≡ received_payments
cent-identical), so re-pointing must keep April identical while unlocking May.

These are integration anchors against the real data in the tmp_db copy:
  - April total commission must stay ฿10,098.07 (express/canonical parity).
  - sp 06 April must be ฿6,663.64 — guards the load-bearing NULL-amount filter
    on paid_invoices (171 'listed-but-unallocated' rows that would phantom-
    attribute +฿2,440 of 06-L's invoices to 06 without the filter).
  - May commission must compute > 0 (was 0 on the frozen express feed).
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


def test_april_total_matches_express_oracle(tmp_db):
    """Parity guard: re-pointing to canonical must not move April's total."""
    import commission

    rows = commission.get_commission_for_month("2026-04", db_path=tmp_db)
    assert abs(_total(rows) - 10098.07) < 0.10, (
        f"April total commission drifted to {_total(rows)} (oracle 10098.07)"
    )


def test_april_sp06_excludes_null_amount_phantom_links(tmp_db):
    """Load-bearing guard: paid_invoices NULL-amount rows must be filtered.
    Without the filter sp 06 inflates to 9103.64 (06-L's invoices leak in)."""
    import commission

    rows = commission.get_commission_for_month("2026-04", db_path=tmp_db)
    assert abs(_sp(rows, "06") - 6663.64) < 0.05, (
        f"sp 06 April = {_sp(rows, '06')} (expected 6663.64; 9103.64 means the "
        f"NULL-amount paid_invoices rows were not filtered)"
    )


def test_may_commission_computes_from_canonical(tmp_db):
    """RED driver: the frozen express feed returns nothing for May; the
    canonical re-point must surface real May commission for sp 06 (Tier A)."""
    import commission

    rows = commission.get_commission_for_month("2026-05", db_path=tmp_db)
    assert _sp(rows, "06") > 0, "sp 06 May commission must compute from canonical"
    assert abs(_sp(rows, "06") - 5940.32) < 0.05, (
        f"sp 06 May = {_sp(rows, '06')} (hand-verified oracle 5940.32)"
    )


def test_may_month_appears_in_payment_activity(tmp_db, monkeypatch):
    """The /commission month dropdown is built from
    _months_with_payment_activity, which must also read canonical receipts so
    May/June surface as selectable months."""
    import config
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_db)
    import importlib
    import app as app_mod
    importlib.reload(app_mod)

    months = app_mod._months_with_payment_activity()
    assert "2026-05" in months, "May must appear in the commission month dropdown"
    assert "2026-06" in months, "June must appear too"
