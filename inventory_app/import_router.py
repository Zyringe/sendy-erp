"""Unified import box — detect an Express report's type from its header.

The /import box reads the report's Thai title line (cp874, printed by Express in
the first few lines) and classifies it so it can dispatch to the right canonical
importer. Detection keys on SPECIFIC report titles, not bare keywords, so the
'ขายเงินเชื่อ เรียงตามเลขที่' report (a different layout that does NOT parse with
parse_weekly) is left 'unknown' for the operator to classify, rather than
misrouting to sales.

Returns one of:
    sales, purchase, payments_in, payments_out,
    credit_notes_ar, credit_notes_ap, ar_snapshot, ap_snapshot, unknown
"""
from __future__ import annotations

REPORT_TYPES = (
    "sales", "purchase", "payments_in", "payments_out",
    "credit_notes_ar", "credit_notes_ap", "ar_snapshot", "ap_snapshot",
)


def detect_express_report(path):
    """Classify an Express export by its title line. Returns a REPORT_TYPES
    value or 'unknown'. Never raises — an unreadable file is 'unknown'."""
    try:
        with open(path, encoding="cp874") as f:
            head = "".join(next(f, "") for _ in range(8)).replace("\xa0", " ")
    except (OSError, UnicodeDecodeError):
        return "unknown"

    # Credit notes first — both kinds carry 'ใบลดหนี้'; the รับคืน/ส่งคืน
    # qualifier separates the AR (customer returns) from the AP (we return to
    # supplier) side. They route to different importers.
    if "ใบลดหนี้" in head:
        if "ส่งคืน" in head:
            return "credit_notes_ap"
        return "credit_notes_ar"   # 'รับคืนสินค้า' or unqualified → AR side
    if "การรับชำระหนี้" in head:
        return "payments_in"
    if "การจ่ายชำระหนี้" in head:
        return "payments_out"
    if "ลูกหนี้คงค้าง" in head:
        return "ar_snapshot"
    if "เจ้าหนี้คงค้าง" in head:
        return "ap_snapshot"
    # Specific sales/purchase report titles — NOT bare 'ขาย'/'ซื้อ', so the
    # wrong 'ขายเงินเชื่อ' report stays unknown.
    if "ประวัติการขาย" in head or "รายงานการขาย" in head:
        return "sales"
    if "ประวัติการซื้อ" in head or "รายงานการซื้อ" in head:
        return "purchase"
    return "unknown"
