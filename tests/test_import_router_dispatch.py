"""Unified import box — commit dispatch routing.

commit_file(path, report_type) routes each detected report to its canonical
importer. The importers themselves are already tested elsewhere; here we lock
that the DISPATCH sends each type to the right one with the right args, and
that sales/payments_in go to the CANONICAL importers (never the express twins).
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))

PATH = "/tmp/whatever.csv"


def test_payments_in_routes_to_models_import_payments(monkeypatch):
    import import_router, models
    seen = {}
    monkeypatch.setattr(models, "import_payments",
                        lambda p: seen.update(path=p) or {"imported": 5})
    out = import_router.commit_file(PATH, "payments_in")
    assert seen["path"] == PATH
    assert out["ok"] is True and out["type"] == "payments_in"


def test_credit_notes_ar_routes_to_import_credit_notes(monkeypatch):
    import import_router
    import import_credit_notes as icn
    seen = {}
    monkeypatch.setattr(icn, "import_credit_notes",
                        lambda p, db_path=None: seen.update(path=p) or {"parsed": 3})
    out = import_router.commit_file(PATH, "credit_notes_ar")
    assert seen["path"] == PATH and out["type"] == "credit_notes_ar"


def test_sales_routes_to_import_weekly_canonical(monkeypatch):
    """sales must go through parse_weekly → models.import_weekly (sales_transactions),
    NEVER parse_express_sales (express_sales twin)."""
    import import_router, models
    import parse_weekly
    seen = {}
    monkeypatch.setattr(parse_weekly, "parse_sales", lambda p: ["e1", "e2"])
    monkeypatch.setattr(models, "import_weekly",
                        lambda entries, kind, fn: seen.update(kind=kind, n=len(entries)) or {"inserted": 2})
    out = import_router.commit_file(PATH, "sales", filename="ขาย_x.csv")
    assert seen["kind"] == "sales" and seen["n"] == 2 and out["ok"] is True


@pytest.mark.parametrize("rtype,express_kind", [
    ("payments_out", "payments_out"),
    ("credit_notes_ap", "credit_notes"),
    ("ar_snapshot", "ar_snapshot"),
    ("ap_snapshot", "ap_snapshot"),
])
def test_express_family_routes_to_express_importer(monkeypatch, rtype, express_kind):
    import import_router
    import import_express
    seen = {}
    monkeypatch.setattr(import_express, "run_import",
                        lambda ft, p, **kw: seen.update(ft=ft, path=p, dry=kw.get("dry_run")))
    out = import_router.commit_file(PATH, rtype)
    assert seen["ft"] == express_kind and seen["path"] == PATH
    assert seen["dry"] is False           # commit, not preview
    assert out["type"] == rtype and out["ok"] is True


def test_unknown_type_raises(monkeypatch):
    import import_router
    with pytest.raises(ValueError):
        import_router.commit_file(PATH, "unknown")
