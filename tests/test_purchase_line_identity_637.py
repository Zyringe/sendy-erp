"""Regression coverage for GH #637's purchase-line identity mismatch."""
import datetime
import sqlite3

from express_dbf_source import build_purchase_entries


def _aptrn(doc_no):
    return {
        "DOCNUM": doc_no,
        "RECTYP": "1",
        "SUPCOD": "SUP-637",
        "FLGVAT": 0,
        "DOCDAT": datetime.date(2026, 9, 21),
    }


def _stcrd(doc_no, seqnum, code, qty):
    return {
        "DOCNUM": doc_no,
        "SEQNUM": seqnum,
        "STKCOD": code,
        "STKDES": f"product {code}",
        "TRNQTY": qty,
        "TQUCOD": "ตัว",
        "UNITPR": 10.0,
        "DISC": "",
        "TRNVAL": qty * 10.0,
        "NETVAL": qty * 10.0,
        "RDOCNUM": "",
    }


def _fixture_rows():
    """STCRD file order is deliberately not Express SEQNUM order."""
    return [
        _stcrd("HP637001", 3, "P-A", 30),
        _stcrd("HP637002", 9, "P-A", 90),
        _stcrd("HP637001", 2, "P-B", 20),
        _stcrd("HP637001", 1, "P-A", 10),
    ]


def test_purchase_line_seq_is_product_ordinal_in_seqnum_order():
    entries = build_purchase_entries(
        [_aptrn("HP637001"), _aptrn("HP637002")],
        _fixture_rows(),
        [{"SUPCOD": "SUP-637", "SUPNAM": "supplier"}],
    )

    # Count first: an empty or filtered fixture must not make the property pass.
    assert len(entries) == 4
    assert [
        (e["doc_no"], e["product_code_raw"], e["qty"], e["line_seq"])
        for e in entries
    ] == [
        ("HP637001", "P-A", 30, 2),
        ("HP637002", "P-A", 90, 1),  # same product, different document
        ("HP637001", "P-B", 20, 1),  # same document, different product
        ("HP637001", "P-A", 10, 1),
    ]


def _seed_products(db_path):
    conn = sqlite3.connect(db_path)
    try:
        pids = {}
        for code in ("P-A", "P-B"):
            pid = conn.execute(
                "INSERT INTO products (product_name, unit_type, cost_price) "
                "VALUES (?, 'ตัว', 0)",
                (f"product {code}",),
            ).lastrowid
            conn.execute(
                "INSERT INTO product_code_mapping "
                "(bsn_code, bsn_name, product_id, bsn_unit) VALUES (?, ?, ?, '')",
                (code, f"product {code}", pid),
            )
            pids[code] = pid
        conn.commit()
        return pids
    finally:
        conn.close()


def _weekly_purchase_file(path, rows):
    header = [
        "(BSN)บจก.บุญสวัสดิ์นำชัย" + " " * 92 + "หน้า   :        1",
        "  รายงานประวัติการซื้อ แยกตามผู้จำหน่าย",
        "รหัสผู้จำหน่ายจาก                       ถึง  ไพ" + " " * 69 + "วันที่ : 22/09/69",
        "วันที่จาก          21 ก.ย. 2569         ถึง  31 ธ.ค. 2569",
        "-" * 133,
        "   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย VAT   ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง",
        "-" * 133,
        "  supplier /SUP-637",
    ]
    body = []
    for code in ("P-A", "P-B"):
        body.append(f"   product {code} /{code}")
        for row in sorted(
            (r for r in rows if r["DOCNUM"] == "HP637001" and r["STKCOD"] == code),
            key=lambda r: r["SEQNUM"],
        ):
            line = (
                " " * 8
                + "21/09/69"
                + " " * 3
                + f"{row['DOCNUM']:<9}"
                + f"{row['TRNQTY']:>15.2f}"
                + " "
                + f"{row['TQUCOD']:<4}"
                + f"{row['UNITPR']:>14.2f}"
                + "  0"
                + f"{'':>11}"
                + f"{row['TRNVAL']:>14.2f}"
                + " " * 11
                + f"{row['NETVAL']:>14.2f}"
            )
            assert line[64] == "0"  # CONTROL: fixed money columns are aligned.
            body.append(line)
        body.append("          รวมตาม ซื้อเชื่อ")
    path.write_text(
        "\r\n".join(f'"{line}"' for line in header + body + [">>>> จบรายงาน <<<<"]) + "\r\n",
        encoding="cp874",
    )


def _state(db_path, pids):
    conn = sqlite3.connect(db_path)
    try:
        return {
            "purchase_rows": conn.execute(
                "SELECT COUNT(*) FROM purchase_transactions WHERE doc_no='HP637001'"
            ).fetchone()[0],
            "ledger_rows": conn.execute(
                "SELECT COUNT(*) FROM transactions "
                "WHERE reference_no='HP637001' AND note='BSN ซื้อ'"
            ).fetchone()[0],
            "stock": {
                code: conn.execute(
                    "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
                ).fetchone()[0]
                for code, pid in pids.items()
            },
        }
    finally:
        conn.close()


def test_weekly_purchase_over_dbf_rows_is_noop_with_removals_off(empty_db, tmp_path):
    import import_router
    import models

    pids = _seed_products(empty_db)
    rows = _fixture_rows()
    dbf_entries = build_purchase_entries(
        [_aptrn("HP637001")], rows, [{"SUPCOD": "SUP-637", "SUPNAM": "supplier"}]
    )
    assert len(dbf_entries) == 3
    first = models.import_weekly(dbf_entries, "purchase", "express_dbf:fixture637")
    assert first["imported"] == 3
    assert first["unchanged"] == 0

    weekly = tmp_path / "ซื้อ_637.csv"
    _weekly_purchase_file(weekly, rows)
    before = _state(empty_db, pids)
    assert before == {
        "purchase_rows": 3,
        "ledger_rows": 3,
        "stock": {"P-A": 40, "P-B": 20},
    }

    result = import_router.commit_file(
        str(weekly), "purchase", filename=weekly.name, apply_removals=False
    )
    summary = result["summary"]

    # Assert all counts before collection/stock properties: this must prove the
    # real unchanged branch ran, not pass because no lines reached commit_file.
    assert summary["unchanged"] == 3, summary
    assert summary["imported"] == 0, summary
    assert summary["overwritten"] == 0, summary
    assert summary["removed"] == 0, summary
    assert summary["removed_skipped"] == 0, summary
    assert _state(empty_db, pids) == before
