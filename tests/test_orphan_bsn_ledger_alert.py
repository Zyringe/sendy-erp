"""A hand-written ledger row that LOOKS like a BSN sync row is immortal.

WHY THIS EXISTS (2026-08-13, found by Put: "my ถุงหิ้ว stock is missing")
--------------------------------------------------------------------------
`models/imports.py` rebuilds a product's ledger by deleting `note IN ('BSN ขาย',
'BSN ขาย-คืน')` — an EXACT match, deliberately, so a sales re-import can never
wipe the same product's `BSN ซื้อ` rows (nothing would re-post them).

The cost of that correctness: any OTHER note starting with `BSN` survives every
re-import forever, while the real row next to it is deleted and re-created each
time. The 2026-06-16 kg→แพ็ค reconcile inserted three such rows
(`BSN re-added pack-basis (2.0 แพ็ค from sales_transactions)`), and from the next
import onward one 2-แพ็ค sale deducted 4. It ran undetected for two months and
cost 5 แพ็ค across pids 684 / 1373 / 1374.

Nothing in the app could see it: the ledger stayed internally consistent
(SUM == stock_levels), so every drift check passed. Only a comparison against
the SOURCE tables showed the gap. Hence a guard at the one moment the damage is
done — an import that re-posts a doc for a product carrying such a row.
"""
import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

from tests import mapping_fixture  # noqa: E402

PID = 909301
CODE = "ZORPHAN1"
DOC = "IV9930001-1"

# The note the 2026-06-16 reconcile actually wrote, verbatim.
ORPHAN_NOTE = "BSN re-added pack-basis (2.0 แพ็ค from sales_transactions)"


def _entry(doc, qty):
    return {"date_iso": "2026-06-15", "doc_no": doc, "line_seq": 1,
            "product_code_raw": CODE, "product_name_raw": "ถุงหิ้วทดสอบ",
            "party": "ร้านทดสอบ", "party_code": "99ท001", "qty": qty,
            "unit": "แพ็ค", "unit_price": 33.0, "vat_type": 0, "discount": 0,
            "total": 33.0 * qty, "net": 33.0 * qty}


@pytest.fixture
def seeded(tmp_db, patch_models_conn):
    """Force every row this test asserts on. `tmp_db` clones the LIVE dev DB
    WITH its data, so nothing here may be inherited."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    mapping_fixture.apply_mig124_if_needed(conn)
    mapping_fixture.reset_codes(conn, CODE)
    conn.execute("DELETE FROM transactions WHERE product_id=?", (PID,))
    conn.execute("DELETE FROM sales_transactions WHERE product_id=?", (PID,))
    conn.execute("DELETE FROM products WHERE id=?", (PID,))
    # The sweep is unscoped, and tmp_db is a copy of the LIVE dev DB — which
    # really does carry the three 2026-06-16 ถุงหิ้ว orphans. Inheriting them
    # would make "a clean import stays silent" fail for a reason that has
    # nothing to do with the code under test. Force the ledger clean.
    conn.execute(
        "DELETE FROM transactions"
        " WHERE (note LIKE 'BSN%' OR note LIKE 'ประวัติขาย%')"
        "   AND note NOT IN ('BSN ขาย','BSN ขาย-คืน','BSN ซื้อ','BSN ซื้อ-คืน')"
        "   AND note NOT LIKE 'ประวัติขาย (ไม่นับสต็อค):%'")
    conn.execute("INSERT INTO products (id,product_name,unit_type,sku_code,is_active)"
                 " VALUES (?,?,?,?,1)", (PID, "ถุงหิ้วทดสอบ", "แพ็ค", f"SK{PID}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,is_ignored,bsn_unit)"
                 " VALUES (?,?,?,0,'')", (CODE, "ถุงหิ้วทดสอบ", PID))
    conn.execute("DELETE FROM system_alerts")
    conn.commit()

    def _factory():
        c = sqlite3.connect(tmp_db, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c
    patch_models_conn(_factory)
    yield tmp_db
    conn.close()


def _seed_ledger_row(db, note, reference_no=DOC, change=-2):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO transactions"
              " (product_id,txn_type,quantity_change,unit_mode,reference_no,note,created_at)"
              " VALUES (?,'OUT',?,'unit',?,?,'2026-06-15 00:00:00')",
              (PID, change, reference_no, note))
    c.commit()
    c.close()


def _alerts(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM system_alerts WHERE resolved_at IS NULL"
        " AND kind='orphan_bsn_ledger' ORDER BY id")]
    c.close()
    return rows


def _notes(db):
    c = sqlite3.connect(db)
    rows = [r[0] for r in c.execute(
        "SELECT note FROM transactions WHERE product_id=? ORDER BY id", (PID,))]
    c.close()
    return rows


def test_an_orphan_bsn_row_colliding_with_a_resynced_doc_alerts(seeded):
    _seed_ledger_row(seeded, ORPHAN_NOTE)

    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    # CONTROL FIRST: the import must actually have posted its own row, or the
    # assertions below would pass on a code path that never ran.
    notes = _notes(seeded)
    assert notes.count("BSN ขาย") == 1, f"import did not post its own row: {notes}"
    assert ORPHAN_NOTE in notes, "the orphan is supposed to survive the re-import"

    a = _alerts(seeded)
    assert len(a) == 1, f"the double-deduction must reach /alerts, got {a}"
    assert a[0]["severity"] == "error", "stock is wrong RIGHT NOW, not merely at risk"
    assert DOC in a[0]["message"], "the message must name the document to check"
    assert "ถุงหิ้วทดสอบ" in a[0]["message"], "and the product"


def test_a_clean_product_raises_nothing(seeded):
    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    assert _notes(seeded).count("BSN ขาย") == 1, "control: the import ran"
    assert _alerts(seeded) == [], "a normal import must stay silent"


@pytest.mark.parametrize("note", [
    "BSN ขาย", "BSN ขาย-คืน", "BSN ซื้อ", "BSN ซื้อ-คืน",
    "ประวัติขาย (ไม่นับสต็อค): ถุงหิ้วทดสอบ",
])
def test_the_notes_a_sync_really_writes_are_never_flagged(seeded, note):
    """Pins the canonical set. If `_sync_bsn_to_stock` ever writes a new note
    and this list is not updated, the guard would alert on every import."""
    _seed_ledger_row(seeded, note, reference_no="IV9930099-1")

    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    assert _notes(seeded).count("BSN ขาย") >= 1, "control: the import ran"
    assert _alerts(seeded) == [], f"{note!r} is written by the sync itself"


def test_an_orphan_on_a_product_this_import_never_touched_still_alerts(seeded):
    """The coverage the unscoped sweep buys, and the reason it exists.

    A script that writes an orphan onto a product whose canonical row already
    exists does its damage with no import involved. Scoped to `affected_pids`
    that product would alert only if some later import happened to touch it.
    Here the import touches PID and nothing else, and the orphan sits on
    OTHER_PID — it must still be found."""
    OTHER_PID = 909302
    c = sqlite3.connect(seeded)
    c.execute("DELETE FROM products WHERE id=?", (OTHER_PID,))
    c.execute("INSERT INTO products (id,product_name,unit_type,sku_code,is_active)"
              " VALUES (?,?,?,?,1)", (OTHER_PID, "สินค้าที่ไม่ได้ import", "ตัว",
                                      f"SK{OTHER_PID}"))
    for note in (ORPHAN_NOTE, "BSN ขาย"):
        c.execute("INSERT INTO transactions"
                  " (product_id,txn_type,quantity_change,unit_mode,reference_no,note,created_at)"
                  " VALUES (?,'OUT',-2,'unit','IV9930055-1',?,'2026-06-15 00:00:00')",
                  (OTHER_PID, note))
    c.commit(); c.close()

    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    assert _notes(seeded).count("BSN ขาย") == 1, "control: the import ran on PID"
    a = _alerts(seeded)
    assert len(a) == 1, f"an untouched product's orphan must still be found, got {a}"
    assert f"#{OTHER_PID}" in a[0]["message"]
    assert a[0]["severity"] == "error", "its canonical row is already there"


def test_two_orphans_on_one_document_raise_two_alerts(seeded):
    """Codex review 2026-08-13 — why the dedupe key is transactions.id.

    Keyed on (product_id, reference_no) these two collapse into ONE alert,
    whose message names a single id to delete. Put deletes that one, marks the
    alert acknowledged, and the second orphan keeps double-deducting with
    nothing open to say so."""
    _seed_ledger_row(seeded, ORPHAN_NOTE)
    _seed_ledger_row(seeded, ORPHAN_NOTE)          # same product, same doc

    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    assert _notes(seeded).count("BSN ขาย") == 1, "control: the import ran"
    a = _alerts(seeded)
    assert len(a) == 2, f"each orphan ROW is its own incident, got {len(a)}: {a}"
    ids = {json.loads(x["context_json"])["transaction_id"] for x in a}
    assert len(ids) == 2, f"the two alerts must name different rows: {ids}"


def test_a_null_reference_orphan_is_reported_as_a_warning_not_a_false_error(seeded):
    """Codex review 2026-08-13 — why sibling matching uses `=`, not `IS`.

    Null-safe `IS` made a NULL-reference orphan match every unrelated NULL-
    reference canonical row on the same product, producing a false "stock is
    wrong NOW" error. With `=` it matches nothing.

    The NOT NULL guard belongs in the SUBQUERY, not the outer WHERE: the row
    must still be REPORTED (it is just as immortal), only downgraded to a
    warning. Putting it in the outer WHERE would hide it completely."""
    c = sqlite3.connect(seeded)
    c.execute("INSERT INTO transactions"
              " (product_id,txn_type,quantity_change,unit_mode,reference_no,note,created_at)"
              " VALUES (?,'OUT',-2,'unit',NULL,?,'2026-06-15 00:00:00')",
              (PID, ORPHAN_NOTE))
    # An unrelated canonical row, also NULL-referenced, on the same product.
    c.execute("INSERT INTO transactions"
              " (product_id,txn_type,quantity_change,unit_mode,reference_no,note,created_at)"
              " VALUES (?,'OUT',-9,'unit',NULL,'BSN ขาย','2026-06-01 00:00:00')",
              (PID,))
    c.commit(); c.close()

    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    a = _alerts(seeded)
    assert len(a) == 1, f"the NULL-reference orphan must still be reported: {a}"
    assert a[0]["severity"] == "warning", \
        "nothing shares its (null) reference — calling it a live double-deduction is false"


def test_an_orphan_with_no_matching_doc_is_a_warning_not_an_error(seeded):
    """No canonical row shares its reference_no, so nothing is double-counted
    yet — but the row is still immortal and will collide the day that doc is
    re-imported. Worth surfacing, not worth crying about."""
    _seed_ledger_row(seeded, ORPHAN_NOTE, reference_no="IV9930077-1")

    models.import_weekly([_entry(DOC, 2.0)], "sales", "wk.csv")

    assert _notes(seeded).count("BSN ขาย") == 1, "control: the import ran"
    a = _alerts(seeded)
    assert len(a) == 1, f"an immortal row is still worth reporting, got {a}"
    assert a[0]["severity"] == "warning"
