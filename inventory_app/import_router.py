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

import os

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


# report_type → express_importer file_type (the express-family share one path).
# credit_notes_ap is the supplier-side ใบลดหนี้; express_importer's 'credit_notes'
# parser is supplier-keyed → express_credit_notes (a kept, single-source table).
_EXPRESS_KIND = {
    "payments_out": "payments_out",
    "credit_notes_ap": "credit_notes",
    "ar_snapshot": "ar_snapshot",
    "ap_snapshot": "ap_snapshot",
}


def commit_file(path, report_type, filename=None, db_path=None):
    """Dispatch one detected file to its CANONICAL importer and commit.

    Returns a uniform summary: {type, ok, summary}. Raises ValueError for an
    unknown report_type (a programmer/detection error); importer runtime errors
    propagate so the caller can isolate per-file. Importers are reused as-is —
    sales/payments_in go to their canonical homes, never the express twins.
    """
    if report_type == "payments_in":
        import models
        return {"type": report_type, "ok": True, "summary": models.import_payments(path)}

    if report_type == "credit_notes_ar":
        from import_credit_notes import import_credit_notes as _icn
        return {"type": report_type, "ok": True, "summary": _icn(path, db_path=db_path)}

    if report_type in ("sales", "purchase"):
        import models
        from parse_weekly import parse_sales, parse_purchases
        entries = parse_sales(path) if report_type == "sales" else parse_purchases(path)
        stats = models.import_weekly(entries, report_type, filename or os.path.basename(path))
        return {"type": report_type, "ok": True, "summary": stats}

    if report_type in _EXPRESS_KIND:
        import config
        import import_express
        # Pin the DB to config.DATABASE_PATH (honours DATA_DIR → /data on Railway).
        # import_express's own DB_PATH default is inventory_app/instance/inventory.db,
        # which does not exist on the prod container → "unable to open database file".
        import_express.run_import(_EXPRESS_KIND[report_type], path, dry_run=False,
                                  db_path=db_path or config.DATABASE_PATH)
        return {"type": report_type, "ok": True, "summary": {"imported": True}}

    raise ValueError(f"unknown report_type: {report_type!r}")


def preview_file(path, report_type, db_path=None):
    """Read-only preview for one file. Returns {type, ok, count, detail}.
    Writes NOTHING (counts/dry-runs only). Raises ValueError on unknown type."""
    if report_type in ("sales", "purchase"):
        import models
        from parse_weekly import parse_sales, parse_purchases
        entries = parse_sales(path) if report_type == "sales" else parse_purchases(path)
        plan = models.preview_import(entries, report_type)
        return {"type": report_type, "ok": True, "count": len(entries), "detail": plan}

    if report_type == "payments_in":
        import sqlite3
        import config
        import models
        recs = models.parse_payment_csv(path)
        conn = sqlite3.connect(db_path or config.DATABASE_PATH)
        try:
            existing = {row[0] for row in conn.execute("SELECT re_no FROM received_payments")}
        finally:
            conn.close()
        new = sum(1 for r in recs if r["re_no"] not in existing)
        return {"type": report_type, "ok": True, "count": len(recs),
                "detail": {"new": new, "existing": len(recs) - new}}

    if report_type == "credit_notes_ar":
        # import_credit_notes uses internal SAVEPOINT/RELEASE, so a manual
        # conn+rollback does NOT undo its writes. Use the dedicated dry-run
        # (own connection, isolation_level=None + BEGIN/ROLLBACK) which truly
        # leaves the DB untouched.
        from import_credit_notes import preview_credit_notes_import
        summary = preview_credit_notes_import(path, db_path=db_path)
        return {"type": report_type, "ok": True,
                "count": summary.get("parsed", 0), "detail": summary}

    if report_type in _EXPRESS_KIND:
        import import_express as ie
        kind = _EXPRESS_KIND[report_type]
        if kind == "ap_snapshot":
            records = ie.p_ap.parse_ap_snapshot(path)[0]   # (records, total, subtotals)
        elif kind == "ar_snapshot":
            records = list(ie.p_ar.parse_ar_snapshot(path))
        elif kind == "payments_out":
            records = list(ie.p_pout.parse_payments_out(path))
        else:                                              # 'credit_notes' (AP side)
            records = list(ie.p_cn.parse_credit_notes(path))
        return {"type": report_type, "ok": True, "count": len(records), "detail": {}}

    raise ValueError(f"unknown report_type: {report_type!r}")
