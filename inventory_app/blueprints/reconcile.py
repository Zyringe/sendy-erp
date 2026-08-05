"""bp_reconcile — reconcile-scan review page.

URL prefix: /reconcile

Detection itself runs inside import_router.commit_express_dbf() (see
models/reconcile.py::scan_reconcile), triggered by the normal Express DBF
upload — this blueprint only exposes the human review/action surface:

  GET  /reconcile          → open flags (+ ?all=1 for resolved history),
                              manager+ (view; mirrors _arf_require_manager's
                              admin/manager/shareholder set — shareholder
                              reads everything per its role description)
  POST /reconcile/<id>/apply    → confirm-delete for a 'deleted' flag
  POST /reconcile/<id>/dismiss  → ไม่ใช่-เก็บไว้ (requires a note)
  POST /reconcile/<id>/reopen   → เปิดพิจารณาใหม่ (clears suppression)

Both POSTs are manager-whitelist only (access_control._MANAGER_POST_OK) —
staff can upload (detection runs) but cannot apply/dismiss/reopen.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session

import models

bp_reconcile = Blueprint('reconcile', __name__, url_prefix='/reconcile')


def _require_manager():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))
    return None


@bp_reconcile.route('')
def index():
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    show_all = request.args.get('all') == '1'
    open_flags = models.list_open_reconcile_flags()
    resolved_flags = models.list_resolved_reconcile_flags() if show_all else []
    return render_template('reconcile/index.html', open_flags=open_flags,
                           resolved_flags=resolved_flags, show_all=show_all,
                           platform_refusal_msg=models.PLATFORM_REFUSAL_MSG)


@bp_reconcile.route('/<int:flag_id>/apply', methods=['POST'])
def apply(flag_id):
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    result = models.apply_reconcile_flag(flag_id, session.get('display_name') or session.get('username') or 'manager')
    if result.get('ok'):
        flash('ลบเอกสารตาม Express แล้ว' if not result.get('noop') else 'เอกสารนี้ apply ไปแล้ว (ไม่มีอะไรเปลี่ยนแปลง)',
              'success')
    else:
        flash(f"apply ไม่สำเร็จ: {result.get('error')}", 'danger')
    return redirect(url_for('reconcile.index'))


@bp_reconcile.route('/<int:flag_id>/dismiss', methods=['POST'])
def dismiss(flag_id):
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    note = (request.form.get('note') or '').strip()
    result = models.dismiss_reconcile_flag(
        flag_id, session.get('display_name') or session.get('username') or 'manager', note)
    if result.get('ok'):
        flash('เก็บไว้ (dismiss) แล้ว', 'success')
    else:
        flash(f"dismiss ไม่สำเร็จ: {result.get('error')}", 'danger')
    return redirect(url_for('reconcile.index'))


@bp_reconcile.route('/<int:flag_id>/reopen', methods=['POST'])
def reopen(flag_id):
    redirect_ = _require_manager()
    if redirect_:
        return redirect_
    result = models.reopen_reconcile_flag(
        flag_id, session.get('display_name') or session.get('username') or 'manager')
    if result.get('ok'):
        flash('เปิดพิจารณาใหม่แล้ว — สแกนครั้งถัดไปจะตรวจซ้ำ', 'success')
    else:
        flash(f"เปิดพิจารณาใหม่ไม่สำเร็จ: {result.get('error')}", 'danger')
    return redirect(url_for('reconcile.index', all=1))
