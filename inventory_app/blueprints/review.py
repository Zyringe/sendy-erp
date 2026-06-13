"""bp_review — ตรวจบิล v2 read-only feed.

URL prefix: /review

Routes:
  GET  /review        → suspicious-document feed (newest-first; last 6 months by
                        default, ?all=1 for full history)
  POST /review/scan   → re-scan the last RECENT_SCAN_DAYS days (scan_recent);
                        redirects back to feed. Windowed to stay under the
                        gunicorn worker timeout (full scan_all timed out on prod).
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
                           show_all=show_all, include_medium=include_medium,
                           recent_days=rr.RECENT_SCAN_DAYS)


@bp_review.route('/scan', methods=['POST'])
def scan():
    summary = rr.scan_recent()
    flagged = summary['docs_flagged']
    scanned = summary['docs_scanned']
    days = summary['days']
    flash(
        f'สแกนบิล {days} วันล่าสุดแล้ว {scanned} ใบ — พบ {flagged} ใบที่ต้องเช็ค',
        'success',
    )
    return redirect(url_for('review.index'))
