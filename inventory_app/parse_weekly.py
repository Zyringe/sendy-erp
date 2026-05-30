"""
Parser for BSN weekly sales (ขาย) and purchase (ซื้อ) fixed-width report files.
Encoding: cp874  |  Lines are CSV-quoted  |  Non-breaking spaces (\xa0) used as padding
"""
import re


def _clean(line: str) -> str:
    return line.strip().strip('"').replace('\xa0', ' ')


def _be_to_iso(d: str) -> str:
    """DD/MM/YY Buddhist Era short year → YYYY-MM-DD Gregorian"""
    parts = d.strip().split('/')
    day, month, by = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{(2500 + by) - 543:04d}-{month:02d}-{day:02d}"


# BSN's discount columns accept: empty | percent (5%, 25+5%) | decimal baht (32.00, 14.00).
# Both the line-discount column ("ส่วนลด") and the doc-level discount column ("ส่วนลดรวม")
# share this format. The `.` and `%` are both essential — without them the regex shifts
# columns and either (a) absorbs the discount into total, or (b) truncates net at the
# percent sign. See test_parse_sales_decimal_baht_discount + test_parse_sales_doc_level_discount_percent.
_DISCOUNT_COL = r'[\d+%.]*'

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

    with open(filepath, encoding='cp874') as f:
        lines = [_clean(l) for l in f.readlines()]

    for line in lines:
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
        if re.search(r'\d{2}/\d{2}/\d{2}', line) and current_prod_name:
            m = tx_pat.search(line)
            if m:
                try:
                    entry = {
                        'date_iso':         _be_to_iso(m.group(1)),
                        'doc_no':           re.sub(r'\s+', '', m.group(2)),
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
                except (ValueError, IndexError):
                    pass

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


# Two 4-digit Buddhist-era years (e.g. "2567" and "2569") anywhere on a line.
_TWO_THAI_YEARS_RE = re.compile(r'(25\d\d).*?(25\d\d)')


def is_history_export(filepath: str) -> bool:
    """
    Return True when the file is a full-history Express export
    (ประวัติการขาย_แยกตามลูกค้า / ประวัติการซื้อ_…) rather than a
    normal weekly BSN file.

    Two independent signals must BOTH be true:

    Signal 1 — title line contains both "ประวัติ" (history) and "แยกตาม"
               (grouped-by). Weekly files share this title; the signal
               alone is not sufficient.

    Signal 2 — the "วันที่จาก" (date-from) header line contains two
               Buddhist-era 4-digit years and the start year is strictly
               less than the end year.  Weekly files always have
               start_year == end_year (same calendar year); a full-history
               export starts 1–3 years earlier.

    Requires BOTH signals to be true so a valid weekly file is never
    rejected (conservative / no false-positives).

    Only the first ~15 lines are read (the header section).
    """
    title_match = False
    date_range_match = False

    try:
        with open(filepath, encoding='cp874') as f:
            for i, raw in enumerate(f):
                if i >= 15:
                    break
                c = _clean(raw)
                # Signal 1: report title line
                if 'ประวัติ' in c and 'แยกตาม' in c:
                    title_match = True
                # Signal 2: วันที่จาก line with multi-year span
                if 'วันที่จาก' in c:
                    m = _TWO_THAI_YEARS_RE.search(c)
                    if m:
                        start_year = int(m.group(1))
                        end_year = int(m.group(2))
                        if start_year < end_year:
                            date_range_match = True
    except (OSError, UnicodeDecodeError):
        return False

    return title_match and date_range_match


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
