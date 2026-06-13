"""bp_review — ตรวจบิล v2 read-only feed.

URL prefix: /review

Routes:
  GET  /review        → suspicious-document feed (newest-first; last 6 months by
                        default, ?all=1 for full history)
  POST /review/scan   → full re-scan (scan_all); redirects back to feed
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash

import review_rules as rr

bp_review = Blueprint('review', __name__, url_prefix='/review')


@bp_review.route('')
def index():
    show_all = request.args.get('all') == '1'
    include_medium = request.args.get('lvl') == 'all'
    since = None if show_all else rr.default_since()
    feed = rr.get_review_feed(since_date=since, include_medium=include_medium)
    return render_template('review/index.html', feed=feed,
                           show_all=show_all, include_medium=include_medium)


@bp_review.route('/scan', methods=['POST'])
def scan():
    summary = rr.scan_all()
    flagged = summary['docs_flagged']
    scanned = summary['docs_scanned']
    flash(
        f'สแกนแล้ว {scanned} ใบ — พบ {flagged} ใบที่ต้องเช็ค',
        'success',
    )
    return redirect(url_for('review.index'))
