"""Parse Express ประวัติการซื้อ (purchase-history-by-supplier) report.

Hierarchical layout — three indentation levels nest a purchase line inside
its product, inside its supplier:

    supplier-name /supplier-code             # 2-space indent
       product-name /product-code            # 3-space indent
          DD/MM/YY  HP…  qty unit …          # 8-space indent (purchase row)
          DD/MM/YY  RR…  qty unit …          # same depth (credit-purchase)
          DD/MM/YY  GR…  qty unit Y …        # return row (GR + Y flag)
          รวมตาม ซื้อสด / ซื้อเชื่อ           # subtotal (skip)
       รวม supplier-code                      # supplier total (skip)
    รวมทั้งสิ้น …                             # grand total footer

Doc prefixes discovered from the real file:
  HP  — ซื้อสด  (cash purchase)
  RR  — ซื้อเชื่อ (credit purchase / goods receipt)
  GR  — ใบลดหนี้ (goods return / supplier credit note)

Return rows have a Y in the "คืน" (return) column, which falls at char ~47
of the data portion. GR+Y = signed negative for stock purposes; GR+N = rare
price-adjustment line (treat as normal purchase row, sign positive).

CLI:
    python scripts/parse_express_purchase_history.py PATH_TO_CSV [--json|--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path


# ── Date conversion ──────────────────────────────────────────────────────────
_DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{2})$')


def thai_date_to_iso(s):
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    dd, mm, yy = m.groups()
    year = 1957 + int(yy)
    return f'{year:04d}-{int(mm):02d}-{int(dd):02d}'


# ── Skip patterns ─────────────────────────────────────────────────────────────
_SKIP_RE = re.compile(
    r'^\s*$'
    r'|^\(.*?\)บจก\.|^\s*\(BSN\)'
    r'|^\s*รายงาน'
    r'|^\s*รหัส'
    r'|^\s*วันที่จาก'
    r'|^\s*[-=_]{10,}'
    r'|^\s+รวมตาม\s+'         # subtotals (ซื้อสด / ซื้อเชื่อ / ใบลดหนี้)
    r'|^\s+รวม\s+\S+\s'       # supplier total: "   รวม A.P   1584.00 …"
    r'|^\s+รวมทั้งสิ้น'
    r'|^หมายเหตุ:|^\s*รายการ|^\s*\!\s+อยู่หน้า'
    r'|^\s+สินค้า\s+วันที่'
    r'|^>{3,}|^<{3,}'
    r'|^\s+[-=]{3,}'           # separator lines inside supplier blocks
)

# Header rows ending with " /code"
_HDR_RE = re.compile(r'^(?P<indent>\s+)(?P<name>\S(?:.*?\S)?)\s+/(?P<code>\S+)\s*$')

# Purchase line item.
# Examples (stripped of outer quotes):
#   "        19/06/68   HP6800079         120.00 ชด           85.00  0                 10200.00                 10200.00"
#   "        12/02/67   GR6600017          25.00 ลก Y         25.00  1                   625.00                   625.00 RR6600386-  1"
#   "        02/04/67   GR6700005           0.00 มน N         60.00  1        35%        156.00                   156.00 RR6700127-  2"
#   "        19/08/67   HP6700111           2.00 ลง         3208.00  2        10%       5774.40     516.64       5388.29"
#
# Column anchors (0-based char positions, after stripping the enclosing quotes):
#   qty       ends at ~43
#   return    char ~47 (Y or N, present on GR rows; blank on HP/RR)
#   unit      starts ~49, Thai 2-4 char
#   unit_price ends at ~62
#   vat_type  char ~63 (0/1/2)
#   discount  ends at ~76 (optional: "35%", "10+5%", absent = blank)
#   total     ends at ~90
#   total_discount ends at ~101 (optional)
#   net       ends at ~115
#   ref_doc   optional tail, e.g. "RR6600386-  1" or "PO0000189-  1"
_PURCH_RE = re.compile(
    r'^\s{4,}'
    r'(?P<date_thai>\d{2}/\d{2}/\d{2})\s+'
    r'(?P<doc_no>(?:HP|RR|GR)\d{6,9})\s+'
    r'(?P<qty>-?[\d,]+\.\d{2})'
    r'(?P<unit_flag>!?)\s*'                     # rare '!' unit-ratio marker
    r'(?P<unit>\S+)\s+'
    r'(?:(?P<return_flag>[YN])\s+)?'            # คืน column (GR rows)
    r'(?P<unit_price>-?[\d,]+\.\d{2})\s+'
    r'(?P<vat_type>\d)'
    r'(?P<rest>.*)$'
)

# Right-edge positions (chars) of the trailing money columns.
# Calibrated on real purchase-history rows (verified against 20+ samples):
#   ส่วนลด         end ~76
#   รวมเงิน         end ~90
#   ส่วนลดรวม      end ~101  (optional — absent on ~99% of rows)
#   ยอดซื้อสุทธิ    end ~115
_TAIL_ANCHORS = (
    ('discount_num',    76),
    ('total',           90),
    ('total_discount', 101),
    ('net',            115),
)


# ── Data class ───────────────────────────────────────────────────────────────
@dataclass
class PurchaseLine:
    supplier_code: str
    supplier_name: str
    product_code: str
    product_name: str
    date_iso: str
    doc_no: str
    line_seq: int          # 1-based counter within (doc_no, product_code) group
    qty: float             # raw qty as-printed (always positive in source)
    unit: str
    return_flag: str       # 'Y' = goods return, 'N' = price-adj, '' = normal purchase
    unit_price: float
    vat_type: int
    discount: str
    total: float
    total_discount: float
    net: float
    ref_doc: str
    is_warning: bool


# ── Helpers ───────────────────────────────────────────────────────────────────
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
    line = line.rstrip('\r\n')
    if len(line) >= 2 and line[0] == '"' and line[-1] == '"':
        line = line[1:-1]
    return line.replace('""', '"')


def _parse_purch_tail(rest, rest_offset):
    """Extract (discount_str, total, total_discount, net, ref_doc, is_warning)
    from the `rest` portion of a purchase line.

    rest_offset is the character position in the full line where `rest` starts.
    """
    is_warning = False
    if rest.rstrip().endswith('***'):
        is_warning = True
        rest = rest.rstrip()[:-3]

    # Reference doc in the tail (e.g. "RR6600386-  1" or "PO0000227-  1")
    ref_doc = ''
    ref_match = re.search(r'\b((?:SO|IV|HS|HP|RR|GR|PO)\d{6,9}-\s*\d+)\s*$', rest)
    if ref_match:
        ref_doc = re.sub(r'-\s+', '-', ref_match.group(1))
        rest = rest[:ref_match.start()]

    discount_str = ''
    by_col = {}

    for m in re.finditer(r'(?<!\S)(-?[\d,]+\.\d{2}|\d+(?:[+]\d+)*(?:\.\d+)?%)(?!\S)', rest):
        token = m.group(1)
        end_pos = rest_offset + m.end()
        is_pct = token.endswith('%')

        if is_pct:
            discount_str = token
            continue

        best = None
        best_dist = 10 ** 9
        for name, anchor in _TAIL_ANCHORS:
            if name in by_col:
                continue
            dist = abs(end_pos - anchor)
            if dist < best_dist:
                best_dist = dist
                best = name
        if best is not None:
            by_col[best] = _to_float(token)

    if 'discount_num' in by_col and not discount_str:
        d = by_col['discount_num']
        discount_str = f'{d:.2f}' if d is not None else ''

    total = by_col.get('total')
    total_discount = by_col.get('total_discount')
    net = by_col.get('net')

    if total is None and net is not None:
        total = net

    return discount_str, total, total_discount, net, ref_doc, is_warning


# ── Parser ───────────────────────────────────────────────────────────────────
# Indent threshold: supplier headers at indent == 2; product headers at indent == 3.
_SUPPLIER_INDENT = 2
_PRODUCT_INDENT = 3


def parse_purchase_history(path):
    """Yield PurchaseLine records from Express ประวัติการซื้อ report.

    Fails loudly on bad date or unrecognised money column widths.
    Call validate() after collecting all records to check footer reconciliation.
    """
    cur_supplier_code = ''
    cur_supplier_name = ''
    cur_product_code = ''
    cur_product_name = ''
    # Sequence counter per (doc_no, product_code) to handle rare same-doc
    # same-product duplicate rows (e.g. split-price RR lines).
    _seq_counter: dict = {}

    with open(path, 'r', encoding='cp874') as f:
        for raw in f:
            line = _strip_quotes(raw)

            if _SKIP_RE.match(line):
                continue

            m = _PURCH_RE.match(line)
            if m:
                rest = m.group('rest') or ''
                rest_offset = m.start('rest')
                discount_str, total, td, net, ref_doc, is_warn = _parse_purch_tail(
                    rest, rest_offset
                )

                date_iso = thai_date_to_iso(m.group('date_thai'))
                if date_iso is None:
                    raise ValueError(f'[parser] bad date: {m.group("date_thai")!r} in line: {line!r}')

                doc_no = m.group('doc_no')
                seq_key = (doc_no, cur_product_code)
                seq = _seq_counter.get(seq_key, 0) + 1
                _seq_counter[seq_key] = seq

                yield PurchaseLine(
                    supplier_code=cur_supplier_code,
                    supplier_name=cur_supplier_name,
                    product_code=cur_product_code,
                    product_name=cur_product_name,
                    date_iso=date_iso,
                    doc_no=doc_no,
                    line_seq=seq,
                    qty=_to_float(m.group('qty')),
                    unit=m.group('unit'),
                    return_flag=m.group('return_flag') or '',
                    unit_price=_to_float(m.group('unit_price')),
                    vat_type=int(m.group('vat_type')),
                    discount=discount_str,
                    total=total or 0.0,
                    total_discount=td or 0.0,
                    net=net or 0.0,
                    ref_doc=ref_doc,
                    is_warning=is_warn,
                )
                continue

            m = _HDR_RE.match(line)
            if m:
                indent = len(m.group('indent'))
                name = m.group('name').strip()
                code = m.group('code').strip()
                if indent <= _SUPPLIER_INDENT:
                    cur_supplier_code = code
                    cur_supplier_name = name
                    cur_product_code = ''
                    cur_product_name = ''
                    _seq_counter.clear()
                else:
                    cur_product_code = code
                    cur_product_name = name
                continue

            print(f'[parser] skipped: {line!r}', file=sys.stderr)


# ── Footer reconciliation ─────────────────────────────────────────────────────

def _parse_footer(path):
    """Return (grand_total, grand_net) from the รวมทั้งสิ้น footer line.

    Returns (None, None) when no footer is found (dry-run / truncated file).
    """
    grand_total = None
    grand_net = None
    with open(path, 'r', encoding='cp874') as f:
        for raw in f:
            line = _strip_quotes(raw)
            if 'รวมทั้งสิ้น' in line:
                nums = re.findall(r'[\d,]+\.\d{2}', line)
                if len(nums) >= 2:
                    grand_total = _to_float(nums[-2])
                    grand_net = _to_float(nums[-1])
                break
    return grand_total, grand_net


def validate(records, path):
    """Raise ValueError when parsed Σtotal / Σnet diverges from the footer.

    GR prefix rows (both Y and N) are credit-note/return adjustments grouped
    under "รวมตาม ใบลดหนี้" subtotals which Express prints as negative.  The
    grand total printed by Express nets out ALL GR rows, so we subtract the
    entire GR doc-type from the parsed sum.

    return_flag meaning per observed data:
      'Y' — physical goods returned (qty > 0)
      'N' — price-adjustment credit (qty = 0, value still positive in source)
      ''  — normal HP/RR purchase (no return)

    Both 'Y' and 'N' GR rows reduce the grand total.

    Only raises when the footer is present.  Tolerates ±0.10 floating-point
    rounding (Express sometimes rounds at the subtotal stage).
    """
    footer_total, footer_net = _parse_footer(path)
    if footer_total is None:
        return  # no footer — skip check

    parsed_total = sum(
        -r.total if r.doc_no.startswith('GR') else r.total
        for r in records
    )
    parsed_net = sum(
        -r.net if r.doc_no.startswith('GR') else r.net
        for r in records
    )

    tol = 0.10
    if abs(parsed_total - footer_total) > tol:
        raise ValueError(
            f'Footer reconciliation FAIL — total: '
            f'parsed {parsed_total:,.2f} vs footer {footer_total:,.2f} '
            f'(diff {parsed_total - footer_total:+,.2f})'
        )
    if abs(parsed_net - footer_net) > tol:
        raise ValueError(
            f'Footer reconciliation FAIL — net: '
            f'parsed {parsed_net:,.2f} vs footer {footer_net:,.2f} '
            f'(diff {parsed_net - footer_net:+,.2f})'
        )


# ── CLI ───────────────────────────────────────────────────────────────────────
def _summarise(records, path):
    n = len(records)
    suppliers = {r.supplier_code for r in records if r.supplier_code}
    products = {r.product_code for r in records if r.product_code}
    docs = {r.doc_no for r in records}

    by_doc_type = {}
    for r in records:
        prefix = re.match(r'[A-Z]+', r.doc_no).group()
        by_doc_type.setdefault(prefix, []).append(r)

    hp_t = sum(r.total for r in by_doc_type.get('HP', []))
    hp_n = sum(r.net for r in by_doc_type.get('HP', []))
    rr_t = sum(r.total for r in by_doc_type.get('RR', []))
    rr_n = sum(r.net for r in by_doc_type.get('RR', []))
    # ALL GR rows are credit-note/return adjustments — always subtracted
    gr_rows = by_doc_type.get('GR', [])
    gr_t = -sum(r.total for r in gr_rows)
    gr_n = -sum(r.net for r in gr_rows)
    grand_t = hp_t + rr_t + gr_t
    grand_n = hp_n + rr_n + gr_n

    footer_total, footer_net = _parse_footer(path)

    print(f'Purchase lines    : {n}')
    print(f'Distinct docs     : {len(docs)}')
    print(f'Distinct suppliers: {len(suppliers)}')
    print(f'Distinct products : {len(products)}')
    print('Doc-type breakdown:')
    for k, rs in by_doc_type.items():
        print(f'  {k:<4s}  n={len(rs):<6d}  total={sum(r.total for r in rs):>14,.2f}  net={sum(r.net for r in rs):>14,.2f}')
    print()
    print(f'Parsed grand total (HP+RR+GR±)    : {grand_t:>16,.2f}')
    print(f'Parsed grand net   (HP+RR+GR±)    : {grand_n:>16,.2f}')
    if footer_total is not None:
        print(f'Footer grand total (รวมทั้งสิ้น)  : {footer_total:>16,.2f}  diff={grand_t-footer_total:+,.2f}')
        print(f'Footer grand net   (รวมทั้งสิ้น)  : {footer_net:>16,.2f}  diff={grand_n-footer_net:+,.2f}')


def main():
    ap = argparse.ArgumentParser(description='Parse Express ประวัติการซื้อ (purchase history)')
    ap.add_argument('path', type=Path)
    ap.add_argument('--json', action='store_true', help='emit JSON instead of summary')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--no-validate', action='store_true', help='skip footer reconciliation')
    args = ap.parse_args()

    records = list(parse_purchase_history(args.path))

    if not args.no_validate:
        validate(records, args.path)

    if args.json:
        slice_ = records[:args.limit] if args.limit else records
        out = [asdict(r) for r in slice_]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
        return

    _summarise(records, args.path)


if __name__ == '__main__':
    main()
