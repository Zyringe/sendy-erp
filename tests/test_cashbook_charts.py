"""Unit tests for cashbook chart data prep (_expense_topn) + render."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from blueprints.cashbook import _expense_topn


def _cats(n):
    # n categories, descending totals (10, 9, 8, ...), like _get_category_summary
    return [{'category': f'c{i}', 'total': float(10 - i)} for i in range(n)]


def test_expense_topn_under_threshold_returned_asis():
    cats = [{'category': 'a', 'total': 5.0}, {'category': 'b', 'total': 3.0}]
    out = _expense_topn(cats, n=7)
    assert out == cats
    assert all(c['category'] != 'อื่นๆ' for c in out)


def test_expense_topn_exactly_threshold_no_other():
    cats = _cats(7)
    out = _expense_topn(cats, n=7)
    assert len(out) == 7
    assert all(c['category'] != 'อื่นๆ' for c in out)


def test_expense_topn_folds_tail_into_other():
    cats = _cats(9)
    out = _expense_topn(cats, n=7)
    assert len(out) == 8
    assert out[7]['category'] == 'อื่นๆ'
    assert out[7]['total'] == cats[7]['total'] + cats[8]['total']


def test_expense_topn_preserves_grand_total():
    cats = _cats(9)
    out = _expense_topn(cats, n=7)
    assert sum(c['total'] for c in out) == sum(c['total'] for c in cats)


def test_expense_topn_empty():
    assert _expense_topn([], n=7) == []
