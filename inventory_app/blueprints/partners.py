"""Partners blueprint — customers, suppliers, customer map/geocoding, and
regions admin.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain a `partners.`
prefix.
"""
import os

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort, current_app)

import models
from database import get_connection

bp_partners = Blueprint('partners', __name__)


# ── Customers ─────────────────────────────────────────────────────────────────

@bp_partners.route('/customers')
def customer_list():
    search    = request.args.get('q', '').strip()
    region_id = request.args.get('region_id', '').strip()
    region    = request.args.get('region', '').strip()  # legacy bookmarks
    page      = request.args.get('page', 1, type=int) or 1
    per_page  = current_app.config['ITEMS_PER_PAGE']

    # Legacy ?region=<text>: warn the user when it doesn't resolve so we don't
    # silently show all customers and look like the filter is broken.
    if region and not region_id:
        from database import get_connection as _gc
        _conn = _gc()
        match = _conn.execute(
            "SELECT id FROM regions WHERE code = ? OR name_th = ? LIMIT 1",
            (region, region),
        ).fetchone()
        _conn.close()
        if not match:
            flash(f'ไม่พบเขต "{region}" — แสดงลูกค้าทั้งหมดแทน', 'warning')

    customers, total = models.get_customers(
        search=search or None,
        region=region or None,
        region_id=region_id or None,
        page=page, per_page=per_page,
    )
    pages   = (total + per_page - 1) // per_page
    regions = models.get_regions()
    return render_template('customers.html',
                           customers=customers, total=total,
                           page=page, pages=pages,
                           search=search, region=region, region_id=region_id,
                           regions=regions)


@bp_partners.route('/customer/<path:customer_name>')
def customer_summary(customer_name):
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    data = models.get_customer_summary(customer_name, date_from, date_to)
    unpaid_bills = models.get_customer_unpaid_bills(customer_name)
    unpaid_total = sum(b['total_net'] or 0 for b in unpaid_bills)

    master = models.get_customer_master(data['customer_code']) if data.get('customer_code') else None
    return render_template('customer_summary.html',
                           data=data,
                           unpaid_bills=unpaid_bills, unpaid_total=unpaid_total,
                           master=master,
                           salespersons=models.get_active_salespersons(),
                           regions=models.get_all_regions(),
                           orphan_codes=models.get_orphan_salesperson_codes())


@bp_partners.route('/customer/<customer_code>/reassign', methods=['POST'])
def customer_reassign(customer_code):
    salesperson = request.form.get('salesperson', '').strip()
    region_id   = request.form.get('region_id', '').strip()

    result = models.update_customer_assignment(customer_code, salesperson, region_id)
    if result['ok']:
        flash('บันทึก salesperson / region เรียบร้อย (master record)', 'success')
    else:
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    # Use the canonical name from the master record so the post-redirect
    # destination can never be steered by a hostile form value.
    master = models.get_customer_master(customer_code)
    if master:
        return redirect(url_for('partners.customer_summary', customer_name=master['name']))
    return redirect(url_for('partners.customer_list'))


@bp_partners.route('/customers/bulk-reassign', methods=['GET', 'POST'])
def customer_bulk_reassign():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        abort(403)

    if request.method == 'POST':
        codes       = request.form.getlist('customer_codes')
        salesperson = request.form.get('salesperson', '').strip()
        region_id   = request.form.get('region_id', '').strip()
        mode        = request.form.get('mode', 'salesperson')

        result = models.bulk_reassign_customers(codes, salesperson, region_id, mode=mode)
        if result['ok']:
            flash(f'อัปเดต {result["updated"]} ลูกค้าเรียบร้อย (master record)', 'success')
        else:
            flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')
        redirect_args = {
            'q':                  request.form.get('q', '') or None,
            'salesperson_filter': request.form.get('salesperson_filter', '') or None,
            'region_filter':      request.form.get('region_filter', '') or None,
            'orphan':             '1' if request.form.get('orphan_filter') == '1' else None,
        }
        return redirect(url_for('partners.customer_bulk_reassign',
                                **{k: v for k, v in redirect_args.items() if v}))

    search        = request.args.get('q', '').strip()
    salesperson_f = request.args.get('salesperson_filter', '').strip()
    region_f      = request.args.get('region_filter', '').strip()
    orphan_only   = request.args.get('orphan') == '1'
    page          = request.args.get('page', 1, type=int) or 1
    per_page      = 100

    region_id_int = int(region_f) if region_f.isdigit() else None
    customers, total = models.get_customers_master(
        search=search or None,
        salesperson=salesperson_f or None,
        region_id=region_id_int,
        orphan_only=orphan_only,
        page=page, per_page=per_page,
    )
    pages = (total + per_page - 1) // per_page

    return render_template(
        'customers_bulk_reassign.html',
        customers=customers, total=total, page=page, pages=pages,
        search=search, salesperson_filter=salesperson_f,
        region_filter=region_f, orphan_only=orphan_only,
        salespersons=models.get_active_salespersons(),
        regions=models.get_all_regions(),
        orphan_codes=models.get_orphan_salesperson_codes(),
    )


# ── Suppliers ─────────────────────────────────────────────────────────────────

@bp_partners.route('/suppliers')
def supplier_list():
    search   = request.args.get('q', '').strip()
    page     = int(request.args.get('page', 1))
    per_page = current_app.config['ITEMS_PER_PAGE']
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

def _parse_bsn_customers():
    import re
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', '..', 'data', 'source', 'bsn_customer_info.csv')
    with open(csv_path, encoding='cp874', errors='replace') as f:
        content = f.read()
    lines = [l.strip('"').replace('\xa0', ' ') for l in content.split('\n')]

    customers = []
    current_type = ''
    i = 0
    while i < len(lines):
        line = lines[i]
        type_match = re.match(r'\s+ประเภท\s*:\s*(.+)', line)
        if type_match:
            current_type = type_match.group(1).strip()
            i += 1; continue

        cust_match = re.match(r'  (\d{2}[ก-ฮA-Za-z]\d{2,3})\s+(.+?)\s{3,}(\S+)\s+(\S+)\s+\d+', line)
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
                if re.match(r'  \d{2}[ก-ฮA-Za-z]\d{2,3}\s', nl): break
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
        i += 1
    return customers


@bp_partners.route('/customers/map')
def customer_map():
    zone   = request.args.get('zone', '').strip()
    ctype  = request.args.get('type', '').strip()
    total, geocoded = models.get_geocode_progress()
    zones  = models.get_customer_zones()
    ctypes = models.get_customer_types()
    customers_json = models.get_customers_for_map(
        zone=zone or None, customer_type=ctype or None
    )
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


# ── Regions admin (fill in name_th + sort_order) ─────────────────────────────

@bp_partners.route('/regions', methods=['GET', 'POST'])
def regions_admin():
    if session.get('role') != 'admin':
        abort(403)

    if request.method == 'POST':
        try:
            region_id = int(request.form.get('region_id', '0'))
        except ValueError:
            abort(400)
        result = models.update_region(
            region_id,
            request.form.get('name_th', ''),
            request.form.get('sort_order', ''),
            request.form.get('note', ''),
        )
        if result['ok']:
            flash(f'อัปเดต region #{region_id} เรียบร้อย', 'success')
        else:
            flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')
        return redirect(url_for('partners.regions_admin'))

    return render_template('regions.html', regions=models.get_all_regions_with_counts())
