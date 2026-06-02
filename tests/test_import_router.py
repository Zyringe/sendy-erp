"""Unified import box — Express report-type detection.

detect_express_report(path) reads the report's Thai title line (cp874) and
classifies it so the /import box can dispatch to the right canonical importer.
Matches SPECIFIC report titles (ประวัติการขาย, not bare ขาย) so the wrong
'ขายเงินเชื่อ เรียงตามเลขที่' report — which does NOT parse with parse_weekly —
is left 'unknown' (Put picks) instead of misrouting to sales.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


def _write(tmp_path, title_line, name="r.csv"):
    """Write a minimal 3-line Express report (cp874) with the given title."""
    p = tmp_path / name
    p.write_text(
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                หน้า   :        1"\n'
        f'"{title_line}"\n'
        '"วันที่จาก   1 ม.ค. 2567  ถึง  31 ธ.ค. 2569"\n',
        encoding="cp874",
    )
    return str(p)


def test_detect_sales(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานประวัติการขาย แยกตามลูกค้า")) == "sales"


def test_detect_purchase(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานประวัติการซื้อ แยกตามผู้จำหน่าย")) == "purchase"


def test_detect_payments_in(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ")) == "payments_in"


def test_detect_payments_out(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานการจ่ายชำระหนี้ เรียงตามวันที่จ่ายเงิน")) == "payments_out"


def test_detect_credit_notes_ar(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานใบลดหนี้/รับคืนสินค้า เรียงตามเลขที่")) == "credit_notes_ar"


def test_detect_credit_notes_ap(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานใบลดหนี้/ส่งคืนสินค้า เรียงตามเลขที่")) == "credit_notes_ap"


def test_detect_ar_snapshot(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " ลูกหนี้คงค้างแบบละเอียด")) == "ar_snapshot"


def test_detect_ap_snapshot(tmp_path):
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " เจ้าหนี้คงค้างแบบละเอียด")) == "ap_snapshot"


def test_wrong_sales_report_is_unknown_not_sales(tmp_path):
    """'ขายเงินเชื่อ เรียงตามเลขที่' is a different report that does NOT parse
    with parse_weekly — must be 'unknown' (Put picks), never auto-routed to sales."""
    from import_router import detect_express_report
    assert detect_express_report(_write(tmp_path, " รายงานขายเงินเชื่อ เรียงตามเลขที่")) == "unknown"


def test_unreadable_file_is_unknown(tmp_path):
    from import_router import detect_express_report
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    assert detect_express_report(str(p)) == "unknown"
