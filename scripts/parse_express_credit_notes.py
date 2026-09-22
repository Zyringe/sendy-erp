"""Parse Express ใบลดหนี้ (credit-note / goods-return) report.

Express exports this as a fixed-width pseudo-CSV with cp874 encoding,
quoted lines, embedded page breaks, and multi-row records (header +
detail lines + optional notes block). pandas/csv won't help — we walk
the file line by line as a small state machine.

CLI:
    python scripts/parse_express_credit_notes.py PATH_TO_CSV [--json]

Library:
    from scripts.parse_express_credit_notes import parse_credit_notes
    records = list(parse_credit_notes(Path('...')))
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ── Date conversion (พ.ศ. → ค.ศ.) ─────────────────────────────────────────────
_DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{2})$')


def thai_date_to_iso(s):
    """'17/01/67' (BE 2567) → '2024-01-17' (Gregorian). None on bad input.

    Express prints the year as 2 digits of the Buddhist Era (e.g. '67'
    means BE 2567). AD = BE - 543, so AD = 2500 + yy - 543 = 1957 + yy.
    """
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    dd, mm, yy = m.groups()
    year = 1957 + int(yy)  # 67 → 2024, 68 → 2025, 69 → 2026
    return f'{year:04d}-{int(mm):02d}-{int(dd):02d}'


# ── Line classifiers ─────────────────────────────────────────────────────────
# Page-header / metadata / separator rows that we skip outright.
_SKIP_RE = re.compile(
    r'^\s*$'                                                 # blank
    r'|^\(.*?\)บจก\.|^\s*\(BSN\)'                             # company header
    r'|^\s*รายงาน'                                             # report title
    r'|^\s*วันที่จาก'                                            # date range
    r'|^\s*เลขที่จาก'                                            # doc range
    r'|^\s*ผู้จำหน่าย\s'                                          # supplier filter
    r'|^\s*[-=_]{10,}'                                       # rule line (single)
    r'|^\s+[-=_]{5,}\s+[-=_]{5,}'                            # split rules
    r'|^\s*เลขที่\s+วันที่'                                      # column header row 1
    r'|^\s*คืน รายละเอียด'                                     # column header row 2
    r'|^\s*รวม\s+\d+\s+ใบ'                                    # final summary row
    r'|^หมายเหตุ:\s+เอกสาร'                                   # global legend (file footer)
    r'|^>{3,}|^<{3,}'                                         # ">>>> จบรายงาน <<<<"
)

# Main record row. Examples:
#   "  GR6600017    12/02/67  กนก                        RR6600386    1     625.00     0.00    625.00    Y      2"
#   " *GR6700007    28/05/67  กิจนำ                      RR6700100    1       0.00     0.00      0.00    Y      2"
#   "  GR69000001   09/01/69  กิจนำ                      RR6800367    0     389.51     0.00    389.51    N      2"
# Doc number = GR + 7..9 digits (Express varies year-to-year).
_MAIN_RE = re.compile(
    r'^\s*(?P<void>\*)?\s*'
    r'(?P<doc_no>GR\d{6,9})\s+'
    r'(?P<date_thai>\d{2}/\d{2}/\d{2})\s+'
    r'(?P<supplier>\S(?:.*?\S)?)\s{2,}'                        # supplier (single-space tolerant)
    r'(?P<ref_doc>\S+)\s+'                                      # reference doc (RR/HP/...)
    r'(?P<v_flag>\d+)\s+'                                       # V flag
    r'(?P<discount>[\d,.\-]+)\s+'                               # ส่วนลด
    r'(?P<vat>[\d,.\-]+)\s+'                                    # VAT
    r'(?P<total>[\d,.\-]+)\s+'                                  # รวมทั้งสิ้น
    r'(?P<cleared>[YN])\s+'                                     # ตัดหนี้แล้ว
    r'(?P<type_code>\d+)\s*$'                                   # ประเภท
)

# Detail row left-side: cleared, line_no, product_code, name, qty+unit.
# Trailing values (unit_price, optional discount, line_total) handled by
# token-split of `rest` because they can be 0, 2, or 3 tokens depending
# on the record style:
#   2 tokens : unit_price + line_total       (no discount applied)
#   3 tokens : unit_price + discount + line_total
#   0 tokens : exchange/replacement record (no money, qty only)
#
# Examples:
#   "     Y   1 561ต1060  ตลับเมตร 5 เมตร Bull tech       25.00ลูก     25.00          625.00"
#   "     Y   1 529ป6140  แปรงทาสี SUPER-808(4นิ้ว)        2.00โหล   1500.00   30%    2100.00"
#   "     Y   1 030บ5100  บานพับสแตนเลส 4\"ไม่มีแหวน    242.00ตัว"
#   "     N   1 528ก2215  กระดาษทรายม้วน#80'HORSE SHOE'        ม้วน      60.00   35%   78.00"
_DETAIL_RE = re.compile(
    r'^\s{4,}'
    r'(?P<cleared>[YN])\s+'
    r'(?P<line_no>\d+)\s+'
    r'(?P<product_code>\S+)\s+'
    r'(?P<product_name>.+?)\s{2,}'
    r'(?:'
        r'(?P<qty>[\d,.]+)(?P<unit>\S+)'                        # qty + unit jammed together
        r'|(?P<unit_only>\S+)'                                   # OR unit alone (qty empty)
    r')'
    r'(?P<rest>.*?)\s*$'
)

# Note-section header. After this, indented lines until blank are notes.
_NOTE_HDR_RE = re.compile(r'^\s+หมายเหตุ:\s*$')

# Anything else heavily indented (8+ leading spaces) and not a detail row →
# treat as informal exchange/note text. e.g.
#   "        ขอเปลี่ยน"
#   "        ดจ./ไม้ไฟฟ้า 3/8x8\"รุ่นมีเดือย 2 โหล"
_INFORMAL_INDENT_RE = re.compile(r'^\s{6,}\S')


# ── Data classes ─────────────────────────────────────────────────────────────
@dataclass
class CreditNoteLine:
    line_no: int
    product_code: str
    product_name: str
    qty: float
    unit: str
    unit_price: float
    discount: str
    line_total: float
    is_cleared: bool


@dataclass
class CreditNote:
    doc_no: str
    date_iso: str
    supplier_name: str
    ref_doc: str
    v_flag: int
    discount: float
    vat: float
    total: float
    is_cleared: bool
    is_void: bool
    type_code: int
    note: str = ''
    lines: list = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _to_float(s):
    if s is None:
        return None
    s = s.replace(',', '').strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _strip_quotes(line):
    """Express wraps every line in quotes. Remove them safely."""
    line = line.rstrip('\r\n')
    if len(line) >= 2 and line[0] == '"' and line[-1] == '"':
        return line[1:-1]
    return line


def _parse_detail_tail(rest):
    """Split the detail-row tail into (unit_price, discount, line_total).

    The tail can be 0, 2, or 3 whitespace-separated tokens.
    """
    tokens = rest.split()
    if not tokens:
        return None, None, None
    if len(tokens) == 2:
        return _to_float(tokens[0]), None, _to_float(tokens[1])
    if len(tokens) == 3:
        return _to_float(tokens[0]), tokens[1], _to_float(tokens[2])
    # Unexpected token count — best effort: use first as unit_price, last as total.
    return _to_float(tokens[0]), ' '.join(tokens[1:-1]), _to_float(tokens[-1])


# ── Main parser ──────────────────────────────────────────────────────────────
def parse_credit_notes(path):
    """Yield CreditNote objects parsed from Express ใบลดหนี้ report."""
    current = None
    in_note_block = False
    note_lines = []

    def _flush_notes():
        nonlocal in_note_block, note_lines
        if in_note_block and current is not None and note_lines:
            existing = current.note
            joined = '\n'.join(note_lines).strip()
            current.note = (existing + '\n' + joined).strip() if existing else joined
        in_note_block = False
        note_lines = []

    with open(path, 'r', encoding='cp874') as f:
        for raw in f:
            line = _strip_quotes(raw)

            if not line.strip():
                _flush_notes()
                continue

            if _SKIP_RE.match(line):
                _flush_notes()
                continue

            m = _MAIN_RE.match(line)
            if m:
                _flush_notes()
                if current is not None:
                    yield current
                current = CreditNote(
                    doc_no=m.group('doc_no'),
                    date_iso=thai_date_to_iso(m.group('date_thai')),
                    supplier_name=m.group('supplier').strip(),
                    ref_doc=m.group('ref_doc'),
                    v_flag=int(m.group('v_flag')),
                    discount=_to_float(m.group('discount')) or 0.0,
                    vat=_to_float(m.group('vat')) or 0.0,
                    total=_to_float(m.group('total')) or 0.0,
                    is_cleared=m.group('cleared') == 'Y',
                    is_void=m.group('void') == '*',
                    type_code=int(m.group('type_code')),
                )
                continue

            if _NOTE_HDR_RE.match(line):
                in_note_block = True
                note_lines = []
                continue

            if in_note_block:
                note_lines.append(line.strip())
                continue

            m = _DETAIL_RE.match(line)
            if m and current is not None:
                qty = _to_float(m.group('qty'))
                # "2.00!หล": the '!' Express glues on flags an odd factor; it is
                # not part of the unit (parse_weekly strips it the same way).
                unit = (m.group('unit') or m.group('unit_only')).replace('!', '')
                unit_price, discount, line_total = _parse_detail_tail(m.group('rest') or '')
                current.lines.append(CreditNoteLine(
                    line_no=int(m.group('line_no')),
                    product_code=m.group('product_code'),
                    product_name=m.group('product_name').strip(),
                    qty=qty,
                    unit=unit,
                    unit_price=unit_price,
                    discount=discount,
                    line_total=line_total,
                    is_cleared=m.group('cleared') == 'Y',
                ))
                continue

            # Informal exchange/note text appearing outside a หมายเหตุ block.
            if _INFORMAL_INDENT_RE.match(line) and current is not None:
                existing = current.note
                add = line.strip()
                current.note = (existing + '\n' + add).strip() if existing else add
                continue

            print(f'[parser] skipped unrecognised line: {line!r}', file=sys.stderr)

    _flush_notes()
    if current is not None:
        yield current


# ── CLI ──────────────────────────────────────────────────────────────────────
def _summarise(records):
    n_records = len(records)
    n_lines = sum(len(r.lines) for r in records)
    total_amt = sum(r.total for r in records)
    cleared = sum(1 for r in records if r.is_cleared)
    void = sum(1 for r in records if r.is_void)
    suppliers = sorted({r.supplier_name for r in records})
    print(f'Records      : {n_records}')
    print(f'Detail lines : {n_lines}')
    print(f'Total amount : {total_amt:,.2f}')
    print(f'Cleared (Y)  : {cleared}')
    print(f'Void (*)     : {void}')
    print(f'Suppliers    : {len(suppliers)}')
    for s in suppliers:
        n = sum(1 for r in records if r.supplier_name == s)
        amt = sum(r.total for r in records if r.supplier_name == s)
        print(f'  {s:<20s}  n={n:<3d}  amt={amt:>12,.2f}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', type=Path)
    ap.add_argument('--json', action='store_true', help='emit JSON instead of summary')
    ap.add_argument('--limit', type=int, default=None, help='only first N records')
    args = ap.parse_args()

    records = list(parse_credit_notes(args.path))

    if args.json:
        slice_ = records[:args.limit] if args.limit else records
        out = [asdict(r) for r in slice_]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
        return

    _summarise(records)


if __name__ == '__main__':
    main()
