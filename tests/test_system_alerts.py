"""Durable operational alerts (mig 149) + the caller-owned alerting contract.

WHY THIS EXISTS
    PR2 makes a WACC cost-identity failure raise instead of silently writing a
    wrong cost basis, and the caller shows a flash message. But Put is the only
    person who acts on digital problems, and the flash reaches whoever ran the
    import — who will not reliably relay it. Flash-only means the failure is
    effectively SILENT to the one person who can fix it.

THE CONTRACT UNDER TEST
    recalculate_product_wacc raises and persists NOTHING — not even an alert.
    The owning caller rolls back, CLOSES, then writes the alert on a fresh
    connection, then re-raises. Writing on the failed connection would roll the
    alert back with the failure; opening a second connection while the first
    still holds the write lock risks "database is locked".
"""


def _wacc_error(product_id=1, reference_no='RR001', code='C1', seq=1,
                operation='purchase_import'):
    import models
    return models.WaccIdentityError(
        'linked purchase line no longer exists',
        product_id=product_id, reference_no=reference_no,
        source_bsn_code=code, source_line_seq=seq, operation=operation)


def _open(conn):
    return conn.execute(
        "SELECT id, kind, message, dedupe_key, context_json, resolved_at,"
        " resolved_by FROM system_alerts WHERE resolved_at IS NULL"
        " ORDER BY id").fetchall()


def test_alert_survives_the_rollback_of_the_failed_work(empty_db_conn):
    """The whole point: the alert must OUTLIVE the transaction that failed.

    Simulates the caller contract — the failed connection is rolled back, then
    the alert is written on a fresh one. If the alert were written on the
    rolled-back connection it would vanish, which is the silence this exists
    to prevent.
    """
    import models

    empty_db_conn.execute(
        "INSERT INTO products (id, product_name, unit_type) VALUES (9001, 'X', 'ตัว')")
    empty_db_conn.rollback()          # the failed work is discarded

    models.record_wacc_identity_alert(_wacc_error(), operation='purchase_import',
                                      extra={'filename': 'week01.csv'})

    rows = _open(empty_db_conn)
    assert len(rows) == 1, rows
    assert rows[0]['kind'] == models.KIND_WACC_IDENTITY
    assert 'RR001' in rows[0]['message']
    # The rolled-back product must NOT be there — proving the alert really did
    # survive a rollback rather than riding along inside it.
    assert empty_db_conn.execute(
        "SELECT COUNT(*) FROM products WHERE id=9001").fetchone()[0] == 0


def test_recalculate_itself_never_writes_an_alert(empty_db_conn):
    """WACC raises; persisting is the caller's job. If wacc.py wrote the alert
    on its own connection it would be rolled back with the failed work."""
    import models

    pid = empty_db_conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('NoAlert','ตัว')"
    ).lastrowid
    empty_db_conn.execute(
        "INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    empty_db_conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change,"
        " unit_mode, reference_no, note, created_at, source_bsn_code,"
        " source_line_seq) VALUES (?,'IN',5,'unit','RRX','BSN ซื้อ',"
        " '2026-04-24 00:00:00','GHOST',1)", (pid,))
    empty_db_conn.commit()

    try:
        models.recalculate_product_wacc(pid, empty_db_conn)
        raise AssertionError('expected WaccIdentityError')
    except models.WaccIdentityError:
        pass

    assert _open(empty_db_conn) == [], \
        "recalculate_product_wacc must not persist an alert itself"


def test_one_unresolved_alert_per_incident(empty_db_conn):
    """Repeating the same incident must not spam — even from a NEW batch.

    dedupe_key is incident identity (product + reference + source line +
    operation). Diagnostics like filename/batch id live in context_json and are
    deliberately excluded, so a retry in a different batch is still the same
    incident.
    """
    import models

    models.record_wacc_identity_alert(_wacc_error(), extra={'batch_id': 1,
                                                            'filename': 'a.csv'})
    models.record_wacc_identity_alert(_wacc_error(), extra={'batch_id': 2,
                                                            'filename': 'b.csv'})
    assert len(_open(empty_db_conn)) == 1, "a retry must not raise a second alert"

    # A genuinely different incident DOES get its own alert.
    models.record_wacc_identity_alert(_wacc_error(reference_no='RR999'))
    assert len(_open(empty_db_conn)) == 2


def test_resolve_stamps_who_and_allows_a_later_recurrence(empty_db_conn):
    """Acknowledging records WHO cleared it, and a recurrence is news again."""
    import models

    models.record_wacc_identity_alert(_wacc_error())
    alert_id = _open(empty_db_conn)[0]['id']

    models.resolve_system_alert(alert_id, 'putty')
    assert _open(empty_db_conn) == []
    row = empty_db_conn.execute(
        "SELECT resolved_at, resolved_by FROM system_alerts WHERE id=?",
        (alert_id,)).fetchone()
    assert row['resolved_by'] == 'putty'
    assert row['resolved_at'] is not None

    # The partial unique index only covers UNRESOLVED rows, so the same
    # incident happening again raises a fresh alert rather than being swallowed.
    models.record_wacc_identity_alert(_wacc_error())
    assert len(_open(empty_db_conn)) == 1


def test_alerting_failure_never_masks_the_original_error(empty_db_conn):
    """If the alert insert itself fails, the money-path error must still win.

    An alert-table problem must never replace an actionable WACC failure, and
    must never turn a failure into a success.
    """
    import models

    empty_db_conn.execute("DROP TABLE system_alerts")
    empty_db_conn.commit()

    # Best-effort: returns None, does not raise, so the caller's `raise` of the
    # original WaccIdentityError is what reaches the user.
    assert models.record_wacc_identity_alert(_wacc_error()) is None


def test_badge_counts_unresolved_alerts(empty_db_conn):
    import models

    assert models.count_open_system_alerts() == 0
    models.record_wacc_identity_alert(_wacc_error())
    assert models.count_open_system_alerts() == 1
    models.resolve_system_alert(_open(empty_db_conn)[0]['id'], 'putty')
    assert models.count_open_system_alerts() == 0


# ── Route + template ─────────────────────────────────────────────────────────
# curl/test-client give HTML, not visual appearance — these pin behaviour
# (does the section appear, is resolving gated), not looks.

def _client(monkeypatch, role='admin'):
    import os
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')
    from app import app
    app.config['WTF_CSRF_ENABLED'] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = role
        s['username'] = 'tester'
        s['user_id'] = 99
    return c


def test_alerts_page_shows_open_system_alerts(empty_db_conn, monkeypatch):
    import models

    c = _client(monkeypatch, 'admin')
    html = c.get('/alerts').get_data(as_text=True)
    assert 'ปัญหาระบบที่ยังไม่ได้แก้' not in html, "no section when there are none"

    models.record_wacc_identity_alert(_wacc_error())
    html = c.get('/alerts').get_data(as_text=True)
    assert 'ปัญหาระบบที่ยังไม่ได้แก้' in html
    assert 'RR001' in html
    # The acknowledge control is present for an admin.
    assert 'รับทราบแล้ว' in html


def test_staff_cannot_resolve_a_system_alert(empty_db_conn, monkeypatch):
    """The people who fail to relay a failure must not be able to clear it
    before Put sees it — otherwise this whole table is pointless."""
    import models

    models.record_wacc_identity_alert(_wacc_error())
    alert_id = _open(empty_db_conn)[0]['id']

    staff = _client(monkeypatch, 'staff')
    staff.post(f'/alerts/{alert_id}/resolve')
    assert len(_open(empty_db_conn)) == 1, "staff must not be able to resolve"

    admin = _client(monkeypatch, 'admin')
    admin.post(f'/alerts/{alert_id}/resolve')
    assert _open(empty_db_conn) == [], "admin should be able to resolve"


def test_ratio_replay_failure_leaves_exactly_one_alert(empty_db_conn):
    """The caller Codex found missing from the chain — now an INTEGRATION test.

    The earlier version monkeypatched recalculate_product_wacc to raise, which
    bypassed the owning wrapper entirely and so could not see that ratio replay
    recorded a SECOND alert on top of the wrapper's. `operation` is part of the
    dedupe key, so the partial unique index did NOT collapse them.

    Note the failure has to be a duplicate business key rather than a dangling
    link: update_unit_conversion_ratio deletes and REBUILDS the product's BSN
    ledger from source, so a hand-broken link is simply repaired by the replay.
    """
    import models
    from models import bsn_sync

    pid = _seed_product_min(empty_db_conn, 'Ratio Replay Integration')
    empty_db_conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 12)",
        (pid,))
    # Two source lines sharing (doc_no, bsn_code, line_seq). mig 148's partial
    # unique index makes this impossible going forward; drop it to model a DB
    # that predates the index, which is exactly what pre-flight must refuse.
    empty_db_conn.execute("DROP INDEX IF EXISTS idx_purchase_txn_doc_code_line")
    for _ in range(2):
        empty_db_conn.execute(
            "INSERT INTO purchase_transactions (date_iso, doc_no, product_id, bsn_code,"
            " line_seq, product_name_raw, supplier, supplier_code, qty, unit,"
            " unit_price, vat_type, discount, total, net, synced_to_stock)"
            " VALUES ('2026-04-24','RRDUP',?,'DUP',1,'r','s','s1',2,'โหล',1.0,0,'',100,100,0)",
            (pid,))
    empty_db_conn.commit()

    try:
        bsn_sync.update_unit_conversion_ratio(pid, 'โหล', 6)
        raise AssertionError('expected WaccIdentityError to propagate')
    except models.WaccIdentityError:
        pass

    rows = _open(empty_db_conn)
    assert len(rows) == 1, f"exactly one alert per incident, got {len(rows)}"
    assert 'ratio_replay' in (rows[0]['context_json'] or ''), rows[0]['context_json']


def _seed_product_min(conn, name):
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES (?, 'ตัว')",
        (name,)).lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
                 (pid,))
    return pid


def _broken_product(conn, name):
    """A product whose ledger row links to a purchase line that does not exist."""
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES (?, 'ตัว')",
        (name,)).lastrowid
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode,"
        " reference_no, note, created_at, source_bsn_code, source_line_seq)"
        " VALUES (?,'IN',5,'unit','RRGHOST','BSN ซื้อ','2026-04-24 00:00:00','GHOST',1)",
        (pid,))
    conn.commit()
    return pid


def test_owning_recalculate_alerts_and_releases(empty_db_conn):
    """recalculate_product_wacc(pid) — no connection passed — owns the one that
    actually raises, so it must roll back, close and alert.

    Closing the CALLER's connection does not cover this: the leaking connection
    is the one opened inside recalculate_product_wacc itself.
    """
    import models

    pid = _broken_product(empty_db_conn, 'Owning Recalc')
    try:
        models.recalculate_product_wacc(pid)      # no conn → owns one
        raise AssertionError('expected WaccIdentityError')
    except models.WaccIdentityError:
        pass

    rows = _open(empty_db_conn)
    assert len(rows) == 1, "an owning recalculate must leave a durable alert"
    # The connection it opened was released, so this write is not blocked.
    empty_db_conn.execute("UPDATE products SET product_name=? WHERE id=?",
                          ('Owning Recalc 2', pid))
    empty_db_conn.commit()


def test_cost_history_lazy_failure_alerts(empty_db_conn):
    """/products/<id>/cost-history lazily recalculates. Without an alert the
    failure is a 500 seen only by whoever clicked."""
    import models

    pid = _broken_product(empty_db_conn, 'Lazy CostHistory')
    try:
        models.get_cost_history(pid)
        raise AssertionError('expected WaccIdentityError')
    except models.WaccIdentityError:
        pass

    rows = _open(empty_db_conn)
    assert len(rows) == 1, "the lazy cost-history read must leave a durable alert"
    assert 'wacc_lazy_read' in (rows[0]['context_json'] or '')
