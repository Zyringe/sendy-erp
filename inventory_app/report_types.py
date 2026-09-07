"""The one declaration of the Express report-type vocabulary.

A "report type" is what `import_router.detect_express_report` classifies an
Express text export as, what the /import-data dropdown offers, and what
`commit_file` / `preview_file` dispatch on. Before this module the vocabulary
was spelled out by hand in three files that had to agree — the drift already
cost one money-path incident (see `retired_reason` on ar_snapshot below) and had
silently produced a fourth inconsistency in vat_book_builder's counts dict.

Each field exists because a specific caller needs it. Nothing here is
speculative; adding a field is fine, adding one "for later" is not:

    key                 the wire value: session rows, the <select>, dispatch
    label               Thai, shown in the dropdown and the preview table
    title_markers       detect: ANY of these appearing in the first 8 lines
    title_requires      detect: and ALL of these too (credit_notes_ap only)
    title_excludes      detect: and NONE of these (credit_notes_ar only)
    retired_reason      None = live. A string = detected and REFUSED, with this
                        shown to the operator.
    supports_removals   may the operator opt into source-line removal
    express_kind        None, or the file_type the express_importer path wants
    dbf_count_field     which sub-key vat_book_builder reads out of per_type
    book_meta_key       the name vat_book_builder writes into book_meta.counts.
                        NOT always the key: 'sales' is written as
                        'sales_imported', and import_express_dbf.html reads
                        `counts.sales_imported` by name — renaming it would
                        render blank, silently.

DETECTION ORDER IS PART OF THE DATA. `REPORT_TYPES` is ordered, and
`detect_express_report` walks it in order. Credit notes come first because both
kinds carry 'ใบลดหนี้' and the ส่งคืน qualifier is what separates the AP side
(we return to a supplier) from the AR side (a customer returns to us) — they go
to different importers, so getting this backwards moves money the wrong way.
"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ReportType:
    key: str
    label: str
    title_markers: Tuple[str, ...] = ()
    title_requires: Tuple[str, ...] = ()
    title_excludes: Tuple[str, ...] = ()
    retired_reason: Optional[str] = None
    supports_removals: bool = False
    express_kind: Optional[str] = None
    dbf_count_field: Optional[str] = None
    book_meta_key: Optional[str] = None

    @property
    def is_retired(self) -> bool:
        return self.retired_reason is not None

    def matches(self, head: str) -> bool:
        """Does this Express export's header say it is this type?

        Never true for a type with no markers — 'unknown' is the fallback the
        detector returns after the walk, not something a header can match.
        """
        if not self.title_markers:
            return False
        if any(x in head for x in self.title_excludes):
            return False
        if not all(r in head for r in self.title_requires):
            return False
        return any(m in head for m in self.title_markers)


# The text-report AR/AP import path was closed by F8 (Put, 2026-08-22): those
# balances now come from the daily Express DBF zip. Detection is deliberately
# KEPT so the operator is told what happened — silently returning 'unknown'
# would render the dropdown on '— ข้ามไฟล์นี้ —' with no reason given, and
# REMOVING the type from the dropdown while the detector still emits it is worse
# still: no <option> matches, so the browser selects the FIRST one ('ขาย') and
# an unchanged confirm would feed a ลูกหนี้คงค้าง report to the SALES importer,
# which writes sales_transactions and moves stock (Codex review, 2026-08-22).
_RETIRED_TO_DBF = (
    "ทางนำเข้าแบบไฟล์รายงานสำหรับ ลูกหนี้/เจ้าหนี้คงค้าง ปิดแล้ว — "
    "ยอดคงค้างมาจาก zip รายวันของ Express ที่หน้า นำเข้า Express (DBF) แทน"
)

# Ordered: the detector walks this list and takes the first match.
REPORT_TYPES: Tuple[ReportType, ...] = (
    # Credit notes first — both kinds carry 'ใบลดหนี้'.
    ReportType(
        key='credit_notes_ap',
        label='ใบลดหนี้ — ส่งคืน (ผู้ขาย)',
        title_markers=('ใบลดหนี้',),
        # The supplier side is the QUALIFIED one. Without this the ordered walk
        # would hand EVERY ใบลดหนี้ — customer returns included — to the AP
        # importer, which is the wrong direction for the money.
        title_requires=('ส่งคืน',),
        express_kind='credit_notes',
        dbf_count_field='imported',
        book_meta_key='credit_notes_ap',
    ),
    ReportType(
        key='credit_notes_ar',
        label='ใบลดหนี้ — รับคืน (ลูกค้า)',
        title_markers=('ใบลดหนี้',),
        # 'รับคืนสินค้า' or unqualified — anything that is not the ส่งคืน side.
        title_excludes=('ส่งคืน',),
        dbf_count_field='upserted',
        book_meta_key='credit_notes_ar',
    ),
    ReportType(
        key='payments_in',
        label='การรับชำระหนี้ (ลูกหนี้)',
        title_markers=('การรับชำระหนี้',),
        supports_removals=True,
        dbf_count_field='imported',
        book_meta_key='payments_in',
    ),
    ReportType(
        key='payments_out',
        label='การจ่ายชำระหนี้ (เจ้าหนี้)',
        title_markers=('การจ่ายชำระหนี้',),
        express_kind='payments_out',
        dbf_count_field='imported',
        book_meta_key='payments_out',
    ),
    ReportType(
        key='ar_snapshot',
        label='ลูกหนี้คงค้าง — ปิดแล้ว ใช้ zip รายวัน',
        title_markers=('ลูกหนี้คงค้าง',),
        retired_reason=_RETIRED_TO_DBF,
        dbf_count_field='imported',
        book_meta_key='ar_snapshot',
    ),
    ReportType(
        key='ap_snapshot',
        label='เจ้าหนี้คงค้าง — ปิดแล้ว ใช้ zip รายวัน',
        title_markers=('เจ้าหนี้คงค้าง',),
        retired_reason=_RETIRED_TO_DBF,
        dbf_count_field='imported',
        book_meta_key='ap_snapshot',
    ),
    # Specific sales/purchase report titles — NOT bare 'ขาย'/'ซื้อ', so the
    # wrong 'ขายเงินเชื่อ' report stays unknown.
    ReportType(
        key='sales',
        label='ขาย',
        title_markers=('ประวัติการขาย', 'รายงานการขาย'),
        supports_removals=True,
        dbf_count_field='imported',
        book_meta_key='sales_imported',
    ),
    ReportType(
        key='purchase',
        label='ซื้อ',
        title_markers=('ประวัติการซื้อ', 'รายงานการซื้อ'),
        supports_removals=True,
        dbf_count_field='imported',
        book_meta_key='purchase_imported',
    ),
    # The fallback. No markers, so `matches` is never true for it.
    #
    # This label reaches the preview table's label column (_REPORT_LABELS.get)
    # but NOT the <option>: import_box.html skips 'unknown' in the loop and
    # renders its own "— ข้ามไฟล์นี้ —" instead, so the operator is told what
    # will HAPPEN rather than what was detected. Verified unchanged from
    # origin/main, 2026-09-07 — do not "fix" the two strings to match.
    ReportType(
        key='unknown',
        label='— ไม่รู้จัก (เลือกเอง) —',
    ),
)

UNKNOWN = 'unknown'

BY_KEY = {rt.key: rt for rt in REPORT_TYPES}


def labels():
    """key -> Thai label, in declaration order. Feeds the /import-data
    <select>; every key stays selectable, retired ones included."""
    return {rt.key: rt.label for rt in REPORT_TYPES}


def retired_keys():
    return frozenset(rt.key for rt in REPORT_TYPES if rt.is_retired)


def retired_reason_for(key):
    """Why THIS type is refused, or None if it is live (or not a type at all).

    Per-type on purpose. The two retired types share one reason today, and a
    single module-level constant would work — right up until they don't, at
    which point the shared constant silently shows one type's reason for the
    other. Asking the type is the same length and cannot go wrong."""
    rt = BY_KEY.get(key)
    return rt.retired_reason if rt else None


def express_kinds():
    """report_type -> express_importer file_type, for the types that share the
    express_importer path."""
    return {rt.key: rt.express_kind for rt in REPORT_TYPES if rt.express_kind}


def removal_capable_keys():
    return frozenset(rt.key for rt in REPORT_TYPES if rt.supports_removals)


def dbf_count_fields():
    """report_type -> the per_type sub-key holding its row count. Only the
    types the DBF import actually reports appear."""
    return {rt.key: rt.dbf_count_field
            for rt in REPORT_TYPES if rt.dbf_count_field}


def book_meta_counts(per_type):
    """The report-type slice of vat_book_builder's book_meta.counts.

    Reads each type's own count field — credit_notes_ar reports 'upserted'
    where its siblings report 'imported', which used to be a hand-typed
    difference nothing pinned — and writes it under that type's own
    book_meta_key, which the import_express_dbf template reads BY NAME."""
    return {rt.book_meta_key: per_type[rt.key][rt.dbf_count_field]
            for rt in REPORT_TYPES
            if rt.book_meta_key and rt.dbf_count_field}
