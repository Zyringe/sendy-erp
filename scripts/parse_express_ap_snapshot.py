"""Parse Express เจ้าหนี้คงค้างแบบละเอียด (AP outstanding snapshot).

The report groups outstanding invoices by supplier type → supplier name/code,
with per-invoice rows and per-supplier subtotals. We flatten into one row per
outstanding document, carrying supplier_type / supplier_name / supplier_code.

Structure:
    ประเภทผู้จำหน่าย : <type>
        <supplier_name> /<supplier_code>
            DD/MM/YY  RR...  <supplier_invoice_no>  bill  paid  outstanding
        รวมเจ้าหนี้ <name> /<code>  N ใบ  <outstanding>
    รวมตามประเภท <type>   N ราย   N ใบ  <outstanding>
    รวมทั้งสิ้น  ผู้จำหน่าย N ราย  N ใบ  <outstanding>

CLI:
    python scripts/parse_express_ap_snapshot.py PATH_TO_CSV
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


# ── Date conversion (พ.ศ. → ค.ศ.) ─────────────────────────────────────────────
_DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{2})$')


def thai_date_to_iso(s):
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    dd, mm, yy = m.groups()
    year = 1957 + int(yy)
    return f'{year:04d}-{int(mm):02d}-{int(dd):02d}'


# Report "as of" date from the header line, e.g. "ณ วันที่ 29 พ.ค. 2569 ... วันที่ : 29/05/69".
# This is the TRUE snapshot date (คงค้าง ณ วันที่ X), independent of the latest
# document date in the body — use it so weekly re-imports supersede correctly.
_ASOF_RE = re.compile(r'วันที่\s*:\s*(\d{2}/\d{2}/\d{2})')


def report_asof_date(path):
    """Return the report's as-of date (ISO) parsed from its header, or None."""
    with open(path, encoding='cp874', errors='replace') as f:
        for _ in range(15):  # header sits in the first few lines
            line = f.readline()
            if not line:
                break
            m = _ASOF_RE.search(line)
            if m:
                return thai_date_to_iso(m.group(1))
    return None


# ── Patterns ─────────────────────────────────────────────────────────────────

# Supplier-type group header: "  ประเภทผู้จำหน่าย : ผู้จำหน่ายประจำ"
_TYPE_HDR_RE = re.compile(r'^\s*ประเภทผู้จำหน่าย\s*:\s*(?P<type>.*?)\s*$')

# Supplier header: "    เซ็นไดเทรดดิ้ง จำกัด /เซ็น"
# Same pattern as AR customer header: name /code at end.
_SUPPLIER_HDR_RE = re.compile(r'^\s+(?P<name>(?:\S(?:.*?\S)?)?)\s*/(?P<code>\S+)\s*$')

# Detail row: date  RR...  supplier_invoice_no  bill  paid  outstanding
# supplier_invoice_no may contain alphanumeric+dashes (e.g. IV6900002, IV26041213)
_DETAIL_RE = re.compile(
    r'^\s+(?P<date_thai>\d{2}/\d{2}/\d{2})\s+'
    r'(?P<doc_no>RR\S+)\s+'
    r'(?P<supplier_invoice_no>\S+)\s+'
    r'(?P<bill>-?[\d,]+\.\d{2})\s+'
    r'(?P<paid>-?[\d,]+\.\d{2})\s+'
    r'(?P<outstanding>-?[\d,]+\.\d{2})\s*$'
)

# Subtotal per supplier: รวมเจ้าหนี้ <name> /<code>  N ใบ  <amount>
_SUB_RE = re.compile(
    r'รวมเจ้าหนี้\s*(.*?)\s*/(\S+)\s+(\d+)\s*ใบ\s+(-?[\d,]+\.\d{2})'
)

# Grand total: รวมทั้งสิ้น  ผู้จำหน่าย  N ราย  N ใบ  <amount>
_GT_RE = re.compile(
    r'รวมทั้งสิ้น.*?ผู้จำหน่าย\s+(\d+)\s*ราย\s+(\d+)\s*ใบ\s+(-?[\d,]+\.\d{2})'
)

_NOISE = (
    'เจ้าหนี้คงค้าง', 'ณ วันที่', 'รหัสผู้จำหน่าย', 'ประเภทผู้จำหน่ายจาก',
    'วันที่', 'เอกสาร#', 'เลขที่บิล', 'ยอดในบิล', 'ตัดยอดโดย',
    'รวมตามประเภท', 'หน้า', 'จบรายงาน', 'บริษัท บุญสวัสดิ์',
    'บจก.', '---', '===',
)


# ── Data class ───────────────────────────────────────────────────────────────
@dataclass
class APOutstanding:
    supplier_type: str
    supplier_name: str
    supplier_code: str
    doc_date_iso: str
    doc_no: str
    supplier_invoice_no: str
    bill_amount: float
    paid_amount: float
    outstanding_amount: float


# ── Helpers ──────────────────────────────────────────────────────────────────
def _num(s):
    return float(s.replace(',', '').strip())


def _strip_quotes(line):
    line = line.rstrip('\r\n')
    if len(line) >= 2 and line[0] == '"' and line[-1] == '"':
        line = line[1:-1]
    return line.replace('""', '"')


# ── Parser ───────────────────────────────────────────────────────────────────
def parse_ap_snapshot(path):
    """Yield APOutstanding records from the Express AP snapshot file."""
    current_type = ''
    current_name = ''
    current_code = ''
    grand_total = None
    subtotals = []

    with open(path, 'r', encoding='cp874') as f:
        lines = f.readlines()

    records = []
    for raw in lines:
        line = _strip_quotes(raw).replace('\xa0', ' ')
        content = line.strip()
        if not content:
            continue

        # Grand total line
        g = _GT_RE.search(content)
        if g:
            grand_total = (int(g.group(1)), int(g.group(2)), _num(g.group(3)))
            continue

        # Per-supplier subtotal
        s = _SUB_RE.search(content)
        if s:
            subtotals.append((s.group(2), int(s.group(3)), _num(s.group(4))))
            continue

        # Supplier-type header
        m = _TYPE_HDR_RE.match(line)
        if m:
            current_type = m.group('type').strip()
            continue

        # Skip noise tokens
        if any(n in content for n in _NOISE):
            continue

        # Detail row (must check before supplier header — detail rows are more specific)
        m = _DETAIL_RE.match(line)
        if m:
            records.append(APOutstanding(
                supplier_type=current_type,
                supplier_name=current_name,
                supplier_code=current_code,
                doc_date_iso=thai_date_to_iso(m.group('date_thai')),
                doc_no=m.group('doc_no'),
                supplier_invoice_no=m.group('supplier_invoice_no'),
                bill_amount=_num(m.group('bill')),
                paid_amount=_num(m.group('paid')),
                outstanding_amount=_num(m.group('outstanding')),
            ))
            continue

        # Supplier header: indented line with /code suffix
        m = _SUPPLIER_HDR_RE.match(line)
        if m and '/' in line:
            name = m.group('name').strip()
            code = m.group('code').strip()
            # Must not be a subtotal line (already caught above) or noise
            if code and ' ' not in code and len(code) <= 12 and not code.endswith('.'):
                current_name, current_code = name, code
                continue

        # Anything else is silently skipped (separator lines, headers, etc.)

    return records, grand_total, subtotals


def _validate(records, grand_total, subtotals):
    """Assert parser output matches footer totals. Raises AssertionError on mismatch."""
    if grand_total is None:
        raise AssertionError('Grand total line not found in file')

    n_suppliers_gt, n_docs_gt, total_gt = grand_total
    codes = {r.supplier_code for r in records}
    total = round(sum(r.outstanding_amount for r in records), 2)

    assert len(records) == n_docs_gt, (
        f'doc count mismatch: parsed {len(records)} != footer {n_docs_gt}')
    assert abs(total - total_gt) < 0.01, (
        f'outstanding total mismatch: parsed {total} != footer {total_gt}')
    assert len(codes) == n_suppliers_gt, (
        f'supplier count mismatch: parsed {len(codes)} != footer {n_suppliers_gt}')

    # Per-supplier subtotal validation
    from collections import defaultdict
    by_code_n = defaultdict(int)
    by_code_out = defaultdict(float)
    for r in records:
        by_code_n[r.supplier_code] += 1
        by_code_out[r.supplier_code] += r.outstanding_amount

    mismatches = []
    for code, n, out in subtotals:
        if by_code_n.get(code, 0) != n or abs(round(by_code_out.get(code, 0) - out, 2)) > 0.01:
            mismatches.append((code, n, by_code_n.get(code, 0), out, round(by_code_out.get(code, 0), 2)))
    if mismatches:
        raise AssertionError(f'Per-supplier mismatches: {mismatches}')


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', type=Path)
    args = ap.parse_args()

    records, grand_total, subtotals = parse_ap_snapshot(args.path)
    _validate(records, grand_total, subtotals)

    n_suppliers = len({r.supplier_code for r in records})
    total = round(sum(r.outstanding_amount for r in records), 2)
    print(f'Documents          : {len(records)}')
    print(f'Distinct suppliers : {n_suppliers}')
    print(f'Total outstanding  : {total:,.2f}')
    print(f'Footer match       : OK')

    by_type = {}
    for r in records:
        by_type.setdefault(r.supplier_type, []).append(r)
    for t, rs in by_type.items():
        amt = round(sum(r.outstanding_amount for r in rs), 2)
        print(f'  {t or "(none)":<28s}  n={len(rs):<3d}  amt={amt:>14,.2f}')


if __name__ == '__main__':
    main()
