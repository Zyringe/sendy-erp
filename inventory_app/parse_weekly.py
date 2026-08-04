"""
Parser for BSN weekly sales (ขาย) and purchase (ซื้อ) fixed-width report files.
Encoding: cp874  |  Lines are CSV-quoted  |  Non-breaking spaces (\xa0) used as padding
"""
import datetime
import os
import re


def _clean(line: str) -> str:
    return line.strip().strip('"').replace('\xa0', ' ')


def _be_to_iso(d: str) -> str:
    """DD/MM/YY Buddhist Era short year → YYYY-MM-DD Gregorian"""
    parts = d.strip().split('/')
    day, month, by = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{(2500 + by) - 543:04d}-{month:02d}-{day:02d}"


# BSN's discount columns accept: empty | percent (5%, 25+5%) | decimal baht (32.00, 14.00)
# | comma-thousands baht (1,800.00). Both the line-discount column ("ส่วนลด") and the
# doc-level discount column ("ส่วนลดรวม") share this format. The `.`, `%` and `,` are all
# essential — without them the regex shifts columns and either (a) absorbs the discount
# into total, (b) truncates net at the percent sign, or (c) — when the doc-discount column
# is a comma'd value like '1,800.00' — fails to consume it, so `net` grabs the discount
# column instead of the true last column (RR6700192: net read as 1,800.00 not 4358.93).
# See test_parse_sales_decimal_baht_discount + test_parse_sales_doc_level_discount_percent
# + test_purchase_net_with_comma_doc_discount.
_DISCOUNT_COL = r'[\d,+%.]*'

# BSN occasionally glues qty and unit with '!' instead of whitespace (e.g. "2.00!หล").
# `.replace('!', '')` on the captured groups strips the artifact at extract time.
_QTY_UNIT_SEP = r'[\s!]+'

# Sales doc no has embedded spaces: "IV6900478-  1"  → normalise to "IV6900478-1"
_TX_SALES = re.compile(
    r'(\d{2}/\d{2}/\d{2})\s+(\w+\-\s*\d+)\s+'           # date  doc_no
    rf'([\d,]+\.?\d*){_QTY_UNIT_SEP}(\S+)\s+'            # qty [\s!]+ unit
    r'([\d,]+\.?\d*)\s+(\d)\s*'                          # unit_price  vat_type
    rf'({_DISCOUNT_COL})\s+([\d,]+\.?\d*)\s+'            # discount  total
    rf'{_DISCOUNT_COL}\s+([\d,]+\.?\d*)'                 # doc_disc (ignored)  net
)

# Purchase doc no is a single token: "HP6900017"
_TX_PURCH = re.compile(
    r'(\d{2}/\d{2}/\d{2})\s+(\S+)\s+'
    rf'([\d,]+\.?\d*){_QTY_UNIT_SEP}(\S+)\s+'            # qty [\s!]+ unit
    r'([\d,]+\.?\d*)\s+(\d)\s*'
    rf'({_DISCOUNT_COL})\s+([\d,]+\.?\d*)\s+'
    rf'{_DISCOUNT_COL}\s+([\d,]+\.?\d*)'
)

_SKIP_PREFIXES = (
    '(BSN)', 'รายงาน', 'รหัส', 'วันที่', 'พนักงาน',
    'เลือก', 'สินค้า วัน', 'รวมตาม', '-----------', '===========',
)


def _is_skip(s: str) -> bool:
    return any(s.startswith(p) for p in _SKIP_PREFIXES) or \
           bool(re.match(r'^[-=\s]+$', s))


# Return / credit-note (ใบลดหนี้) rows are interleaved into the SAME ขาย report
# but carry an extra Y/N clearing marker between the unit and the unit price, so
# _TX_SALES can never match them. They are imported by import_credit_notes.py
# (the ใบลดหนี้ path) — skipping them here is by design, not a parse failure.
#
# Measured 2026-08-03 over every real Express CSV in inventory_app/imports/:
# 269 of 272 unmatched transaction-candidate lines were SR docs. The other 3
# were IV lines with a NEGATIVE unit price, which _validate_parse now surfaces
# instead of dropping silently.
_SR_DOC_LINE = re.compile(r'\d{2}/\d{2}/\d{2}\s+SR\d')


def _validate_parse(filepath: str, entries: list, rejected: list, candidates: int):
    """Fail loudly on a zero/partial parse.

    `rejected` = transaction-candidate lines that had product context but could
    not be turned into an entry. `candidates` = every line that reached the
    transaction branch, product context or not — so a file whose product
    headings never matched (0 entries, 0 rejections) is still caught.

    Called from `_parse`, which is the single code path behind BOTH
    `preview_file` and `commit_file`, so the check cannot be bypassed by
    posting straight to confirm.
    """
    name = os.path.basename(filepath)
    if rejected:
        shown = "\n".join(f"  บรรทัด {ln}: {text[:100]}" for ln, text in rejected[:5])
        more = f"\n  … และอีก {len(rejected) - 5} บรรทัด" if len(rejected) > 5 else ""
        raise ValueError(
            f"อ่านไฟล์ {name} ไม่ครบ — มี {len(rejected)} บรรทัดรายการที่แปลงไม่ได้ "
            f"(อ่านสำเร็จ {len(entries)} บรรทัด). ยกเลิกการนำเข้าไว้ก่อน "
            f"เพราะนำเข้าแค่บางส่วนจะทำให้ยอดขาย/สต็อกขาด:\n" + shown + more
        )
    if candidates and not entries:
        raise ValueError(
            f"อ่านไฟล์ {name} ไม่ได้เลย — พบบรรทัดที่หน้าตาเป็นรายการ {candidates} บรรทัด "
            f"แต่แปลงได้ 0 รายการ. อาจเลือกประเภทรายงานผิด หรือรูปแบบไฟล์เปลี่ยน — "
            f"ตรวจไฟล์ก่อนนำเข้าใหม่"
        )


# Brand-name typos in BSN's source data.
# Pattern → replacement. Case-insensitive, word-boundary safe.
# Add new aliases here when discovered; matching happens at parse time so
# both new imports and re-imports of historical files land with the canonical name.
_BRAND_ALIASES = [
    (re.compile(r'\bBROVO\b', re.IGNORECASE), 'BRAVO'),
]


def _apply_brand_aliases(name: str) -> str:
    if not name:
        return name
    for pat, repl in _BRAND_ALIASES:
        name = pat.sub(repl, name)
    return name


def parse_sales(filepath: str) -> list:
    return _parse(filepath, _TX_SALES, 'sales')


def parse_purchases(filepath: str) -> list:
    return _parse(filepath, _TX_PURCH, 'purchase')


def _parse(filepath: str, tx_pat, file_type: str) -> list:
    entries = []
    current_party = current_party_code = None
    current_prod_name = current_prod_code = None
    # 1-based counter per (doc_no, product_code) so duplicate lines of the same
    # product within one document (e.g. split-price purchase rows, or a product
    # appearing twice on one invoice) get a stable identity for the idempotent
    # upsert key. Mirrors scripts/parse_express_purchase_history.py's line_seq.
    _line_seq: dict = {}

    with open(filepath, encoding='cp874') as f:
        lines = [_clean(l) for l in f.readlines()]

    # Reject VAT-entity files (บริษัท บุญสวัสดิ์ นำชัย จำกัด). NoVAT files always
    # start with "(BSN)"; VAT files start with "บริษัท". Importing the wrong entity
    # silently adds duplicate IV26* doc_nos that pollute revenue and stock.
    if lines and 'บริษัท บุญสวัสดิ์ นำชัย' in lines[0]:
        raise ValueError(
            "ไฟล์นี้เป็นของ VAT entity (บริษัท บุญสวัสดิ์ นำชัย จำกัด) — "
            "ห้าม import เข้า Sendy ใช้ไฟล์ของ (BSN)บจก.บุญสวัสดิ์นำชัย เท่านั้น"
        )

    # Parse-integrity accounting (see _validate_parse): `candidates` counts every
    # line that reaches the transaction branch; `rejected` holds the ones that
    # had product context but produced no entry.
    candidates = 0
    rejected = []

    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        stripped = line.strip()
        lead = len(line) - len(line.lstrip())

        if _is_skip(stripped):
            continue

        # Party line (customer / supplier): 2 leading spaces, has /code
        if lead == 2 and '/' in stripped and not stripped.startswith('รวม'):
            m = re.match(r'^(.+?)\s*/(\S+)\s*$', stripped)
            if m:
                current_party = m.group(1).strip()
                current_party_code = m.group(2).strip()
            continue

        # Product line: 3 leading spaces, has /code, not a total
        if lead == 3 and '/' in stripped and not stripped.startswith('รวม'):
            m = re.match(r'^(.+?)\s*/(\S+)\s*$', stripped)
            if m:
                current_prod_name = _apply_brand_aliases(m.group(1).strip())
                current_prod_code = m.group(2).strip()
            continue

        # Transaction line: contains a date
        if re.search(r'\d{2}/\d{2}/\d{2}', line):
            # ใบลดหนี้ row — owned by import_credit_notes. Skipped BEFORE the
            # candidate count, otherwise a quiet week whose only rows are returns
            # would look like "candidates but zero entries" and be wrongly
            # blocked as a failed parse.
            if _SR_DOC_LINE.search(stripped):
                continue
            candidates += 1
            if not current_prod_name:
                continue                    # counted, but only _validate_parse's zero-parse
                                            # rule acts on it (see its docstring)
            m = tx_pat.search(line)
            if m:
                try:
                    doc_no = re.sub(r'\s+', '', m.group(2))
                    seq_key = (doc_no, current_prod_code)
                    seq = _line_seq.get(seq_key, 0) + 1
                    _line_seq[seq_key] = seq
                    entry = {
                        'date_iso':         _be_to_iso(m.group(1)),
                        'doc_no':           doc_no,
                        'line_seq':         seq,
                        'qty':              float(m.group(3).replace(',', '').replace('!', '')),
                        'unit':             m.group(4).replace('!', ''),
                        'unit_price':       float(m.group(5).replace(',', '')),
                        'vat_type':         int(m.group(6)),
                        'discount':         m.group(7).strip(),
                        'total':            float(m.group(8).replace(',', '')),
                        'net':              float(m.group(9).replace(',', '')),
                        'product_name_raw': current_prod_name,
                        'product_code_raw': current_prod_code,
                        'party':            current_party,
                        'party_code':       current_party_code,
                    }
                    entries.append(entry)
                except (ValueError, IndexError) as exc:
                    rejected.append((lineno, f"{stripped[:90]}  [{exc}]"))
            else:
                rejected.append((lineno, stripped))

    _validate_parse(filepath, entries, rejected, candidates)
    return entries


def detect_file_type(filepath: str) -> str:
    """Return 'sales' or 'purchase' based on file content."""
    with open(filepath, encoding='cp874') as f:
        for line in f:
            c = _clean(line)
            if 'ใบลดหนี้' in c or 'รับคืนสินค้า' in c:
                return 'credit_note'
            if 'ขาย' in c:
                return 'sales'
            if 'ซื้อ' in c:
                return 'purchase'
    return 'unknown'


# Thai month abbreviations (trailing dot included) → month number.
_THAI_MONTH_ABBR = {
    'ม.ค.': 1, 'ก.พ.': 2, 'มี.ค.': 3, 'เม.ย.': 4,
    'พ.ค.': 5, 'มิ.ย.': 6, 'ก.ค.': 7, 'ส.ค.': 8,
    'ก.ย.': 9, 'ต.ค.': 10, 'พ.ย.': 11, 'ธ.ค.': 12,
}

# "วันที่จาก  1 มี.ค. 2569  ถึง  19 เม.ย. 2569"
#   → (start_day, start_month, start_BEyear, end_day, end_month, end_BEyear)
_DATE_FROM_RE = re.compile(
    r'วันที่จาก\s+(\d{1,2})\s+(\S+?)\s+(25\d\d)\s+ถึง\s+(\d{1,2})\s+(\S+?)\s+(25\d\d)'
)

# "วันที่ : 20/04/69"  → the export (report-run) date, DD/MM/YY Buddhist short year.
# The negative lookahead avoids matching the "วันที่จาก" line (no colon there).
_REPORT_DATE_RE = re.compile(r'วันที่\s*:\s*(\d{2})/(\d{2})/(\d{2})')

# A normal weekly import reaches back only a handful of days; a full-history
# export reaches back to the start of the data (months–years).
#
# 42 is MEASURED, not guessed (2026-08-03, every real Express export in
# inventory_app/imports/): the widest genuine weekly reaches back 36 days
# (ขาย_4.5.69.csv / ซื้อ_4.5.69.csv — 243/243 and 16/16 of their doc_nos are
# live in the ERP, so they really were imported), and the narrowest single-year
# history export reaches back 50 (ประวัติการขาย_1.3.69-19.4.69.csv). Anything in
# 37..49 separates them; 42 leaves ~6 days of headroom on each side.
#
# It was 31 while this guard had no production caller, which refused those two
# real weeklies the moment it became load-bearing. Put chose to widen it rather
# than make the team re-export (2026-08-03).
_HISTORY_REACH_BACK_DAYS = 42


def _thai_be_date(day, thai_month, be_year):
    """(day, 'มี.ค.', BE-year) → datetime.date (Gregorian), or None if unparseable."""
    month = _THAI_MONTH_ABBR.get(thai_month)
    if not month:
        return None
    try:
        return datetime.date(int(be_year) - 543, month, int(day))
    except (ValueError, TypeError):
        return None


def _read_header_dates(filepath: str):
    """Pull the export's date evidence out of the header (first ~15 lines).

    Returns (filter_start, report_date, filter_year_span); any element is None
    when that part could not be read. Shared by is_history_export() and
    date_filter_is_readable() so the two can never disagree about a file.
    """
    filter_start = report_date = None
    filter_year_span = None  # end_BEyear − start_BEyear (set even if a month is unparseable)
    try:
        with open(filepath, encoding='cp874') as f:
            for i, raw in enumerate(f):
                if i >= 15:
                    break
                c = _clean(raw)
                if report_date is None:
                    rm = _REPORT_DATE_RE.search(c)
                    if rm:
                        dd, mm, by = rm.groups()
                        try:
                            report_date = datetime.date(
                                2500 + int(by) - 543, int(mm), int(dd))
                        except (ValueError, TypeError):
                            report_date = None
                if filter_year_span is None and 'วันที่จาก' in c:
                    dm = _DATE_FROM_RE.search(c)
                    if dm:
                        sd, smon, sy, ed, emon, ey = dm.groups()
                        filter_start = _thai_be_date(sd, smon, sy)
                        # Year span is robust even when a Thai month abbreviation
                        # is not in the map (so a multi-year dump is still caught).
                        filter_year_span = int(ey) - int(sy)
    except (OSError, UnicodeDecodeError):
        return None, None, None
    return filter_start, report_date, filter_year_span


def date_filter_is_readable(filepath: str) -> bool:
    """True when the header carries a usable "วันที่จาก" range AND report date.

    The weekly importer REFUSES a sales/purchase file for which this is False
    (Put's policy A, tightened 2026-08-03). Rationale: the guard's only job is
    to make silent acceptance of a history dump impossible, and a header we
    cannot read is exactly the shape a "give me everything" export produces —
    a blank filter, or no filter line at all. Erring toward acceptance there
    pointed the fallback at the one outcome the guard exists to prevent.
    Refusing costs the operator a re-export; accepting rewrites months of stock.
    """
    filter_start, report_date, _span = _read_header_dates(filepath)
    return bool(filter_start and report_date)


def is_history_export(filepath: str) -> bool:
    """
    Return True when the file is a full-history Express export
    (ประวัติการขาย_แยกตามลูกค้า / ประวัติการซื้อ_…) rather than a
    normal weekly BSN file.

    Weekly and history files are the SAME Express report, so the title cannot
    tell them apart. The DATES do: the "วันที่จาก" filter START is far before the
    export's report date ("วันที่ :") for a history dump, and a few days before
    it for a weekly increment. We measure start-vs-report, NOT the filter span,
    because Express defaults the "ถึง" end to 31 ธ.ค. even on weekly exports (so
    the end is meaningless on its own).

    ⚠ There is deliberately NO title gate. It used to require a line containing
    both "ประวัติ" AND "แยกตาม", which is NARROWER than what
    import_router.detect_express_report() accepts for the stock-writing path
    ("ประวัติการขาย" / "รายงานการขาย" / "ประวัติการซื้อ" / "รายงานการซื้อ"). A
    full-history export titled "รายงานการขาย แยกตามลูกค้า" or "รายงานประวัติการขาย
    เรียงตามวันที่" therefore routed to sales and was never gated, no matter how
    far back it reached. The caller already knows the report type; the dates are
    the evidence. (Independent review, 2026-08-03.)

    A header whose dates cannot be read is NOT reported as history here — it is
    refused separately via date_filter_is_readable(), so the two failure modes
    keep distinct operator messages.

    Only the first ~15 lines are read (the header section).
    """
    filter_start, report_date, filter_year_span = _read_header_dates(filepath)

    # Reach-back is the PRIMARY signal: filter starts well before the export was
    # run → history. When it is computable it decides on its own — a multi-year
    # span always implies a large reach-back, so the span rule adds nothing here.
    #
    # ⚠ The span rule must NOT short-circuit this. Express defaults the "ถึง" end
    # to 31 ธ.ค. of the CURRENT year, so a weekly exported in early January runs
    # from the old BE year into the new one (span = 1) with a reach-back of a few
    # days. Checking the span first hard-blocked every January weekly import —
    # deterministic, once a year. See test_january_weekly_is_not_history.
    if filter_start and report_date:
        return (report_date - filter_start).days > _HISTORY_REACH_BACK_DAYS

    # FALLBACK only — reach-back could not be computed (e.g. a Thai month
    # abbreviation outside _THAI_MONTH_ABBR). The year span survives that, and a
    # multi-year span necessarily pulls old data.
    if filter_year_span is not None and filter_year_span > 0:
        return True

    return False


# ── Credit-note (ใบลดหนี้ / SR) parser ───────────────────────────────────────
#
# Source: Express export "ใบลดหนี้-DD.M.YY.csv" (cp874)
# Two-row hierarchy:
#
#   master line (leading 2 spaces):
#     SR_no  date(BE DD/MM/YY)  customer_name  salesperson  ref_invoice(IV…)
#     vat_type(0|1|2)  doc_discount  goods_value  VAT  total  Y_marker  type
#
#     - SR_no may be prefixed with '*' to mark cancelled (~3 in 2024-2026 file)
#     - salesperson can include letter suffix ("06-L")
#
#   detail line (leading 5 spaces, after master, may repeat):
#     Y  seq  bsn_code  product_name  qty+unit(GLUED)  unit_price
#     line_discount  amount  trailing_ref(IVxxxx-N or AVGPR-)
#
#     - bsn_code can itself contain '-' (e.g. "026ต2210-1")
#     - qty and unit are GLUED with no separator: "2.00แผง", "30.00ดอก"
#     - unit_price / line_discount / amount may be empty
#
# Each detail row is emitted as ONE entry. A master with zero detail rows
# (9 cases in source) yields ONE placeholder entry with bsn_code=None and
# zero qty so the SR is still tracked.

# Master row: SR no, date, customer, salesperson, [ref_invoice], vat_type,
#             [doc_disc], goods_val, vat_amt, total, Y/N marker.
# ref_invoice is OPTIONAL — some masters omit it (~5 in 2024-2026 file).
# trailing marker can be 'Y' (cleared/ตัดหนี้แล้ว) or 'N' (not yet cleared).
_SR_MASTER_RE = re.compile(
    r'^(\*?)(SR\d+)\s+'                          # cancel-flag, SR no
    r'(\d{2}/\d{2}/\d{2})\s+'                    # date BE
    r'(.+?)\s+'                                  # customer (lazy)
    r'(\d{2}(?:-[A-Z])?)\s+'                     # salesperson
    r'(?:([A-Z]{2}\d\S*)\s+)?'                   # ref invoice (optional, e.g. IV…/HS…)
    r'(\d)\s+'                                   # vat_type
    rf'({_DISCOUNT_COL})\s+'                     # doc-discount
    r'([\d,]+\.?\d*)\s+'                         # goods value
    r'([\d,]+\.?\d*)\s+'                         # VAT
    r'([\d,]+\.?\d*)\s+'                         # total
    r'[YN]'                                      # marker
)

# Detail header: "[YN] seq bsn_code  product_name  [qty<digits>.<digits>]unit"
# Columns separated by 2+ spaces. After this prefix, the remaining columns
# (unit_price / discount / amount / ref) are split on 2+ spaces and assigned
# positionally: see _parse_detail_line.
#
# Marker is 'Y' (cleared/ตัดหนี้แล้ว) or 'N' (record only / ยังไม่ตัดหนี้).
# qty is OPTIONAL — N rows often record the line without a return qty (e.g.
# "N   1 528ก2215  กระดาษทรายม้วน#80   ม้วน   120.00..."), in which case
# unit follows the product_name directly with no leading digits.
_SR_DETAIL_HEAD_RE = re.compile(
    r'^[YN]\s+(\d+)\s+(\S+)\s{2,}'               # marker, seq, bsn_code
    r'(.+?)\s{2,}'                                # product name (until 2+ space gap)
    r'(?:([\d,]+\.\d+))?([^\d\s.,][^\s]*)'       # OPTIONAL qty<digits>.<digits> + unit
    r'(.*)$'                                       # tail (trailing cols)
)


def _parse_float_or_zero(s):
    if s is None:
        return 0.0
    s = s.strip().replace(',', '')
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_credit_notes(filepath: str) -> list:
    """
    Parse Express credit-note (ใบลดหนี้/SR) report file.

    Returns list of dicts, one per detail line. Masters with no detail
    rows yield one placeholder entry (bsn_code=None, qty=0).
    """
    entries = []
    cur_master = None         # dict carrying master-level fields
    cur_master_emitted = False

    def flush_empty_master():
        # Emit placeholder if previous master had no details.
        nonlocal cur_master, cur_master_emitted
        if cur_master is not None and not cur_master_emitted:
            entries.append(_make_entry(cur_master, seq=1, detail=None))

    with open(filepath, encoding='cp874') as f:
        raw_lines = f.readlines()

    for raw in raw_lines:
        line = _clean(raw)
        if not line:
            continue
        stripped = line.lstrip()

        # Master row?
        m = _SR_MASTER_RE.match(stripped)
        if m:
            # Closing previous master if no detail emitted
            flush_empty_master()
            (cancel, sr_no, date_be, customer, salesperson, ref_inv,
             vat_type, doc_disc, goods_val, vat_amt, total_amt) = m.groups()
            cur_master = {
                'sr_no':       sr_no,
                'cancelled':   bool(cancel),
                'date_iso':    _be_to_iso(date_be),
                'customer':    customer.strip(),
                'salesperson': salesperson.strip(),
                'ref_invoice': ref_inv.strip() if ref_inv else None,
                'vat_type':    int(vat_type),
                'doc_disc':    doc_disc.strip(),
                'goods_val':   _parse_float_or_zero(goods_val),
                'vat_amt':     _parse_float_or_zero(vat_amt),
                'total_amt':   _parse_float_or_zero(total_amt),
            }
            cur_master_emitted = False
            continue

        # Detail row? (Y = cleared, N = record only)
        if cur_master is not None and stripped[:1] in ('Y', 'N'):
            detail = _parse_detail_line(stripped)
            if detail:
                entries.append(_make_entry(cur_master, seq=detail['seq'], detail=detail))
                cur_master_emitted = True
            continue

        # Anything else (หมายเหตุ, page header, blank already filtered): ignore.

    flush_empty_master()
    return entries


def _parse_detail_line(stripped):
    """
    Parse one SR detail row.

    Layout (columns separated by 2+ spaces, qty+unit GLUED):

      Y  seq  bsn_code  product_name  qty<n>.<n>unit  [unit_price] [discount] [amount] [ref]

    The middle three numeric columns may be blank-padded; we identify them
    positionally after splitting on 2+ spaces:
      - discount: any token containing '%'  (e.g. '25%', '5+5%')
      - unit_price: first remaining numeric token (leftmost)
      - amount: last remaining numeric token (rightmost), or unit_price if only one
      - ref: any IV…/AVGPR… token (always last column)
    """
    m = _SR_DETAIL_HEAD_RE.match(stripped)
    if not m:
        return None
    seq, bsn_code, name, qty_s, unit, tail = m.groups()

    # Split tail on 2+ space gaps
    tokens = re.split(r'\s{2,}', tail.strip()) if tail.strip() else []
    tokens = [t for t in tokens if t]

    # Pull off trailing reference (IV…, AVGPR…) if present
    ref_line = None
    if tokens:
        last = tokens[-1]
        # Express splits "IV6602766-  1" into two tokens because of multi-space gap;
        # if last token is just digits and prev token ends with '-', glue them.
        if re.match(r'^\d+$', last) and len(tokens) >= 2 and tokens[-2].endswith('-'):
            ref_line = tokens[-2] + last
            tokens = tokens[:-2]
        elif re.match(r'^(IV\S*\-?|AVGPR\-?)\S*$', last) or 'AVGPR' in last:
            ref_line = last
            tokens = tokens[:-1]

    if ref_line:
        ref_line = re.sub(r'\s+', '', ref_line)

    # Now the remaining tokens are the numeric columns.
    discount = ''
    unit_price = 0.0
    amount = 0.0
    numerics = []
    for t in tokens:
        if '%' in t:
            discount = t
        else:
            numerics.append(t)
    if len(numerics) == 1:
        # Solo numeric → unit_price (no amount column)
        unit_price = _parse_float_or_zero(numerics[0])
    elif len(numerics) == 2:
        unit_price = _parse_float_or_zero(numerics[0])
        amount = _parse_float_or_zero(numerics[1])
    elif len(numerics) >= 3:
        # Three numerics with no '%' = unit_price, decimal-baht discount, amount
        unit_price = _parse_float_or_zero(numerics[0])
        discount = numerics[1]
        amount = _parse_float_or_zero(numerics[2])

    return {
        'seq':           int(seq),
        'bsn_code':      bsn_code,
        'product_name':  _apply_brand_aliases(name.strip()),
        'qty':           _parse_float_or_zero(qty_s),
        'unit':          unit,
        'unit_price':    unit_price,
        'discount':      discount.strip(),
        'amount':        amount,
        'ref_line':      ref_line,
    }


def _make_entry(master, seq, detail):
    """Combine master + detail into the canonical output dict."""
    sr_no = master['sr_no']
    if detail is None:
        # Placeholder for master-without-details
        return {
            'date_iso':         master['date_iso'],
            'doc_no':           f"{sr_no}-{seq}",
            'doc_base':         sr_no,
            'bsn_code':         None,
            'product_name_raw': None,
            'customer':         master['customer'],
            'salesperson':      master['salesperson'],
            'ref_invoice':      master['ref_invoice'],
            'ref_invoice_line': None,
            'vat_type':         master['vat_type'],
            'qty':              0.0,
            'unit':             '',
            'unit_price':       0.0,
            'discount':         '',
            'total':            master['total_amt'],
            'net':              master['total_amt'],
            'cancelled':        master['cancelled'],
        }
    return {
        'date_iso':         master['date_iso'],
        'doc_no':           f"{sr_no}-{seq}",
        'doc_base':         sr_no,
        'bsn_code':         detail['bsn_code'],
        'product_name_raw': detail['product_name'],
        'customer':         master['customer'],
        'salesperson':      master['salesperson'],
        'ref_invoice':      master['ref_invoice'],
        'ref_invoice_line': detail['ref_line'],
        'vat_type':         master['vat_type'],
        'qty':              detail['qty'],
        'unit':             detail['unit'],
        'unit_price':       detail['unit_price'],
        'discount':         detail['discount'],
        'total':            detail['amount'],
        'net':              detail['amount'],
        'cancelled':        master['cancelled'],
    }
