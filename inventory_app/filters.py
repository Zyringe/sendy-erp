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
    app.template_filter('from_json')(from_json)
