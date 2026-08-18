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


class HistoryExportBlocked(ValueError):
    """A full-history Express export was dropped on the weekly importer.

    Put's policy A (2026-08-03): full-history data goes through the separate
    Express ZIP module only. Re-running a history dump through the weekly path
    re-writes months of stock movements; there is no second history-import mode.
    """


# Thai, because the operator reads it. Names the report kind that was rejected
# and where to go instead — the route turns this into a link to
# bsn.express_dbf_import.
_HISTORY_BLOCK_MSG = (
    "ไฟล์นี้เป็นรายงาน \"ประวัติ\" (export ย้อนหลังทั้งช่วง) ไม่ใช่ไฟล์รายสัปดาห์ — "
    "นำเข้าทางหน้านี้ไม่ได้ เพราะจะเขียนทับสต็อกและยอดขายย้อนหลังทั้งก้อน. "
    "ถ้าต้องการข้อมูลย้อนหลัง ให้ใช้หน้า \"นำเข้า Express (zip)\" แทน"
)


# Second refusal reason: the header carries no usable date range, so we cannot
# tell a weekly from a history dump at all. Under policy A the safe answer is to
# refuse and let the operator re-export (see parse_weekly.date_filter_is_readable).
_UNREADABLE_HEADER_MSG = (
    "อ่านช่วงวันที่ของไฟล์นี้ไม่ได้ — ไม่มีบรรทัด \"วันที่จาก ... ถึง ...\" "
    "หรือเว้นว่างไว้ ระบบจึงแยกไม่ออกว่าเป็นไฟล์รายสัปดาห์หรือ export ย้อนหลังทั้งหมด. "
    "กรุณา export ใหม่จาก Express โดยระบุ \"วันที่จาก\" ให้ชัดเจน "
    "(ถ้าตั้งใจจะนำเข้าข้อมูลย้อนหลัง ให้ใช้หน้า \"นำเข้า Express (zip)\" แทน)"
)


def history_block_reason(path):
    """The Thai refusal message if this file must not go through the weekly
    importer, else None. Public counterpart of the guard below, so the route can
    re-derive a blocked row's message at confirm time without carrying it in the
    signed session."""
    try:
        _reject_history_export(path)
    except HistoryExportBlocked as exc:
        return str(exc)
    return None


def _reject_history_export(path):
    """Gate for the weekly sales/purchase path — called by BOTH preview_file and
    commit_file so a stale tab or crafted POST straight to /confirm cannot slip
    a history dump past the preview.

    Two distinct refusals, each with its own operator message: the dates say
    history, or the dates cannot be read at all.
    """
    from parse_weekly import date_filter_is_readable, is_history_export
    if is_history_export(path):
        raise HistoryExportBlocked(_HISTORY_BLOCK_MSG)
    if not date_filter_is_readable(path):
        raise HistoryExportBlocked(_UNREADABLE_HEADER_MSG)


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


def commit_file(path, report_type, filename=None, db_path=None,
                apply_removals=False):
    """Dispatch one detected file to its CANONICAL importer and commit.

    Returns a uniform summary: {type, ok, summary}. Raises ValueError for an
    unknown report_type (a programmer/detection error); importer runtime errors
    propagate so the caller can isolate per-file. Importers are reused as-is —
    sales/payments_in go to their canonical homes, never the express twins.

    apply_removals defaults to **False** (sales/purchase AND payments_in): a
    รหัสสินค้า- or พนักงานขาย-FILTERED Express export yields partial invoices whose
    filtered-out lines look deleted, and reversing them mass-deletes real stock
    (for payments_in: real receipt→invoice links). The operator opts in per-file
    on the preview page by confirming the file is a complete weekly export.
    """
    if report_type == "payments_in":
        import models
        # Same opt-in as sales/purchase: a receipt's iv_list is only the
        # AUTHORITATIVE complete allocation set when the operator confirms the
        # export is unfiltered, so stale-link removal rides the same checkbox.
        return {"type": report_type, "ok": True,
                "summary": models.import_payments(path,
                                                  apply_removals=apply_removals)}

    if report_type == "credit_notes_ar":
        from import_credit_notes import import_credit_notes as _icn
        return {"type": report_type, "ok": True, "summary": _icn(path, db_path=db_path)}

    if report_type in ("sales", "purchase"):
        import models
        from parse_weekly import parse_sales, parse_purchases
        _reject_history_export(path)
        entries = parse_sales(path) if report_type == "sales" else parse_purchases(path)
        stats = models.import_weekly(entries, report_type,
                                     filename or os.path.basename(path),
                                     apply_removals=apply_removals)
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
        _reject_history_export(path)
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
            # Removal plan, read-only: links this file's receipts currently have
            # that the file no longer lists. The operator must see this count
            # BEFORE the opt-in checkbox is worth offering — same rule the
            # sales/purchase preview follows.
            removed = 0
            for r in models._merge_duplicate_receipts(recs):
                if r.get("re_no") not in existing:
                    continue
                incoming = [iv["iv_no"] for iv in (r.get("iv_list") or [])]
                if not incoming:
                    continue          # refused at import time, never a removal
                marks = ",".join("?" for _ in incoming)
                removed += conn.execute(
                    f"""SELECT COUNT(*) FROM paid_invoices pi
                          JOIN received_payments rp ON rp.id = pi.re_id
                         WHERE rp.re_no = ? AND pi.doc_no NOT IN ({marks})""",
                    (r["re_no"], *incoming)).fetchone()[0]
        finally:
            conn.close()
        new = sum(1 for r in recs if r["re_no"] not in existing)
        return {"type": report_type, "ok": True, "count": len(recs),
                "detail": {"new": new, "existing": len(recs) - new,
                           "removed": removed}}

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


def _open_optional(eds, dataset_dir, name):
    """Read a table the zip MAY not contain. The team's .bat only started
    carrying ARBIL/BKTRN/OESO/GL on 2026-08-17, and a flash drive that has not
    been refreshed yet still produces the old 9-table zip. A missing optional
    table is a thinner import, not a failed one — the ledger and the outstanding
    snapshots do not depend on any of them.

    ONLY FileNotFoundError. A file that is present but unreadable is a real
    problem and must reach the caller's error branch, not be reported as absent.
    """
    try:
        return eds.open_table(dataset_dir, name)
    except FileNotFoundError:
        return None


def _commit_snapshot(kind, build, db_path, snapshot_date):
    """Build + import one outstanding snapshot, converting a failure into a
    reported one instead of an exception (see the call site's rationale).
    Returns run_import_records' stats dict, or {'imported': 0, 'error': ...}."""
    import import_express
    try:
        return import_express.run_import_records(
            kind, build(), db_path=db_path, snapshot_date=snapshot_date)
    except Exception as exc:
        return {"imported": 0, "skipped": 0, "total": 0, "lines": 0,
                "error": str(exc)[:300]}


def commit_express_dbf(dataset_dir, db_path=None, since_days=60,
                       snapshot_date=None):
    """Import all 8 Express DBF transactional types for one dataset
    directory into Sendy — the DBF branch parallel to the text-report path
    above (Phase 1 slices A+B — payments/credit-notes join sales/purchase
    here). Reads the needed tables straight off disk and feeds each type's
    SAME downstream importer the text-report path uses, so idempotency/
    dedup is unchanged.

    Also lands the two OUTSTANDING snapshots (ลูกหนี้คงค้าง / เจ้าหนี้คงค้าง),
    which used to arrive only as hand-exported text reports and had gone 73 and
    80 days stale. Because vat_book_builder calls this same function against
    vat_book.db, each book gets its OWN snapshot from its own dataset — BSN5657
    into the main DB, xp5 into the VAT book (Put, 2026-08-17: import both books,
    keep each book's figures on that book).

    snapshot_date: the as-of date stamped on both snapshots (ISO). Defaults to
    today — the DBF has no "as of" header the way the printed report does, and
    an export is by definition current as of the day it was taken.

    since_days: recency window (Put's call, Phase 2 follow-up) — only docs
    with DOCDAT within the last `since_days` days are imported. None means
    no filter (the whole history — a manual backfill/override, never the
    daily default). Filtering is scoped to each type's HEADER rows (ARTRN/
    APTRN), which is what keeps a doc's own lines all-or-nothing consistent
    AND is the actual fix for the 12+-minute full-history run: STCRD still
    gets read whole (open_table has no filter — dbfread must scan the file
    regardless), but models.import_weekly() only ever sees entries for
    in-window docs instead of diffing the full multi-year history against
    the DB row by row.

    Called by the web route (blueprints/bsn.py::express_dbf_upload).
    Returns a summary dict.
    """
    import datetime
    import config
    import models
    import express_dbf_source as eds
    import import_credit_notes
    import import_express

    db_path = db_path or config.DATABASE_PATH
    cutoff = (datetime.date.today() - datetime.timedelta(days=since_days)
              if since_days is not None else None)
    snapshot_date = snapshot_date or datetime.date.today().isoformat()

    artrn = eds.open_table(dataset_dir, "ARTRN")
    aptrn = eds.open_table(dataset_dir, "APTRN")
    stcrd = eds.open_table(dataset_dir, "STCRD")
    armas = eds.open_table(dataset_dir, "ARMAS")
    apmas = eds.open_table(dataset_dir, "APMAS")
    artrnrm = eds.open_table(dataset_dir, "ARTRNRM")
    arrcpit = eds.open_table(dataset_dir, "ARRCPIT")
    aprcpit = eds.open_table(dataset_dir, "APRCPIT")

    sales_entries = eds.build_sales_entries(artrn, stcrd, armas, cutoff=cutoff)
    purchase_entries = eds.build_purchase_entries(aptrn, stcrd, apmas, cutoff=cutoff)
    refs = eds.build_invoice_refs(artrn, artrnrm, cutoff=cutoff)
    payments_in_skipped = []
    payments_in_records = eds.build_payments_in_records(
        artrn, arrcpit, armas, cutoff=cutoff, skipped=payments_in_skipped)
    payments_out_records = eds.build_payments_out_records(aptrn, aprcpit, apmas, cutoff=cutoff)
    credit_notes_ar_records = eds.build_credit_notes_ar_records(artrn, armas, cutoff=cutoff)
    credit_notes_ap_records = eds.build_credit_notes_ap_records(aptrn, stcrd, apmas, cutoff=cutoff)

    label = f"express_dbf:{os.path.basename(os.path.normpath(dataset_dir))}"
    sales_stats = models.import_weekly(sales_entries, "sales", label)
    purchase_stats = models.import_weekly(purchase_entries, "purchase", label)
    refs_upserted = _upsert_invoice_refs(refs, db_path)
    # The daily DBF read IS the authoritative complete allocation set: `cutoff`
    # filters RECEIPTS, never lines within one, so every receipt it includes
    # carries all of its ARRCPIT lines. That makes this the one payments_in
    # path that can replace stale links without an operator preview — and it
    # has no preview to offer, so it is also the only path that can keep Sendy
    # in step with Express on its own.
    #
    # EXCEPT a receipt that lost a line to an unmapped RECTYP: its iv_list is
    # PARTIAL, and replacing from a partial set deletes a real link. Those
    # receipts import additively, exactly as before. Real data carries one such
    # line (RE0041138 / DR0000003 / RECTYP='4' / ฿600).
    _tainted = {s.get('re_no') for s in payments_in_skipped}
    _complete = [r for r in payments_in_records if r['re_no'] not in _tainted]
    _partial = [r for r in payments_in_records if r['re_no'] in _tainted]
    payments_in_stats = models.import_payment_records(_complete, apply_removals=True)
    if _partial:
        _extra = models.import_payment_records(_partial)
        for _k in ('imported', 'updated', 'skipped', 'merged', 'removed_links', 'total'):
            payments_in_stats[_k] = payments_in_stats.get(_k, 0) + _extra.get(_k, 0)
        payments_in_stats['errors'] = (
            payments_in_stats['errors'] + _extra['errors'])[:5]
    # Never silent: a skipped DR-type ARRCPIT line (or any future unsupported
    # RECTYP) always surfaces here, count and detail, even when the list is
    # empty — so the route's JSON response has a stable shape either way.
    payments_in_stats["skipped_rectyp"] = payments_in_skipped
    payments_out_stats = import_express.run_import_records(
        "payments_out", payments_out_records, db_path=db_path)
    credit_notes_ar_stats = import_credit_notes.import_credit_note_amounts_records(
        credit_notes_ar_records, db_path=db_path)
    credit_notes_ap_stats = import_express.run_import_records(
        "credit_notes", credit_notes_ap_records, db_path=db_path)

    # Outstanding snapshots. Built from the SAME already-read ARTRN/APTRN rows,
    # and deliberately WITHOUT `cutoff`: since_days scopes the ledger, but a
    # balance is owed regardless of the invoice's age (the real snapshot carries
    # unpaid docs dated 2009). Passing a cutoff here would silently drop the
    # oldest — and largest — debts from AR aging and the dunning list.
    #
    # Isolated per side, like scan_reconcile below: the money import above has
    # already committed, so a snapshot that refuses (the NETAMT invariant guard)
    # must not make the whole upload read as failed and send the team into a
    # retry loop. Degrading is safe — every AR/AP reader takes
    # MAX(snapshot_date_iso), so the previous day's snapshot simply stays
    # current — but it is never SILENT: the error rides the result dict up to
    # the upload page, because a silently stale AR is the exact bug this
    # feature exists to end.
    # ใบวางบิล. Whole table, no window (the bills open invoices point at run back
    # to 2014), and isolated like the snapshots: it is reference data, so a
    # failure must not make a committed ledger import read as failed.
    # บัญชีแยกประเภท — windowed by whole calendar years (see gl_cutoff), unlike
    # the ledger's rolling since_days, and isolated like every register here.
    try:
        gljnl = _open_optional(eds, dataset_dir, "GLJNL")
        if gljnl is None:
            gl_stats = {"accounts": 0, "vouchers": 0, "lines": 0,
                        "skipped": "GLJNL not in this zip"}
        else:
            n_acc, n_vou, n_lin = _replace_general_ledger(
                *eds.build_gl_records(
                    _open_optional(eds, dataset_dir, "GLACC") or [],
                    gljnl,
                    _open_optional(eds, dataset_dir, "GLJNLIT") or [],
                    eds.gl_cutoff()),
                "BSN", db_path)
            gl_stats = {"accounts": n_acc, "vouchers": n_vou, "lines": n_lin}
    except Exception as exc:
        gl_stats = {"accounts": 0, "vouchers": 0, "lines": 0, "error": str(exc)[:300]}

    # ใบสั่งขาย — same isolation and optional-table handling as the two below.
    try:
        oeso = _open_optional(eds, dataset_dir, "OESO")
        if oeso is None:
            sales_orders_stats = {"orders": 0, "lines": 0, "skipped": "OESO not in this zip"}
        else:
            oesoit = _open_optional(eds, dataset_dir, "OESOIT") or []
            n_head, n_line = _replace_sales_orders(
                *eds.build_sales_order_records(oeso, oesoit, armas), "BSN", db_path)
            sales_orders_stats = {"orders": n_head, "lines": n_line}
    except Exception as exc:
        sales_orders_stats = {"orders": 0, "lines": 0, "error": str(exc)[:300]}

    # ทะเบียนเช็ค — same isolation and the same optional-table handling as the
    # billing notes above; neither is something the ledger depends on.
    try:
        bktrn = _open_optional(eds, dataset_dir, "BKTRN")
        if bktrn is None:
            bank_cheques_stats = {"stored": 0, "skipped": "BKTRN not in this zip"}
        else:
            bank_cheques_stats = {"stored": _replace_bank_cheques(
                eds.build_bank_cheque_records(bktrn), "BSN", db_path)}
    except Exception as exc:
        bank_cheques_stats = {"stored": 0, "error": str(exc)[:300]}

    try:
        arbil = _open_optional(eds, dataset_dir, "ARBIL")
        if arbil is None:
            billing_notes_stats = {"upserted": 0, "skipped": "ARBIL not in this zip"}
        else:
            # 'BSN' matches what the snapshots use: each book tags its own rows
            # 'BSN' inside its own DB (the VAT book is BSN's VAT entity), which
            # is what keeps a book's figures on that book.
            billing_notes_stats = {"upserted": _upsert_billing_notes(
                eds.build_billing_note_records(arbil, armas), "BSN", db_path)}
    except Exception as exc:
        # Reading happens INSIDE this block on purpose. A present-but-corrupt
        # ARBIL raises from dbfread rather than returning None, and outside the
        # block that would kill a ledger import that has already committed — and
        # be reported as "not in this zip", which is a different thing and a lie.
        billing_notes_stats = {"upserted": 0, "error": str(exc)[:300]}

    ar_snapshot_stats = _commit_snapshot(
        "ar_snapshot", lambda: eds.build_ar_snapshot_records(artrn, armas),
        db_path, snapshot_date)
    ap_snapshot_stats = _commit_snapshot(
        "ap_snapshot", lambda: eds.build_ap_snapshot_records(aptrn, apmas),
        db_path, snapshot_date)

    # reconcile-scan (reconcile-scan-plan.md §2): read-only detection, reusing
    # the SAME sales_entries/artrn already built above (no second parse).
    # Deliberately LAST and wrapped: this is a read-only observer over data
    # the money pipeline above has ALREADY committed (import_weekly commits
    # internally) — a scan bug must never make a daily upload look failed,
    # skip payments/credit-notes/refs, or block retrying. Never re-raises;
    # the failure is surfaced to the user via the upload result instead
    # (blueprints/bsn.py + import_express_dbf.html render reconcile.error).
    try:
        reconcile_counts = models.scan_reconcile(sales_entries, artrn, cutoff)
    except Exception as exc:
        reconcile_counts = {'error': str(exc)[:200]}

    return {"sales": sales_stats, "purchase": purchase_stats,
            "invoice_refs_upserted": refs_upserted,
            "billing_notes": billing_notes_stats,
            "bank_cheques": bank_cheques_stats,
            "sales_orders": sales_orders_stats,
            "general_ledger": gl_stats,
            "payments_in": payments_in_stats,
            "payments_out": payments_out_stats,
            "credit_notes_ar": credit_notes_ar_stats,
            "credit_notes_ap": credit_notes_ap_stats,
            "ar_snapshot": ar_snapshot_stats,
            "ap_snapshot": ap_snapshot_stats,
            "snapshot_date": snapshot_date,
            "reconcile": reconcile_counts}


def _replace_general_ledger(accounts, vouchers, lines, entity, db_path):
    """บัญชีแยกประเภท (mig 165). All three tables replaced in ONE transaction:
    a line pointing at a voucher that has gone, or an account number that has
    gone, is unreachable.

    Same empty-write guard as the other registers, keyed on the vouchers — the
    chart of accounts alone is not evidence the journal parsed.
    """
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        if not vouchers:
            existing = conn.execute(
                "SELECT COUNT(*) FROM express_gl_vouchers WHERE entity = ?",
                (entity,)).fetchone()[0]
            if existing:
                raise ValueError(
                    f'บัญชีแยกประเภท: parsed 0 vouchers but {existing} already stored '
                    f'for {entity} — refusing to erase them')
            return 0, 0, 0
        conn.execute("BEGIN IMMEDIATE")
        for t in ("express_gl_lines", "express_gl_vouchers", "express_gl_accounts"):
            conn.execute(f"DELETE FROM {t} WHERE entity = ?", (entity,))
        conn.executemany(
            "INSERT INTO express_gl_accounts "
            " (entity, account_no, account_name, level, parent_no, account_type,"
            "  nature, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(entity, a['account_no'], a['account_name'], a['level'], a['parent_no'],
              a['account_type'], a['nature'], a['status']) for a in accounts])
        conn.executemany(
            "INSERT INTO express_gl_vouchers "
            " (entity, voucher, voucher_date_iso, journal_type, reference_no,"
            "  description, source_journal, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(entity, v['voucher'], v['voucher_date_iso'], v['journal_type'],
              v['reference_no'], v['description'], v['source_journal'], v['status'])
             for v in vouchers])
        conn.executemany(
            "INSERT INTO express_gl_lines "
            " (entity, voucher, line_seq, voucher_date_iso, account_no, description,"
            "  entry_side, type_code, amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(entity, l['voucher'], l['line_seq'], l['voucher_date_iso'],
              l['account_no'], l['description'], l['entry_side'], l['type_code'],
              l['amount']) for l in lines])
        conn.commit()
    finally:
        conn.close()
    return len(accounts), len(vouchers), len(lines)


def _replace_sales_orders(heads, lines, entity, db_path):
    """ใบสั่งขาย (mig 164). Replace per entity in ONE transaction so headers and
    lines can never be seen half-swapped: a line whose header has been deleted is
    unreachable, and a header whose lines have not landed reads as an empty order.

    Same empty-write guard as the other registers.
    """
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        if not heads:
            existing = conn.execute(
                "SELECT COUNT(*) FROM express_sales_orders WHERE entity = ?",
                (entity,)).fetchone()[0]
            if existing:
                raise ValueError(
                    f'ใบสั่งขาย: parsed 0 orders but {existing} already stored for '
                    f'{entity} — refusing to erase them')
            return 0, 0
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM express_sales_order_lines WHERE entity = ?", (entity,))
        conn.execute("DELETE FROM express_sales_orders WHERE entity = ?", (entity,))
        conn.executemany(
            "INSERT INTO express_sales_orders "
            " (entity, so_no, so_date_iso, customer_code, customer_name,"
            "  salesperson_code, your_ref, pay_terms, delivery_date_iso,"
            "  completed_date_iso, total, discount_amount, vat_amount, net_amount,"
            "  status_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(entity, h['so_no'], h['so_date_iso'], h['customer_code'],
              h['customer_name'], h['salesperson_code'], h['your_ref'],
              h['pay_terms'], h['delivery_date_iso'], h['completed_date_iso'],
              h['total'], h['discount_amount'], h['vat_amount'], h['net_amount'],
              h['status_code']) for h in heads])
        conn.executemany(
            "INSERT INTO express_sales_order_lines "
            " (entity, so_no, line_seq, product_code, product_name, ordered_qty,"
            "  cancelled_qty, remaining_qty, unit, unit_price, line_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(entity, l['so_no'], l['line_seq'], l['product_code'], l['product_name'],
              l['ordered_qty'], l['cancelled_qty'], l['remaining_qty'], l['unit'],
              l['unit_price'], l['line_total']) for l in lines])
        conn.commit()
    finally:
        conn.close()
    return len(heads), len(lines)


def _replace_bank_cheques(records, entity, db_path):
    """ทะเบียนเช็ค (mig 163). REPLACE per entity, not upsert: BKTRN has no unique
    natural key (CHQNUM repeats on 38 of 12,805 rows), and the table is a mirror
    of Express's register at export time — the same shape as the outstanding
    snapshots.

    Same empty-write guard the snapshots use, for the same reason: a parse that
    yields nothing and a register that is genuinely empty are indistinguishable
    here, and deleting a good register to write nothing is the one outcome that
    cannot be undone by the next import.
    """
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        if not records:
            existing = conn.execute(
                "SELECT COUNT(*) FROM express_bank_cheques WHERE entity = ?",
                (entity,)).fetchone()[0]
            if existing:
                raise ValueError(
                    f'ทะเบียนเช็ค: parsed 0 rows but {existing} already stored for '
                    f'{entity} — refusing to erase them')
            return 0
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM express_bank_cheques WHERE entity = ?", (entity,))
        conn.executemany(
            "INSERT INTO express_bank_cheques "
            " (entity, kind, type_code, cheque_no, trn_date_iso, cheque_date_iso,"
            "  received_date_iso, paid_in_date_iso, bank_code, branch, bank_account,"
            "  party_code, party_name, amount, charge, vat_amount, net_amount,"
            "  remaining_amount, status_code, remark, ref_doc, ref_no, voucher) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(entity, r['kind'], r['type_code'], r['cheque_no'], r['trn_date_iso'],
              r['cheque_date_iso'], r['received_date_iso'], r['paid_in_date_iso'],
              r['bank_code'], r['branch'], r['bank_account'], r['party_code'],
              r['party_name'], r['amount'], r['charge'], r['vat_amount'],
              r['net_amount'], r['remaining_amount'], r['status_code'], r['remark'],
              r['ref_doc'], r['ref_no'], r['voucher']) for r in records]
        )
        conn.commit()
    finally:
        conn.close()
    return len(records)


def _upsert_billing_notes(records, entity, db_path):
    """ใบวางบิล (mig 162). Same shape as _upsert_invoice_refs: a bulk upsert on
    a plain connection with NO express_import_log batch row.

    Deliberately not a run_import_records file_type — that table's file_type
    carries a CHECK over six literal values, so adding a seventh would mean
    rebuilding express_import_log, which two other tables hold foreign keys
    into. A billing note has no batch to trace anyway: it is upserted by
    identity (entity, bill_no), not replaced per import.
    """
    if not records:
        return 0
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executemany(
            "INSERT INTO express_billing_notes "
            " (entity, bill_no, bill_date_iso, sent_date_iso, approved_date_iso,"
            "  customer_code, customer_name, pay_cond, net_amount, is_cancelled,"
            "  remark, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(entity, bill_no) DO UPDATE SET "
            " bill_date_iso=excluded.bill_date_iso, sent_date_iso=excluded.sent_date_iso,"
            " approved_date_iso=excluded.approved_date_iso,"
            " customer_code=excluded.customer_code, customer_name=excluded.customer_name,"
            " pay_cond=excluded.pay_cond, net_amount=excluded.net_amount,"
            " is_cancelled=excluded.is_cancelled, remark=excluded.remark,"
            " updated_at=excluded.updated_at",
            [(entity, r['bill_no'], r['bill_date_iso'], r['sent_date_iso'],
              r['approved_date_iso'], r['customer_code'], r['customer_name'],
              r['pay_cond'], r['net_amount'], int(r['is_cancelled']), r['remark'])
             for r in records]
        )
        conn.commit()
    finally:
        conn.close()
    return len(records)


def _upsert_invoice_refs(refs, db_path):
    """Write express_invoice_refs rows (doc_base PK upsert). A plain sqlite3
    connection pinned to db_path — always config.DATABASE_PATH via the
    caller, never a self-computed instance/ path (the PR #242 prod
    CANTOPEN bug)."""
    if not refs:
        return 0
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO express_invoice_refs (doc_base, youref, remark, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(doc_base) DO UPDATE SET "
            "youref=excluded.youref, remark=excluded.remark, updated_at=excluded.updated_at",
            [(r["doc_base"], r["youref"], r["remark"]) for r in refs]
        )
        conn.commit()
    finally:
        conn.close()
    return len(refs)
