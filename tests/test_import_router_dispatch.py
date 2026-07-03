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
    import config
    seen = {}
    monkeypatch.setattr(import_express, "run_import",
                        lambda ft, p, **kw: seen.update(ft=ft, path=p, dry=kw.get("dry_run"),
                                                        db_path=kw.get("db_path")))
    out = import_router.commit_file(PATH, rtype)
    assert seen["ft"] == express_kind and seen["path"] == PATH
    assert seen["dry"] is False           # commit, not preview
    # Regression (prod "unable to open database file"): the Express family must
    # commit against the CONFIGURED DB — config.DATABASE_PATH honours DATA_DIR —
    # NOT import_express's hard-coded inventory_app/instance/inventory.db, which
    # does not exist on the Railway container, so sqlite3.connect() raised
    # SQLITE_CANTOPEN on every AP/AR Express import via the web box.
    assert seen["db_path"] == config.DATABASE_PATH
    assert out["type"] == rtype and out["ok"] is True


def test_unknown_type_raises(monkeypatch):
    import import_router
    with pytest.raises(ValueError):
        import_router.commit_file(PATH, "unknown")


# ── preview_file (read-only) ──────────────────────────────────────────────
def test_preview_payments_in_counts_new_vs_existing_and_is_readonly(tmp_db, monkeypatch):
    import import_router, models, sqlite3
    conn = sqlite3.connect(tmp_db)
    existing_re = conn.execute("SELECT re_no FROM received_payments LIMIT 1").fetchone()[0]
    before = conn.execute("SELECT COUNT(*) FROM received_payments").fetchone()[0]
    conn.close()
    monkeypatch.setattr(models, "parse_payment_csv",
                        lambda p: [{"re_no": existing_re}, {"re_no": "RE-NEW-9999"}])
    out = import_router.preview_file(PATH, "payments_in", db_path=tmp_db)
    assert out["count"] == 2
    assert out["detail"] == {"new": 1, "existing": 1}
    conn = sqlite3.connect(tmp_db)
    after = conn.execute("SELECT COUNT(*) FROM received_payments").fetchone()[0]
    conn.close()
    assert after == before, "preview must not write"


def test_preview_sales_uses_preview_import(monkeypatch):
    import import_router, models
    import parse_weekly
    seen = {}
    monkeypatch.setattr(parse_weekly, "parse_sales", lambda p: ["e1", "e2", "e3"])
    monkeypatch.setattr(models, "preview_import",
                        lambda entries, ft: seen.update(ft=ft, n=len(entries)) or {"new": 3})
    out = import_router.preview_file(PATH, "sales")
    assert seen == {"ft": "sales", "n": 3} and out["count"] == 3


def test_preview_unknown_raises():
    import import_router
    with pytest.raises(ValueError):
        import_router.preview_file(PATH, "unknown")


def test_preview_credit_notes_ar_uses_readonly_preview(monkeypatch):
    """Regression: preview must route to the read-only preview_credit_notes_import
    (own conn + ROLLBACK), NOT the writing import_credit_notes — whose internal
    SAVEPOINT/RELEASE survives a manual rollback and leaks rows."""
    import import_router
    import import_credit_notes as icn
    seen = {}
    monkeypatch.setattr(icn, "preview_credit_notes_import",
                        lambda p, db_path=None: seen.update(called=True) or {"parsed": 4})

    def _boom(*a, **k):
        raise AssertionError("preview must not call the writing import_credit_notes")
    monkeypatch.setattr(icn, "import_credit_notes", _boom)

    out = import_router.preview_file(PATH, "credit_notes_ar")
    assert seen.get("called") is True and out["count"] == 4


# ── detect_express_report (title-line classifier) ──────────────────────────
def test_detect_express_report_classifies(tmp_path):
    """detect_express_report keys on the Express report title (cp874 header)."""
    import import_router

    def w(name, title):
        p = tmp_path / name
        p.write_text('"(BSN)บจก.บุญสวัสดิ์นำชัย"\n"  ' + title + '"\n', encoding='cp874')
        return str(p)

    d = import_router.detect_express_report
    assert d(w('ar.csv',   'รายงานลูกหนี้คงค้างแบบละเอียด')) == 'ar_snapshot'
    assert d(w('ap.csv',   'รายงานเจ้าหนี้คงค้างแบบละเอียด')) == 'ap_snapshot'
    assert d(w('s.csv',    'รายงานประวัติการขาย แยกตามลูกค้า')) == 'sales'
    assert d(w('p.csv',    'รายงานประวัติการซื้อ แยกตามผู้จำหน่าย')) == 'purchase'
    assert d(w('pin.csv',  'รายงานการรับชำระหนี้ เรียงตามวันที่')) == 'payments_in'
    assert d(w('pout.csv', 'รายงานการจ่ายชำระหนี้ เรียงตามวันที่')) == 'payments_out'
    assert d(w('cnar.csv', 'ใบลดหนี้ รับคืนสินค้า')) == 'credit_notes_ar'
    assert d(w('cnap.csv', 'ใบลดหนี้ ส่งคืนสินค้า')) == 'credit_notes_ap'
    assert d(w('x.csv',    'ขายเงินเชื่อ เรียงตามเลขที่')) == 'unknown'
