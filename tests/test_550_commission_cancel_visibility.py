"""#550 — the "ยกเลิกการจ่าย" (cancel payout) form renders ONLY for a role
that may actually POST `commission.commission_delete_payout`.

Two surfaces render that form:

  1. `templates/commission_payouts.html` — the row's `<td class="td-actions">`
     on /commission/payouts.
  2. `templates/commission_drilldown.html` — the same form in the payout
     history table on /commission/sp/<code>.

Before this change both rendered for every role that can open the page, so
manager and shareholder saw a trash button that POSTed straight into
`ไม่มีสิทธิ์ดำเนินการนี้` — the dead end #542 made worse by telling those
roles to "ยกเลิกที่หน้าคอมมิชชั่น". Put's call 2026-09-17: HIDE the control,
do not grant the permission.

Visibility is derived from the same rule that gates the POST
(`access_control.role_can_post`, pinned independently by
`test_542_commission_cancel_wording.py::test_role_can_post_commission_delete_payout`),
so a future whitelist change moves button and gate together.

⚠ Test shape (rules/verification-discipline.md):
  * Every assertion is scoped to the ONE `<tr>` that holds the fixture payout.
    A page-wide `'commission/payout' in html` would false-pass on the string
    inside a `<script>` or an `onsubmit` confirm text.
  * "manager sees zero forms" is ALSO what an unreachable fixture looks like,
    so `_payout_tr` asserts the row itself rendered (exactly one `<tr>` with
    the seeded amount) in the SAME test, and the empty-state marker is
    asserted ABSENT — a control only the intended branch can emit.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re

import pytest

# Reuse #542's fixture machinery verbatim (the reviewer of #548 confirmed it
# supports a manager-GET test as-is): `conn` seeds the salesperson and clears
# any inherited commission-linked rows from the cloned dev DB, and
# `_seed_commission_row` creates a REAL payout through
# `commission.record_payout` — the same call /commission/payout makes.
# `conn` is imported as a fixture on purpose; keep the noqa.
from tests.test_542_commission_cancel_wording import (  # noqa: F401
    SP_CODE, SP_NAME, conn, _client_as, _seed_commission_row,
)

AMOUNT = '999.00'          # _seed_commission_row's amount_paid, as rendered
YEAR_MONTH = '2026-07'
PAYOUTS_URL = f'/commission/payouts?month={YEAR_MONTH}&sp={SP_CODE}'
DRILLDOWN_URL = f'/commission/sp/{SP_CODE}?month={YEAR_MONTH}'

# The <form> element itself, never the bare URL: the same path also appears
# in the confirm() text and could appear in page JS.
DELETE_FORM_RE = re.compile(r'<form[^>]*action="/commission/payout/\d+/delete"')

PAYOUTS_EMPTY_STATE = 'ไม่มีประวัติการจ่าย'
DRILLDOWN_EMPTY_STATE = 'ยังไม่มีประวัติการจ่าย'


def _payout_tr(html):
    """The one `<tr>` holding the seeded payout.

    This IS the control: a page that never resolved the payout (wrong month,
    fixture not committed, route bailed early) has no such row, so the
    assertion fails loudly instead of reading as "the button was hidden".
    """
    rows = re.findall(r'<tr\b.*?</tr>', html, re.DOTALL)
    hits = [r for r in rows if AMOUNT in r]
    assert len(hits) == 1, (
        f"expected exactly one <tr> containing {AMOUNT!r} (the seeded payout), "
        f"found {len(hits)} — the fixture did not reach the page"
    )
    return hits[0]


def _get(role, tmp_db, url):
    resp = _client_as(role, tmp_db).get(url)
    assert resp.status_code == 200, f"{role} GET {url} → {resp.status_code}"
    return resp.get_data(as_text=True)


# ── /commission/payouts ─────────────────────────────────────────────────────

def test_payouts_admin_sees_the_cancel_form(conn, tmp_db):
    _seed_commission_row(conn)
    html = _get('admin', tmp_db, PAYOUTS_URL)
    assert PAYOUTS_EMPTY_STATE not in html
    row = _payout_tr(html)
    assert len(DELETE_FORM_RE.findall(row)) == 1
    assert 'bi-trash' in row


@pytest.mark.parametrize('role', ['manager', 'shareholder'])
def test_payouts_nonadmin_sees_no_cancel_form_but_still_sees_the_payout(conn, tmp_db, role):
    _seed_commission_row(conn)
    html = _get(role, tmp_db, PAYOUTS_URL)
    # Control first: the page resolved the payout and is NOT the empty state.
    assert PAYOUTS_EMPTY_STATE not in html
    row = _payout_tr(html)
    assert SP_CODE in row, "the row must still show whose payout it is"
    assert YEAR_MONTH in row, "the row must still show the payout's month"
    # …and only THEN that the control is withheld.
    assert DELETE_FORM_RE.findall(row) == []
    assert 'bi-trash' not in row


def test_payouts_empty_actions_cell_keeps_the_column_count(conn, tmp_db):
    """The <td> renders empty rather than disappearing, so <thead>, <tbody>
    and the empty-state colspan stay aligned for a role with no button."""
    _seed_commission_row(conn)
    admin_row = _payout_tr(_get('admin', tmp_db, PAYOUTS_URL))
    manager_row = _payout_tr(_get('manager', tmp_db, PAYOUTS_URL))
    assert 'td-actions' in manager_row
    assert admin_row.count('<td') == manager_row.count('<td')


# ── /commission/sp/<code> (drilldown) ───────────────────────────────────────

def test_drilldown_admin_sees_the_cancel_form(conn, tmp_db):
    _seed_commission_row(conn)
    html = _get('admin', tmp_db, DRILLDOWN_URL)
    assert DRILLDOWN_EMPTY_STATE not in html
    row = _payout_tr(html)
    assert len(DELETE_FORM_RE.findall(row)) == 1
    assert 'bi-trash' in row


@pytest.mark.parametrize('role', ['manager', 'shareholder'])
def test_drilldown_nonadmin_sees_no_cancel_form_but_still_sees_the_payout(conn, tmp_db, role):
    _seed_commission_row(conn)
    html = _get(role, tmp_db, DRILLDOWN_URL)
    assert DRILLDOWN_EMPTY_STATE not in html
    row = _payout_tr(html)
    assert '2026-07-05' in row, "the row must still show the paid date"
    assert DELETE_FORM_RE.findall(row) == []
    assert 'bi-trash' not in row


def test_drilldown_empty_actions_cell_keeps_the_column_count(conn, tmp_db):
    _seed_commission_row(conn)
    admin_row = _payout_tr(_get('admin', tmp_db, DRILLDOWN_URL))
    manager_row = _payout_tr(_get('manager', tmp_db, DRILLDOWN_URL))
    assert admin_row.count('<td') == manager_row.count('<td')


# ── the role matrix this ticket does NOT cover ──────────────────────────────

def test_staff_cannot_open_the_commission_pages_at_all(conn, tmp_db):
    """Documents why staff is absent from the parametrize lists above: the
    whole commission module is GET-gated away from staff (access_control's
    `commission.` prefix rule), so there is no page for a button to be on."""
    client = _client_as('staff', tmp_db)
    for url in (PAYOUTS_URL, DRILLDOWN_URL):
        assert client.get(url).status_code == 302, f"staff GET {url} must redirect"
