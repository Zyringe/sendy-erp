"""Jinja template filters.

Extracted verbatim from app.py (behavior-preserving split).
"""
import json


def fmt_price(v):
    if v is None:
        return '-'
    return f'{v:,.2f}'


def fmt_qty(v):
    if v is None:
        return '-'
    return f'{v:,}'


_TH_MONTHS = ['', 'ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.',
              'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']


def thaidate(v):
    """'YYYY-MM-DD' (or a datetime string) -> 'D <Thai-month-abbr> YYYY'.
    Returns the raw string if it can't be parsed (never raises)."""
    if not v:
        return ''
    s = str(v)[:10]
    try:
        y, m, d = s.split('-')
        return f'{int(d)} {_TH_MONTHS[int(m)]} {y}'
    except (ValueError, IndexError):
        return s


def from_json(v):
    """Parse a JSON string into a Python value for in-template iteration.

    Returns None for empty input or invalid JSON, so templates can use
    `{% if … %}` guards naturally.
    """
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def register_filters(app):
    app.template_filter('fmt_price')(fmt_price)
    app.template_filter('fmt_qty')(fmt_qty)
    app.template_filter('thaidate')(thaidate)
    app.template_filter('from_json')(from_json)
