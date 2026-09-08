"""One definition of "which page of results did the URL ask for".

Thirteen routes each re-derived this, in three idioms, and the bare-int ones
turned a typo in the address bar into a 500:

    int(request.args.get('page', 1))            # ValueError on ?page=abc
    max(1, int(request.args.get('page', 1)))    # same, plus a lower clamp
    request.args.get('page', 1, type=int) or 1  # survives 'abc', not a
                                                # 25-digit number

The last idiom looks safe but isn't: Flask parses a huge integer happily and
it then overflows the SQLite OFFSET bind further down. `/ar` had already been
fixed in place for both cases; this module is that fix, generalised.

Rule, taken from that fix: **anything that is not a usable page number
resolves to page 1** — non-numeric, empty, zero, negative, or absurd. Pages
are a navigation affordance, not user input worth an error page.
"""
from flask import current_app

# Beyond this, (page - 1) * per_page stops fitting a SQLite INTEGER bind.
# The value is inherited from the /ar fix this module generalises.
MAX_PAGE = 1_000_000


def page_arg(args) -> int:
    """Return the ?page number in `args`, or 1 if it isn't a usable one."""
    try:
        page = int(args.get('page', 1))
    except (TypeError, ValueError):
        return 1
    return page if 1 <= page <= MAX_PAGE else 1


def paging(args, per_page=None) -> tuple[int, int]:
    """Return (page, per_page) for a listing route.

    `per_page` defaults to the app's ITEMS_PER_PAGE; pass a value for the
    routes that deliberately size their own page (cashbook 50, bulk-reassign 100).
    """
    if per_page is None:
        per_page = current_app.config['ITEMS_PER_PAGE']
    return page_arg(args), per_page
