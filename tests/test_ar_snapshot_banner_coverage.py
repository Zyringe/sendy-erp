"""Every surface that shows an Express-AR-snapshot balance must disclose how OLD
that snapshot is.

`_ar_snapshot_banner.html` (Finding 1, 2026-08-15) is the one copy of that
warning, and its own header says it is "included by EVERY page that serves the
authoritative collection balance". It was not: `/ar` and `/cashflow` and the
dunning detail page had it, while four surfaces that serve the same number from
the same table did not — including the call card, which is the page a person is
looking at *while the phone is ringing*.

The number those pages show is only safe to chase with while the snapshot is
fresh; `cashflow.AR_SNAPSHOT_STALE_AFTER_DAYS` is 1 day, because Express exports
it roughly daily. A page that shows the balance and hides its age invites someone
to quote a figure that moved yesterday.

⚠ The sweep below is a marker sweep, so it produces false positives on purpose:
`m/sales_trip.html` and `payment_customers.html` say `total_outstanding` while
reading a DIFFERENT source. Those are exempt WITH A REASON rather than excluded
from the pattern, so the next person can see the judgement instead of guessing.
"""
import os
import sqlite3
from datetime import date

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')
TEMPLATES = os.path.join(APP, 'templates')

INCLUDE = "{% include '_ar_snapshot_banner.html' %}"

# A template mentioning any of these is showing a balance that MIGHT come from
# the Express AR snapshot, so it has to be judged one way or the other.
MARKERS = ('aging.', 'unpaid_total', 'total_outstanding', 'unpaid_snapshot_date')

# Judged NOT to need the banner, each with the reason it does not.
EXEMPT = {
    '_ar_snapshot_banner.html':
        'it IS the banner — it matches its own markers because it renders '
        'aging.as_of and aging.age_days.',
    'ar_followup.html':
        'DEAD — zero references in the whole repo (the dunning list was folded '
        'into the /ar tabs). Left on disk deliberately; deleting it is its own '
        'call, and adding the banner to a template nothing renders is worse '
        'than leaving it out.',
    'm/sales_trip.html':
        'its ยอดค้างรวม is summed from sales_transactions via vat_math.cash_sql, '
        'NOT from express_ar_outstanding — a snapshot-age warning would be '
        'about the wrong table.',
    'payment_customers.html':
        'the payment-status ledger, not the AR snapshot. The page already '
        'carries its own note that its totals differ from the Express AR page.',
}


def _templates_matching_markers():
    hits = {}
    for root, _dirs, files in os.walk(TEMPLATES):
        for fn in files:
            if not fn.endswith('.html'):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, TEMPLATES)
            body = open(path, encoding='utf-8').read()
            if any(m in body for m in MARKERS):
                hits[rel] = body
    return hits


def test_every_ar_snapshot_surface_shows_the_freshness_banner():
    """The sweep. A new surface that renders the balance fails here until
    someone either includes the banner or writes down why it does not apply."""
    hits = _templates_matching_markers()

    # Controls: the sweep ran, and it can see a template we KNOW needs the banner.
    assert hits, 'the marker sweep matched nothing — it did not run'
    assert 'express_ar_customer.html' in hits, (
        'the sweep cannot see a known AR surface; the markers are wrong')

    missing = [rel for rel, body in hits.items()
               if rel not in EXEMPT and INCLUDE not in body]
    assert missing == [], (
        'these render an Express AR snapshot balance with no freshness banner: '
        '%s' % missing)


def test_no_exemption_is_stale_or_unexplained():
    """An allowlist rots silently: a file renamed away leaves an entry that
    excuses nothing, and a file that stopped looking like an AR surface leaves
    one that hides a judgement nobody re-made."""
    hits = _templates_matching_markers()
    for rel, reason in EXEMPT.items():
        assert os.path.exists(os.path.join(TEMPLATES, rel)), (
            'exemption names a template that no longer exists: %s' % rel)
        assert reason and len(reason) > 20, 'exemption without a real reason: %s' % rel
        assert rel in hits, (
            '%s no longer looks like an AR surface — drop the exemption rather '
            'than leaving a hole open' % rel)


# ── the four surfaces, rendered ──────────────────────────────────────────────

CODE = 'ZZBANNER1'
NAME = 'ทดสอบ แบนเนอร์ snapshot'
BANNER = '>นำเข้า AR snapshot ใหม่<'


def _seed(db_path, snapshot_date):
    """Force ONE customer with one outstanding bill, and force the WHOLE BSN
    snapshot to `snapshot_date` so `ar_aging()` reads the freshness we mean.

    ⚠ `ar_aging` keys on MAX(snapshot_date_iso) over the entity, so staleness
    cannot be set per-customer — the date has to be forced for the entity, and
    the fixture must not inherit whatever the cloned dev DB happened to hold.
    Deliberately NO sales_transactions rows: the call card's product path calls
    `price_lookup.epochs_for_pairs`, which on local dev resolves to the CLI
    wrapper in scripts/ and raises (issue #476). A customer with no purchase
    history never reaches it, so this file tests the banner and not that bug.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        batch = conn.execute(
            "SELECT id FROM express_import_log ORDER BY id DESC LIMIT 1").fetchone()
        assert batch, 'no express_import_log row to hang the FK on'

        conn.execute("UPDATE express_ar_outstanding SET snapshot_date_iso = ? "
                     "WHERE entity = 'BSN'", (snapshot_date,))
        conn.execute("DELETE FROM express_ar_outstanding WHERE customer_code = ?", (CODE,))
        conn.execute("""
            INSERT INTO express_ar_outstanding
                (batch_id, snapshot_date_iso, customer_code, customer_name,
                 doc_date_iso, doc_no, is_anomalous, bill_amount, paid_amount,
                 outstanding_amount, entity)
            VALUES (?, ?, ?, ?, '2025-04-04', 'ZZBAN-IV', 0, 5000.0, 0, 5000.0, 'BSN')
        """, (batch['id'], snapshot_date, CODE, NAME))

        conn.execute("DELETE FROM customers WHERE code = ? OR name = ?", (CODE, NAME))
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, NAME))
        conn.commit()
    finally:
        conn.close()


def _client():
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')
    from app import app
    app.config['WTF_CSRF_ENABLED'] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'admin'; s['username'] = 'test'; s['user_id'] = 1
    return c


STALE = '2020-01-01'          # far older than AR_SNAPSHOT_STALE_AFTER_DAYS
FRESH = date.today().isoformat()

SURFACES = [
    ('express AR drill-down', '/express/ar/customer/' + CODE, 'ZZBAN-IV'),
    ('customer summary',      '/customer/code/' + CODE,       'ZZBAN-IV'),
    ('mobile customer',       '/m/customer/' + NAME,          'ZZBAN-IV'),
    ('call card',             '/call/' + CODE,                'ค้างชำระทั้งหมด'),
]


@pytest.mark.parametrize('label,url,marker', SURFACES,
                         ids=[s[0] for s in SURFACES])
def test_surface_warns_when_the_snapshot_is_stale(tmp_db, label, url, marker):
    _seed(tmp_db, STALE)
    resp = _client().get(url)
    html = resp.get_data(as_text=True)

    # Control FIRST: the page rendered this customer's balance at all, so the
    # banner assertion has a subject. Without it a 302 or a 500 reads as a pass
    # on the "no banner" test below and as a plain failure here, which is the
    # wrong diagnosis.
    assert resp.status_code == 200, '%s did not render: %s' % (label, resp.status_code)
    assert marker in html, '%s did not show the balance' % label

    assert BANNER in html, '%s shows a %s snapshot with no freshness warning' % (
        label, STALE)


@pytest.mark.parametrize('label,url,marker', SURFACES,
                         ids=[s[0] for s in SURFACES])
def test_surface_stays_quiet_when_the_snapshot_is_fresh(tmp_db, label, url, marker):
    """The control for the test above: a partial included unconditionally, or a
    banner whose staleness test is inverted, passes that one and fails here."""
    _seed(tmp_db, FRESH)
    resp = _client().get(url)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200, '%s did not render: %s' % (label, resp.status_code)
    assert marker in html, '%s did not show the balance' % label

    assert BANNER not in html, '%s cried stale on a same-day snapshot' % label
