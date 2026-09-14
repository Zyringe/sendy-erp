"""Partners blueprint — customers, suppliers, customer map/geocoding.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain a `partners.`
prefix.

#528: the region bulk-reassign page (`/customers/bulk-reassign`) and the
regions admin page (`/regions`) were deleted — เขตการขาย (customers.region_id)
is retired, replaced everywhere by ภาค (customer_geo.region_of, derived from
the address).
"""
import os
import re
from datetime import date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort)

import access_control
import call_card as cc
import cashflow
import customer_geo
import models
import payments_alloc
from database import get_connection
from filters import phone_entries
from paging import paging

bp_partners = Blueprint('partners', __name__)


def _with_phone_entries(customers):
    """Attach each customer's split phone entries for the map popup.

    Every other surface pipes `phone_entries` in the template, but the map
    popup is assembled in JavaScript from a JSON array — so the route runs the
    same filter and ships its output rather than letting the popup invent a
    second convention. Returns new dicts; the caller's rows are untouched.
    """
    return [dict(c, phone_entries=phone_entries(c.get('phone')))
            for c in customers]


# ── Customers ─────────────────────────────────────────────────────────────────

@bp_partners.route('/customers')
def customer_list():
    search           = request.args.get('q', '').strip()
    # #528: ภาค text filter (customer_geo.REGION_ORDER), retiring เขตการขาย.
    # A legacy ?region_id= or an old FK-style ?region=<code|name_th> bookmark
    # is simply ignored — get_customers() no-ops on a value that isn't a real
    # REGION_ORDER string.
    region           = request.args.get('region', '').strip()
    include_billless = request.args.get('include_billless') == '1'
    page, per_page   = paging(request.args)

    customers, total = models.get_customers(
        search=search or None,
        region=region or None,
        page=page, per_page=per_page,
        include_billless=include_billless,
    )
    pages = (total + per_page - 1) // per_page
    return render_template('customers.html',
                           customers=customers, total=total,
                           page=page, pages=pages,
                           search=search, region=region,
                           include_billless=include_billless,
                           regions=customer_geo.REGION_ORDER)


@bp_partners.route('/customer/code/<customer_code>')
def customer_detail(customer_code):
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    # Cost block (#493 slice 3) is gated at the DATA layer: this page is open
    # to staff, so the model is asked for cost only for the same roles the
    # template's `is_manager` flag covers (access_control.inject_auth) —
    # shareholder deliberately excluded. Every other role's data holds none.
    include_cost = session.get('role') in ('admin', 'manager')
    data = models.get_customer_summary_by_code(customer_code, date_from, date_to,
                                               include_cost=include_cost)

    # A code with no master row AND no sales at all is not a customer — 404 rather
    # than render a page titled after whatever was typed into the URL. A code that
    # has sales but no master row IS a real state here (the page flags it with the
    # "ยังไม่มีใน master" badge), so only the both-missing case is a 404.
    # `exists` is deliberately date-INDEPENDENT: keying it off the filtered rows
    # would 404 a real customer whose date filter happens to exclude every bill.
    if not data['exists']:
        abort(404)

    unpaid_bills, unpaid_snapshot_date = models.get_customer_unpaid_bills_by_code(customer_code)
    unpaid_total = sum(b['total_net'] or 0 for b in unpaid_bills)

    # The bills REMOVED from that total. Since #465 `unpaid_bills` is the
    # chaseable population, so a forgiven / already-paid / pre-2024 bill simply
    # vanished from this page — the person on the phone who remembers it had no
    # way to see what happened to it (ADR 0012, #468). Same code key as the list
    # above: `get_customer_unpaid_bills_by_code` TRIMs for this reason.
    excluded_docs, _excluded_snapshot = cashflow.bsn_ar_excluded_docs_by_code(customer_code)
    # The บิลค้างชำระ card below is the Express AR snapshot, so this page owes
    # the same freshness warning the other AR surfaces carry.
    aging = cashflow.ar_aging()

    master = models.get_customer_master(customer_code)

    # How fast this customer pays (#499): receipt history over SETTLED bills,
    # shown beside the credit term. Not AR — neither outstanding nor chaseable
    # (ADR 0012) — and deliberately blind to the date filter above: it is
    # always the latest 20 settled bills.
    pay_speed = payments_alloc.payment_speed(customer_code)

    # Call-log notes (#497) — the SAME rows the call card lists (same table,
    # same key: `customer_call_log.customer_code`, and on THIS route
    # `customer_code` is always the literal code, never a bill name to
    # resolve). The add-note box only renders for a role whose POST
    # whitelist actually contains call.call_note — never a hand-copied role
    # tuple that could drift from the real gate.
    conn = get_connection()
    call_log = cc.get_log(conn, customer_code)
    conn.close()
    can_add_note = access_control.role_can_post(session.get('role', ''), 'call.call_note')

    # ซื้อล่าสุด + days quiet (#493) — last_purchase_date is already the
    # evidence-filtered date (never a credit note or a freebie-only line);
    # the day count is a rendering concern, computed here, not in the model.
    days_quiet = None
    last_purchase_date = data['summary'].get('last_purchase_date')
    if last_purchase_date:
        try:
            days_quiet = (date.today() - date.fromisoformat(last_purchase_date)).days
        except ValueError:
            # A malformed date_iso must not 500 the whole page — every live
            # row is a clean YYYY-MM-DD today, but this is a rendering
            # concern, not a data-integrity guarantee to bet the page on.
            days_quiet = None

    return render_template('customer_summary.html',
                           data=data,
                           days_quiet=days_quiet,
                           audit_history=models.get_customer_audit_history(customer_code),
                           unpaid_bills=unpaid_bills, unpaid_total=unpaid_total,
                           unpaid_snapshot_date=unpaid_snapshot_date,
                           excluded_docs=excluded_docs,
                           aging=aging,
                           master=master,
                           pay_speed=pay_speed,
                           call_log=call_log,
                           can_add_note=can_add_note,
                           elapsed_th=cc.elapsed_th,
                           salespersons=models.get_active_salespersons(),
                           orphan_codes=models.get_orphan_salesperson_codes())


@bp_partners.route('/customer/<path:customer_name>')
def customer_summary(customer_name):
    """Shim: `/customer/<name>` is keyed on the BILL name, which is ambiguous
    (BUG 2 — one bill name can span >1 physical company, e.g. ทรัพย์ทวี).
    Resolve to the code-keyed page instead of rendering here directly.
    """
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    codes = models.resolve_customer_codes(customer_name)

    if len(codes) == 1:
        redirect_args = {'customer_code': codes[0]}
        if date_from:
            redirect_args['date_from'] = date_from
        if date_to:
            redirect_args['date_to'] = date_to
        return redirect(url_for('partners.customer_detail', **redirect_args))

    if len(codes) > 1:
        flash(f'ชื่อ "{customer_name}" มี {len(codes)} รหัสลูกค้า เลือกรายที่ต้องการ', 'warning')
    else:
        flash(f'ไม่พบรหัสลูกค้าสำหรับ "{customer_name}"', 'warning')
    return redirect(url_for('partners.customer_list', q=customer_name))


@bp_partners.route('/customer/<customer_code>/reassign', methods=['POST'])
def customer_reassign(customer_code):
    """Save handler for the customer-edit modal (customer_summary.html):
    group 1 (salesperson) + group 2 contact fields in one POST.
    See models.update_customer_edit for the field-group + stamping rules.
    """
    salesperson = request.form.get('salesperson', '').strip()

    # MISSING is not CLEAR. This URL is unchanged from the pre-modal card, which
    # POSTed salesperson (+ region_id, before #528 retired it) and nothing
    # else — a page rendered before this deploy and submitted after it is a
    # real caller, not a hypothetical one. Reading its absent contact keys as
    # blanks wipes phone/contact/address, stamps the row as curated, and
    # pushes NULLs into the pending review row (reproduced against a live
    # copy on 2026-08-01 for 11ม06 — all three columns went to NULL). So
    # branch on what the request actually carried.
    present = [k for k in models.CUSTOMER_CONTACT_FIELDS if k in request.form]

    if not present:
        # Legacy assignment-only form. Same behaviour it always had.
        result = models.update_customer_assignment(customer_code, salesperson)
    elif len(present) == len(models.CUSTOMER_CONTACT_FIELDS):
        contact = {k: request.form.get(k, '') for k in models.CUSTOMER_CONTACT_FIELDS}
        result = models.update_customer_edit(
            customer_code, salesperson, contact, session.get('username'))
    else:
        # Neither shape — refuse rather than guess which half to trust.
        missing = [k for k in models.CUSTOMER_CONTACT_FIELDS if k not in request.form]
        result = {'ok': False,
                  'error': f'ฟอร์มส่งข้อมูลไม่ครบ (ขาด: {", ".join(missing)}) — '
                           'ลองรีเฟรชหน้าแล้วบันทึกใหม่'}

    if result['ok']:
        flash('บันทึกข้อมูลลูกค้าเรียบร้อย', 'success')
    else:
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    # Redirect by CODE — customer_code is already the trusted route param
    # (not a hostile form value), and the code-keyed page can never diverge
    # from it the way the old master-NAME redirect could (BUG 1).
    return redirect(url_for('partners.customer_detail', customer_code=customer_code))


# ── Suppliers ─────────────────────────────────────────────────────────────────

@bp_partners.route('/suppliers')
def supplier_list():
    search   = request.args.get('q', '').strip()
    page, per_page = paging(request.args)
    suppliers, total = models.get_suppliers(
        search=search or None, page=page, per_page=per_page
    )
    pages = (total + per_page - 1) // per_page
    return render_template('suppliers.html',
                           suppliers=suppliers, total=total,
                           page=page, pages=pages, search=search)


@bp_partners.route('/supplier/<path:supplier_name>')
def supplier_summary(supplier_name):
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    data = models.get_supplier_summary(supplier_name, date_from, date_to)
    return render_template('supplier_summary.html', data=data)


# ── Customer Map ──────────────────────────────────────────────────────────────

# Customer rows live in the CODE COLUMN — exactly two leading spaces — and end
# with the salesperson / zone / discount trio. Put ruled 2026-08-15 that EVERY
# customer code present in Express is legitimate and must be imported with all
# its information, so the code token is `\S+` rather than a digit pattern.
#
# Verified against data/source/bsn_customer_info.csv + ARMAS.DBF (2026-08-15):
# this matches 2,663 rows, every one of them a real ARMAS customer code, with
# zero codes matched that ARMAS does not have and zero duplicate code lines.
# The old `\d{2}[ก-ฮA-Za-z]\d{2,3}` shape matched only 1,477 — it silently
# dropped 1,186 real customers (three- and four-digit prefixes like `032ท03` /
# `1101ค01`, two-Thai-letter `23ทธ01`, and the 280 Laos `L…` codes plus
# `Bหน้าร้าน` / `S…` / `Z…`).
_CUSTOMER_RE = re.compile(r'  (\S+)\s{2,}(.+?)\s{3,}(\S+)\s+(\S+)\s+(\d+)\s*$')

# A CANDIDATE is any line in that same column with a token and more text after
# it. Anything candidate-shaped that the strict row regex refuses is a format
# shift and must fail loud rather than be skipped.
#
# Anchoring on the column is load-bearing: measured on the same file, an
# unanchored "lstrip() starts with two digits" rule also matches 434
# address-continuation lines (postcodes like `10240`, phone numbers) sitting
# ~17 columns to the right — first at line 51 — so every real import would
# refuse for the wrong reason.
_CUSTOMER_CANDIDATE_RE = re.compile(r'  (\S+)\s{2,}\S')

# The only structural line that shares the code column: the per-page column
# header (529 occurrences, one per page break). Everything else at two-space
# indent in the real export is a customer row or a `ประเภท :` section header.
_COLUMN_HEADER_RE = re.compile(r'\s*รหัส\s.*ส่วนลด')

# A customer NAME never contains a 3+-space run — that gap is a column
# separator. Seeing one inside the captured name means the row has more
# trailing columns than the regex counts back from EOL.
_COLUMN_RUN_RE = re.compile(r'\s{3,}')


def _parse_bsn_customers(csv_path=None):
    """Parse the BSN customer-master export into a list of customer dicts.

    Raises ValueError — never returns a short list — when the file is not the
    customer report, when it yields zero customers, or when any line in the
    customer-code column fails the strict row regex. A partial parse used to be
    reported to the operator as "นำเข้าสำเร็จ".

    SCOPE (Put, 2026-08-15): every customer code present in Express is
    legitimate and must be imported with all of its information. The strict
    row regex therefore accepts ANY code token in the code column, not a digit
    pattern. Verified against data/source/bsn_customer_info.csv joined to
    ARMAS.DBF on 2026-08-15: 2,663 rows parse, all 2,663 codes exist in ARMAS,
    no code is matched that ARMAS lacks, and no code line is matched twice.

    Before that ruling the regex required `\d{2}[ก-ฮA-Za-z]\d{2,3}` and matched
    only 1,477 rows, silently dropping 1,186 real customers: 899 three-digit
    prefixes (`032ท03`), `042บ021`, `1101ค01`, `23ทธ01`, the 280 Laos `L…`
    codes, `Bหน้าร้าน`, and the `S…` / `Z…` shop codes. Every one of them
    already existed in `customers` from another path, which is exactly why
    nobody noticed.
    """
    if csv_path is None:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'data', 'source', 'bsn_customer_info.csv')
    with open(csv_path, encoding='cp874', errors='replace') as f:
        content = f.read()
    lines = [l.strip('"').replace('\xa0', ' ') for l in content.split('\n')]

    # "Is this even the right report" gate, before the shape accounting below
    # can produce a confusing error. Deliberately POSITIONAL: a bare
    # "contains รายงาน anywhere" only passed on the real file by way of its
    # ">>>> จบรายงาน <<<<" trailer, so any Express report mentioning ลูกค้า
    # would clear it — and a sales-by-customer report also carries codes in a
    # left column, which is worse than a clean refusal (the row regex would
    # match wrongly-positioned fields and write garbage name/zone).
    if 'รายละเอียดลูกค้า' not in '\n'.join(lines[:10]):
        raise ValueError(
            'ไฟล์ผิดประเภท: ไม่ใช่รายงานรายละเอียดลูกค้าของ BSN (ไม่พบหัวรายงาน)'
        )

    customers = []
    rejected = []          # (line_number, excerpt) for candidate-shaped misses
    current_type = ''
    i = 0
    while i < len(lines):
        line = lines[i]
        type_match = re.match(r'\s+ประเภท\s*:\s*(.+)', line)
        if type_match:
            current_type = type_match.group(1).strip()
            i += 1; continue
        if _COLUMN_HEADER_RE.match(line):
            # Repeats once per page break; structural, never a customer row.
            i += 1; continue

        cust_match = _CUSTOMER_RE.match(line)
        if cust_match and _COLUMN_RUN_RE.search(cust_match.group(2)):
            # The regex counts three tokens back from EOL, so if the
            # ประเภทราคา column is ever populated the lazy name group
            # swallows the real salesperson and every field shifts one
            # right — writing a zone code into customers.salesperson.
            # A 3+-space run inside the NAME is the tell. Refuse loudly
            # instead: it lands in `rejected` below.
            cust_match = None
        if cust_match:
            code = cust_match.group(1)
            name = cust_match.group(2).strip()
            salesperson = cust_match.group(3)
            zone = cust_match.group(4)
            customer = {
                'code': code, 'name': name, 'salesperson': salesperson,
                'zone': zone, 'customer_type': current_type,
                'address': '', 'phone': '', 'tax_id': '',
                'credit_days': 0, 'contact': '',
            }
            addr_parts = []
            j = i + 1
            while j < len(lines) and j < i + 10:
                nl = lines[j]
                # Break on the CANDIDATE shape, not just the strict one: a
                # format-shifted customer row must end this block and return to
                # the main loop (where it is recorded as rejected) instead of
                # being silently swallowed as address text.
                if _CUSTOMER_CANDIDATE_RE.match(nl): break
                if re.match(r'\(BSN\)', nl.strip()): j += 5; break
                am = re.match(r'\s+ที่อยู่\s*:\s*(.*?)\s+ผู้ติดต่อ\s*:\s*(.*)', nl)
                if am:
                    a = am.group(1).strip()
                    if a: addr_parts.append(a)
                    customer['contact'] = am.group(2).strip()
                elif re.match(r'\s{17,}[^\s]', nl):
                    a = re.sub(r'\s+เลขที่.*', '', re.sub(r'\s+เครดิต.*', '', nl)).strip()
                    if a and not a.startswith('(BSN)'): addr_parts.append(a)
                cm = re.search(r'เครดิต\s*:\s*(\d+)', nl)
                if cm: customer['credit_days'] = int(cm.group(1))
                pm = re.match(r'\s+โทร\.\s*:\s*(.*?)\s+เงื่อนไข', nl)
                if pm: customer['phone'] = pm.group(1).strip()
                tm = re.match(r'\s+Tax ID\s*:\s*(\d+)', nl)
                if tm: customer['tax_id'] = tm.group(1)
                j += 1
            customer['address'] = ' '.join(addr_parts)
            customers.append(customer)
            i = j; continue
        if _CUSTOMER_CANDIDATE_RE.match(line):
            # Sits in the customer-code column but the strict regex refused it:
            # the format shifted. Record it — do NOT skip on.
            rejected.append((i + 1, line.strip()))
        i += 1

    if rejected:
        line_no, excerpt = rejected[0]
        raise ValueError(
            f'อ่านข้อมูลลูกค้าไม่ครบ: บรรทัด {line_no}: {excerpt[:120]}'
            f' (พบทั้งหมด {len(rejected)} บรรทัด)'
        )
    if not customers:
        raise ValueError('ไม่พบรายการลูกค้าในรายงาน — ไฟล์ผิดประเภทหรือรูปแบบเปลี่ยน')
    return customers


@bp_partners.route('/customers/map')
def customer_map():
    zone   = request.args.get('zone', '').strip()
    ctype  = request.args.get('type', '').strip()
    total, geocoded = models.get_geocode_progress()
    zones  = models.get_customer_zones()
    ctypes = models.get_customer_types()
    customers_json = _with_phone_entries(models.get_customers_for_map(
        zone=zone or None, customer_type=ctype or None
    ))
    return render_template('customer_map.html',
                           customers_json=customers_json,
                           zones=zones, ctypes=ctypes,
                           sel_zone=zone, sel_type=ctype,
                           total=total, geocoded=geocoded)


@bp_partners.route('/customers/import-bsn', methods=['POST'])
def customer_import_bsn():
    if session.get('role') != 'admin':
        abort(403)
    try:
        customers = _parse_bsn_customers()
    except FileNotFoundError:
        flash('ไม่พบไฟล์ bsn_customer_info.csv ใน data/source/ กรุณาวางไฟล์ก่อนนำเข้า', 'danger')
        return redirect(url_for('partners.customer_map'))
    except ValueError as e:
        # Zero/partial parse — refuse the whole import rather than write a
        # silent subset and flash "นำเข้าสำเร็จ" over it.
        flash(str(e), 'danger')
        return redirect(url_for('partners.customer_map'))
    inserted, updated, protected = models.import_customers_from_bsn(customers)
    flash(
        f'นำเข้าสำเร็จ: เพิ่มใหม่ {inserted} รายการ, อัปเดต {updated} รายการ'
        + (f', ป้องกัน {protected} รายการ (ข้อมูลติดต่อถูกทำความสะอาดแล้ว)' if protected else ''),
        'success'
    )
    return redirect(url_for('partners.customer_map'))


@bp_partners.route('/customers/geocode/<code>', methods=['POST'])
def customer_geocode(code):
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    import urllib.request, urllib.parse, json as _json
    conn = get_connection()
    row = conn.execute("SELECT address, name FROM customers WHERE code=?", (code,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    address = row['address'] or row['name']
    query = urllib.parse.urlencode({'q': address + ' ประเทศไทย', 'format': 'json',
                                    'limit': 1, 'accept-language': 'th'})
    url = f'https://nominatim.openstreetmap.org/search?{query}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'SendaiBoonswat-ERP/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            models.save_customer_geocode(code, lat, lng)
            return jsonify({'ok': True, 'lat': lat, 'lng': lng, 'display': data[0].get('display_name','')})
        return jsonify({'ok': False, 'reason': 'no result'})
    except Exception as e:
        return jsonify({'ok': False, 'reason': str(e)}), 500


