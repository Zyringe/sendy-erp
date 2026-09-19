"""`thaiperiod` — renders a cashbook cost's `belongs_to_period` in พ.ศ.

The column stores Gregorian (`YYYY` or `YYYY-MM`, mig 187's CHECK allows
nothing else) because every other date in this DB is Gregorian. Put reads the
statement in พ.ศ. though, and the costs on that line are literally named for
their พ.ศ. year ("โบนัสปี 68"), so showing a bare `2025` next to that
description invites exactly the misreading it should prevent (Put, 2026-09-19).

Deliberately NOT done by changing `thaidate`: that filter renders a Gregorian
year on every page in the app, and this is one column on one card.
"""
import pytest

from filters import thaiperiod


@pytest.mark.parametrize('value,expected', [
    ('2025', 'ปี 2568'),        # the live case — the FY2568 bonuses
    ('2024', 'ปี 2567'),
    ('2025-12', 'ธ.ค. 2568'),
    ('2025-01', 'ม.ค. 2568'),
    ('2026-02', 'ก.พ. 2569'),
])
def test_renders_the_period_in_buddhist_era(value, expected):
    assert thaiperiod(value) == expected


@pytest.mark.parametrize('value', [None, ''])
def test_empty_renders_empty(value):
    assert thaiperiod(value) == ''


@pytest.mark.parametrize('value', ['2025-13', 'ไม่แน่ใจ', '25', '2025-1-1'])
def test_an_unparseable_value_comes_back_raw_rather_than_raising(value):
    """mig 187's CHECK makes these un-storable, so this is the second line of
    defence: a display filter must never be the thing that 500s the page."""
    assert thaiperiod(value) == value


def test_registered_on_the_app_so_the_template_can_call_it():
    """A filter that exists but is never registered raises
    TemplateAssertionError at render time, which no unit test above can see."""
    from app import app
    assert 'thaiperiod' in app.jinja_env.filters
    assert app.jinja_env.filters['thaiperiod']('2025') == 'ปี 2568'
