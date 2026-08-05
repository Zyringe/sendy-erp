"""A skipped billable line must reach Put, not just whoever ran the import.

PR #364 surfaced skipped `is_ignored` lines on the import RESULTS page. Put's
objection is the same one system_alerts.py was written for, in its own words:
"the flash reaches whoever ran the import — who will not reliably relay it."
The team clicks past the results page; a durable alert on /alerts does not
disappear until someone acknowledges it.

So the skip now also records a system alert (mig 149 machinery, unchanged).
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

PID = 909201
IGN = "ZALERTFEE"
OK = "ZALERTOK"


def _entry(code, doc, net):
    return {"date_iso": "2026-05-09", "doc_no": doc, "line_seq": 1,
            "product_code_raw": code,
            "product_name_raw": "ค่าขนส่ง" if code == IGN else "ของจริง",
            "party": "ฟูแสงวัสดุ", "party_code": "33ฟ001", "qty": 1.0, "unit": "ใบ",
            "unit_price": net, "vat_type": 0, "discount": 0, "total": net, "net": net}


@pytest.fixture
def seeded(tmp_db, patch_models_conn):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    mapping_fixture.apply_mig124_if_needed(conn)
    mapping_fixture.reset_codes(conn, IGN, OK)
    conn.execute("INSERT INTO products (id,product_name,unit_type,sku_code,is_active)"
                 " VALUES (?,?,?,?,1)", (PID, "ของจริง", "ใบ", f"SK{PID}"))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,is_ignored,bsn_unit)"
                 " VALUES (?,?,?,1,'')", (IGN, "ค่าขนส่ง", PID))
    conn.execute("INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,is_ignored,bsn_unit)"
                 " VALUES (?,?,?,0,'')", (OK, "ของจริง", PID))
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


def _alerts(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM system_alerts WHERE resolved_at IS NULL ORDER BY id")]
    c.close()
    return rows


def test_a_skipped_billable_line_raises_a_system_alert(seeded):
    models.import_weekly([_entry(OK, "IV9920001-1", 100.0),
                          _entry(IGN, "IV9920001-2", 218.0)], "sales", "wk.csv")
    a = _alerts(seeded)
    assert len(a) == 1, "the skip must reach /alerts, not only the results page"
    assert a[0]["severity"] == "warning", "the import succeeded — this is not an error"
    assert IGN in a[0]["message"] and "ค่าขนส่ง" in a[0]["message"]
    assert "218" in a[0]["message"], "the money must be in the message"


def test_the_alert_carries_context_for_diagnosis(seeded):
    models.import_weekly([_entry(IGN, "IV9920002-1", 218.0)], "sales", "wk.csv")
    ctx = json.loads(_alerts(seeded)[0]["context_json"])
    assert ctx["file_type"] == "sales"
    assert ctx["filename"] == "wk.csv"
    assert any(d["bsn_code"] == IGN for d in ctx["ignored_detail"])


def test_a_repeat_does_not_spam_a_second_open_alert(seeded):
    """Dedupe is the point of the partial unique index: one OPEN alert per code
    until someone acknowledges it on /alerts."""
    models.import_weekly([_entry(IGN, "IV9920003-1", 218.0)], "sales", "wk1.csv")
    models.import_weekly([_entry(IGN, "IV9920004-1", 30.0)], "sales", "wk2.csv")
    assert len(_alerts(seeded)) == 1


def test_a_recurrence_after_acknowledging_alerts_again(seeded):
    """…but once resolved, it must be able to fire again — a recurrence is news."""
    models.import_weekly([_entry(IGN, "IV9920005-1", 218.0)], "sales", "wk1.csv")
    models.resolve_system_alert(_alerts(seeded)[0]["id"], "put")
    models.import_weekly([_entry(IGN, "IV9920006-1", 30.0)], "sales", "wk2.csv")
    assert len(_alerts(seeded)) == 1, "a fresh occurrence after acknowledgement must alert"


def test_a_clean_import_raises_no_alert(seeded):
    """Control: no skipped lines, no noise on /alerts."""
    models.import_weekly([_entry(OK, "IV9920007-1", 100.0)], "sales", "wk.csv")
    assert _alerts(seeded) == []


def test_an_alert_failure_never_sinks_a_good_import(seeded, monkeypatch):
    """Best-effort, exactly like the WACC caller: alerting is observability, and
    an alert-table problem must not fail an import that actually succeeded."""
    import models.system_alerts as sa
    monkeypatch.setattr(sa, "create_system_alert",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("alert table down")))
    stats = models.import_weekly([_entry(OK, "IV9920008-1", 100.0),
                                  _entry(IGN, "IV9920008-2", 218.0)], "sales", "wk.csv")
    assert stats["imported"] == 1 and stats["ignored"] == 1
    c = sqlite3.connect(seeded)
    n = c.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base='IV9920008'").fetchone()[0]
    c.close()
    assert n == 1, "the real line must still be imported"
