"""bp_review — ตรวจบิล review UI routes.

URL prefix: /review

Routes:
  GET  /review                           → redirect to latest batch
  GET  /review/batches                   → batch list
  GET  /review/batch/<batch_id>          → main review page
  POST /review/batch/<batch_id>/rescan   → re-run scan_batch
  POST /review/doc/<doc_review_id>       → mark ok/wrong
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session)

import review_rules as rr

bp_review = Blueprint('review', __name__, url_prefix='/review')


@bp_review.route('')
def index():
    """Redirect to latest sales batch; fall back to batch list when none."""
    batches = rr.get_sales_batches(limit=1)
    if batches:
        return redirect(url_for('review.batch_view', batch_id=batches[0]['id']))
    return redirect(url_for('review.batch_list'))


@bp_review.route('/batches')
def batch_list():
    batches = rr.get_sales_batches(limit=20)
    return render_template('review/index.html', batches=batches)


@bp_review.route('/batch/<int:batch_id>')
def batch_view(batch_id):
    batches = rr.get_sales_batches(limit=20)
    review = rr.get_batch_review(batch_id)

    all_docs = [d for docs in review.values() for d in docs]
    auto_passed = sum(1 for d in all_docs if d['review_status'] == 'auto_passed')
    pending     = sum(1 for d in all_docs if d['review_status'] == 'pending')
    ok          = sum(1 for d in all_docs if d['review_status'] == 'ok')
    wrong       = sum(1 for d in all_docs if d['review_status'] == 'wrong')

    return render_template(
        'review/batch.html',
        batch_id=batch_id,
        batches=batches,
        review=review,
        auto_passed=auto_passed,
        pending=pending,
        reviewed=ok + wrong,
        ok=ok,
        wrong=wrong,
    )


@bp_review.route('/batch/<int:batch_id>/rescan', methods=['POST'])
def rescan(batch_id):
    summary = rr.scan_batch(batch_id)
    flash(
        f'สแกนใหม่แล้ว: พบ {summary["docs_flagged"]} บิลต้องตรวจ '
        f'/ {summary["docs_clean"]} บิลผ่านอัตโนมัติ',
        'success',
    )
    return redirect(url_for('review.batch_view', batch_id=batch_id))


@bp_review.route('/doc/<int:doc_review_id>', methods=['POST'])
def mark_doc(doc_review_id):
    status   = request.form.get('status', '').strip()
    note     = request.form.get('note', '').strip()
    batch_id = request.form.get('batch_id', type=int)

    if status not in ('ok', 'wrong'):
        flash('สถานะไม่ถูกต้อง', 'danger')
        return redirect(request.referrer or url_for('review.index'))

    if status == 'wrong' and not note:
        flash('กรุณาระบุหมายเหตุเมื่อทำเครื่องหมายว่าผิด', 'danger')
        dest = (
            url_for('review.batch_view', batch_id=batch_id)
            + f'#doc-{doc_review_id}'
            if batch_id else None
        )
        return redirect(dest or request.referrer or url_for('review.index'))

    reviewer = session.get('username', 'unknown')
    try:
        rr.mark_doc(doc_review_id, status, note or None, reviewer)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(request.referrer or url_for('review.index'))

    flash('บันทึกผล: ' + ('ถูกต้อง' if status == 'ok' else 'ผิด'), 'success')
    dest = (
        url_for('review.batch_view', batch_id=batch_id)
        + f'#doc-{doc_review_id}'
        if batch_id else None
    )
    return redirect(dest or request.referrer or url_for('review.index'))
