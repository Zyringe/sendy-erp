"""An import that SKIPS a billable line must say so, in words the operator reads.

Why this exists: `888ค8888` / ค่าขนส่ง is mapped with is_ignored=1, so
import_weekly drops those lines. That is correct for stock (a shipping fee is
not inventory) but it silently drops REVENUE with it — ฿592 of real delivery
income vanished this way over two years, ฿480 of it billed to named B2B trade
customers, and nobody noticed.

Two things made it invisible:
  * commit counted the skipped line in `skipped_dup` — literally reported as a
    DUPLICATE — while preview counted it under its own `ignored` bucket. The two
    disagreed, and the commit label actively misled ("duplicate row, fine").
  * `skipped_dup` has exactly ONE increment in the whole module: the is_ignored
    branch. It never counted duplicates at all (a true re-upload lands in
    `unchanged`), so the name was wrong from the start.

The fix does not change what is imported. It changes what the operator is told.
"""
import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

from tests import mapping_fixture  # noqa: E402

PID = 909101
IGN_CODE = "ZIGNFEE1"      # stands in for 888ค8888 / ค่าขนส่ง
OK_CODE = "ZOKPROD1"


def _entry(code, doc, net):
    return {"date_iso": "2026-05-09", "doc_no": doc, "line_seq": 1,
            "product_code_raw": code, "product_name_raw": "ค่าขนส่ง" if code == IGN_CODE else "ของจริง",
            "party": "C", "party_code": "C1", "qty": 1.0, "unit": "ใบ",
            "unit_price": net, "vat_type": 0, "discount": 0,
            "total": net, "net": net}


@pytest.fixture
def seeded(tmp_db, patch_models_conn):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    mapping_fixture.apply_mig124_if_needed(conn)
    mapping_fixture.reset_codes(conn, IGN_CODE, OK_CODE)
    conn.execute("INSERT INTO products (id,product_name,unit_type,sku_code,is_active) "
                 "VALUES (?,?,?,?,1)", (PID, "ของจริง", "ใบ", f"SK{PID}"))
    # the ignored one carries a product_id, exactly like 888ค8888 -> pid 1211
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,is_ignored,bsn_unit) "
                 "VALUES (?,?,?,1,'')", (IGN_CODE, "ค่าขนส่ง", PID))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,is_ignored,bsn_unit) "
                 "VALUES (?,?,?,0,'')", (OK_CODE, "ของจริง", PID))
    conn.commit()
    # A FRESH connection per call, not one shared object: preview_import closes
    # its connection in a `finally`, so a shared one would be dead by the time
    # import_weekly ran — a harness artifact, since production calls
    # get_connection() per invocation.
    def _factory():
        c = sqlite3.connect(tmp_db, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c
    patch_models_conn(_factory)
    yield tmp_db
    conn.close()


def test_ignored_lines_are_reported_separately_not_as_duplicates(seeded):
    stats = models.import_weekly(
        [_entry(OK_CODE, "IV9910001-1", 100.0), _entry(IGN_CODE, "IV9910001-2", 218.0)],
        "sales", "f.csv")
    assert stats["ignored"] == 1, "a skipped billable line needs its own count"
    assert stats.get("skipped_dup", 0) == 0, \
        "an ignored line is not a duplicate — that label is what hid ฿592"


def test_the_report_names_the_code_and_the_money_skipped(seeded):
    """A bare count is not actionable. The operator must be able to see WHAT was
    dropped and for HOW MUCH without opening the DB."""
    stats = models.import_weekly(
        [_entry(OK_CODE, "IV9910002-1", 100.0), _entry(IGN_CODE, "IV9910002-2", 218.0)],
        "sales", "f.csv")
    detail = stats["ignored_detail"]
    assert len(detail) == 1
    row = detail[0]
    assert row["bsn_code"] == IGN_CODE
    assert row["name"] == "ค่าขนส่ง"
    assert row["lines"] == 1
    assert row["net"] == 218.0


def test_preview_and_commit_agree_on_the_ignored_count(seeded):
    """They disagreed: preview counted `ignored`, commit counted `skipped_dup`."""
    entries = [_entry(OK_CODE, "IV9910003-1", 100.0), _entry(IGN_CODE, "IV9910003-2", 218.0)]
    plan = models.preview_import(entries, "sales")
    stats = models.import_weekly(entries, "sales", "f.csv")
    assert plan["ignored"] == stats["ignored"] == 1


def test_nothing_changes_about_what_actually_imports(seeded):
    """Control: this is a reporting fix. The ignored line must still be skipped,
    and the real line must still land."""
    models.import_weekly(
        [_entry(OK_CODE, "IV9910004-1", 100.0), _entry(IGN_CODE, "IV9910004-2", 218.0)],
        "sales", "f.csv")
    c = sqlite3.connect(seeded)
    codes = [r[0] for r in c.execute(
        "SELECT bsn_code FROM sales_transactions WHERE doc_base='IV9910004'")]
    c.close()
    assert codes == [OK_CODE], f"expected only the real line to import, got {codes}"


def test_no_ignored_lines_means_no_noise(seeded):
    """Control for the warning UI: a clean import must report nothing to explain."""
    stats = models.import_weekly([_entry(OK_CODE, "IV9910005-1", 100.0)], "sales", "f.csv")
    assert stats["ignored"] == 0
    assert stats["ignored_detail"] == []
