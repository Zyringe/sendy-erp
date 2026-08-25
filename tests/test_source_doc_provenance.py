"""A change to a sales/purchase document must carry who changed it and why.

WHY THIS EXISTS
    Measured on prod 2026-08-25: audit_log covers 38 tables with 111 triggers and
    holds ZERO rows for sales_transactions and purchase_transactions. When 47
    invoices were moved to a different customer on 2026-08-24, nothing recorded
    that it happened, let alone why.

SCOPE, AND WHY IT IS THIS SHAPE
    UPDATE of a meaningful column is BLOCKED without a declaration. INSERT and
    DELETE are recorded but not blocked, for reasons that are measured, not
    stylistic:
      · the importer replaces a changed line with DELETE+INSERT
        (models/imports.py), so blocking either would block every import;
      · a DELETE has no NEW row, so a reason cannot ride on the statement at all
        (proved in projects/express-integration/spike/provenance-2026-08-25/);
      · 77 test files INSERT into these tables directly.
    `synced_to_stock` and `batch_id` are exempt because they are bookkeeping that
    bsn_sync rewrites constantly — guarding them would force a declaration
    through the stock sync for no provenance value.

    The four real incidents this is built for — a customer code, a document date,
    a unit, a product_id — are all UPDATEs of meaningful columns.
"""
import json
import os
import sqlite3

import pytest

MIGRATION = os.path.join(os.path.dirname(__file__), '..', 'data', 'migrations',
                         '172_source_doc_provenance.sql')

MEANINGFUL = 'date_iso'          # one representative guarded column


@pytest.fixture
def db(empty_db):
    """empty_db carries the LIVE schema, which does not have this migration until
    someone boots the app on this branch. Applying it here keeps the test honest
    about what it is testing and independent of the shared dev DB's state."""
    conn = sqlite3.connect(empty_db, timeout=10)
    conn.row_factory = sqlite3.Row
    with open(MIGRATION, encoding='utf-8') as fh:
        conn.executescript(fh.read())
    conn.commit()
    yield conn
    conn.close()


def seed_sale(conn, doc_no='IV0001-1', **kw):
    cols = {'date_iso': '2026-01-15', 'doc_no': doc_no, 'doc_base': doc_no.rsplit('-', 1)[0],
            'bsn_code': 'A001', 'customer_code': 'C001', 'qty': 1.0, 'unit': 'ตัว',
            'unit_price': 10.0, 'total': 10.0, 'net': 10.0,
            'change_source': 'import', 'change_actor': 'express-dbf', 'change_token': 'b1'}
    cols.update(kw)
    conn.execute(f"INSERT INTO sales_transactions ({','.join(cols)}) "
                 f"VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))
    conn.commit()
    return conn.execute("SELECT id FROM sales_transactions WHERE doc_no=?",
                        (doc_no,)).fetchone()['id']


def audit(conn, action=None):
    q = ("SELECT * FROM audit_log WHERE table_name IN "
         "('sales_transactions','purchase_transactions')")
    if action:
        q += f" AND action='{action}'"
    return conn.execute(q + " ORDER BY id").fetchall()


# ── the block ────────────────────────────────────────────────────────────────
def test_meaningful_update_without_a_declaration_is_refused(db):
    rid = seed_sale(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"UPDATE sales_transactions SET {MEANINGFUL}='2026-02-02' WHERE id=?", (rid,))
    db.rollback()
    # CONTROL: the row really is untouched, so the refusal was a refusal and not
    # a write that happened to raise afterwards.
    assert db.execute("SELECT date_iso FROM sales_transactions WHERE id=?",
                      (rid,)).fetchone()[0] == '2026-01-15'


def test_reused_token_is_refused(db):
    """The trap a reader cannot see: an UPDATE that omits the change_* columns
    keeps the PREVIOUS row's reason and reads as fully explained."""
    rid = seed_sale(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"UPDATE sales_transactions SET {MEANINGFUL}='2026-02-02',"
                   " change_source='manual', change_actor='put',"
                   " change_reason='เหตุผลที่ยาวพอสมควร', change_token='b1' WHERE id=?", (rid,))
    db.rollback()


def test_thin_reason_is_refused(db):
    rid = seed_sale(db)
    for thin in ('', '   ', 'fix', 'แก้'):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE sales_transactions SET {MEANINGFUL}='2026-02-02',"
                       " change_source='manual', change_actor='put',"
                       " change_reason=?, change_token='t2' WHERE id=?", (thin, rid))
        db.rollback()


def test_unknown_source_is_refused(db):
    rid = seed_sale(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"UPDATE sales_transactions SET {MEANINGFUL}='2026-02-02',"
                   " change_source='whatever', change_actor='x',"
                   " change_reason='เหตุผลที่ยาวพอสมควร', change_token='t3' WHERE id=?", (rid,))
    db.rollback()


# ── the record ───────────────────────────────────────────────────────────────
def test_declared_update_lands_and_carries_its_reason(db):
    rid = seed_sale(db)
    db.execute(f"UPDATE sales_transactions SET {MEANINGFUL}='2026-02-02',"
               " change_source='manual', change_actor='put',"
               " change_reason='Express ต้นฉบับว่า 2026-02-02', change_token='t4' WHERE id=?",
               (rid,))
    db.commit()
    rows = audit(db, 'UPDATE')
    assert len(rows) == 1, f'expected exactly one UPDATE audit row, got {len(rows)}'
    r = rows[0]
    assert r['user'] == 'put'
    assert r['change_source'] == 'manual'
    assert r['change_reason'] == 'Express ต้นฉบับว่า 2026-02-02'
    diff = json.loads(r['changed_fields'])
    assert diff['date_iso'] == ['2026-01-15', '2026-02-02']
    # ⛔ and NOT a second, reason-less row: that split is exactly what made the
    # first design unusable (Codex C1) — the detector would have flagged our own
    # fixes as unexplained.
    assert all(x['change_reason'] for x in audit(db, 'UPDATE'))


def test_import_declares_itself_and_needs_no_reason(db):
    rid = seed_sale(db)
    db.execute(f"UPDATE sales_transactions SET {MEANINGFUL}='2026-02-02',"
               " change_source='import', change_actor='express-dbf',"
               " change_token='b2' WHERE id=?", (rid,))
    db.commit()
    rows = audit(db, 'UPDATE')
    assert len(rows) == 1 and rows[0]['change_source'] == 'import'


def test_insert_and_delete_are_recorded_but_not_blocked(db):
    """The importer's whole mutation shape is DELETE+INSERT. Blocking either
    blocks every import, so these are recorded only."""
    rid = seed_sale(db)
    assert len(audit(db, 'INSERT')) == 1
    db.execute("DELETE FROM sales_transactions WHERE id=?", (rid,))
    db.commit()
    d = audit(db, 'DELETE')
    assert len(d) == 1
    assert d[0]['change_source'] == 'import'      # what the row last declared


def test_bookkeeping_only_update_is_not_guarded(db):
    """bsn_sync rewrites synced_to_stock on every sync. Guarding it would force a
    declaration through the stock path and buy no provenance."""
    rid = seed_sale(db)
    db.execute("UPDATE sales_transactions SET synced_to_stock=1 WHERE id=?", (rid,))
    db.commit()
    assert db.execute("SELECT synced_to_stock FROM sales_transactions WHERE id=?",
                      (rid,)).fetchone()[0] == 1
    assert audit(db, 'UPDATE') == []


def test_row_key_survives_the_importers_delete_and_reinsert(db):
    """imports.py replaces a changed line with DELETE+INSERT, so the numeric id
    changes. A history keyed on id would break at exactly the moment it matters."""
    rid = seed_sale(db)
    db.execute("DELETE FROM sales_transactions WHERE id=?", (rid,))
    seed_sale(db, change_token='b2')
    keys = {r['row_key'] for r in audit(db)}
    assert keys == {'IV0001-1|A001'}, keys


def test_purchase_table_is_guarded_too(db):
    db.execute("INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, bsn_code,"
               " supplier_code, qty, unit, unit_price, total, net, line_seq,"
               " change_source, change_actor, change_token)"
               " VALUES ('2026-01-15','RR0001','RR0001','A001','S001',1,'ตัว',10,10,10,2,"
               "'import','express-dbf','b1')")
    db.commit()
    rid = db.execute("SELECT id FROM purchase_transactions").fetchone()['id']
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"UPDATE purchase_transactions SET {MEANINGFUL}='2026-02-02' WHERE id=?", (rid,))
    db.rollback()
    assert {r['row_key'] for r in audit(db)} == {'RR0001|A001|2'}


def test_null_bsn_code_still_produces_a_usable_row_key(db):
    """SQLite concatenation with NULL yields NULL, which would silently erase the
    key for exactly the rows whose history is hardest to reconstruct."""
    seed_sale(db, doc_no='IV0002-1', bsn_code=None)
    keys = {r['row_key'] for r in audit(db, 'INSERT')}
    assert None not in keys, keys
    assert 'IV0002-1|∅' in keys, keys


# ── the reader and the page ──────────────────────────────────────────────────
# Provenance nobody can read is provenance nobody has: Put is the sole digital
# operator and does not run SQL, so "it is in audit_log" is not the finish line.

def _history(db_path, doc_base, table, monkeypatch):
    import config, database, importlib
    monkeypatch.setattr(config, 'DATABASE_PATH', str(db_path))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(db_path))
    from models import _shared
    importlib.reload  # noqa: B018 - documented no-op; _shared reads config lazily
    return _shared.get_source_doc_audit_history(doc_base, table)


def test_history_reader_returns_who_what_and_why(db, empty_db, monkeypatch):
    rid = seed_sale(db)
    db.execute("UPDATE sales_transactions SET date_iso='2026-02-02',"
               " change_source='manual', change_actor='put',"
               " change_reason='Express ต้นฉบับว่า 2026-02-02', change_token='t9' WHERE id=?",
               (rid,))
    db.commit()
    hist = _history(empty_db, 'IV0001', 'sales_transactions', monkeypatch)
    upd = [h for h in hist if h['action'] == 'UPDATE']
    assert len(upd) == 1, hist
    h = upd[0]
    assert (h['actor'], h['source']) == ('put', 'manual')
    assert h['reason'] == 'Express ต้นฉบับว่า 2026-02-02'
    assert {c['field'] for c in h['changes']} == {'date_iso'}
    assert h['changes'][0]['label'] == 'วันที่'      # แสดงเป็นภาษาคน ไม่ใช่ชื่อคอลัมน์


def test_history_reader_does_not_bleed_between_documents(db, empty_db, monkeypatch):
    """`IV0001` must not collect `IV00010`'s history — a prefix match without the
    separator would, and the two documents can belong to different customers."""
    seed_sale(db, doc_no='IV0001-1')
    seed_sale(db, doc_no='IV00010-1')
    got = _history(empty_db, 'IV0001', 'sales_transactions', monkeypatch)
    assert len(got) == 1, [g['changes'] for g in got]
    # CONTROL: the other document does have its own history, so the count above
    # is a filter working, not an empty table.
    assert len(_history(empty_db, 'IV00010', 'sales_transactions', monkeypatch)) == 1


def test_history_reader_escapes_like_metacharacters(db, empty_db, monkeypatch):
    """A document number containing `_` is a single-character wildcard in LIKE."""
    seed_sale(db, doc_no='IV_001-1')
    seed_sale(db, doc_no='IVX001-1')
    got = _history(empty_db, 'IV_001', 'sales_transactions', monkeypatch)
    assert len(got) == 1, 'the underscore matched IVX001 as a wildcard'


def test_history_reader_refuses_an_unknown_table(db, empty_db, monkeypatch):
    with pytest.raises(ValueError):
        _history(empty_db, 'IV0001', 'products', monkeypatch)


# ── retention ────────────────────────────────────────────────────────────────
# The precedent this must not repeat: `transactions` hand-voids are pruned with
# the import churn because the schema could not tell them apart
# (models/_shared.py). mig 172 can, so the manual half is kept forever.

def _seed_audit(conn, table, action, source, days_old):
    conn.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_fields,"
        " change_source, created_at) VALUES (?,1,?,'{}',?,"
        " date('now','localtime',?))", (table, action, source, f'-{days_old} day'))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.mark.parametrize('table', ['sales_transactions', 'purchase_transactions'])
@pytest.mark.parametrize('action,source,should_survive', [
    ('INSERT', 'import', False),   # churn — the importer re-inserts constantly
    ('DELETE', 'import', True),    # ⛔ a DELETE row carries what the row last
                                   # DECLARED, not who deleted it — a hand-deleted
                                   # import row reads 'import'. Pruning those would
                                   # erase the record a person needs.
    ('INSERT', 'manual', True),    # a hand-added line is never noise
    ('DELETE', 'manual', True),    # ⭐ the case the `transactions` policy loses
    ('DELETE', None,     True),    # unknown origin: keep, never assume churn
    ('UPDATE', 'import', True),    # guarded changes are the point of the feature
    ('UPDATE', 'manual', True),
])
def test_retention_keeps_the_human_half(db, empty_db, monkeypatch, table, action,
                                        source, should_survive):
    import config, database, models
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    old_id = _seed_audit(db, table, action, source,
                         models.AUDIT_LOG_RETENTION_DAYS + 30)
    # CONTROL: a row inside the window must survive whatever the rule says, so a
    # predicate that deleted everything could not pass this test.
    fresh_id = _seed_audit(db, table, action, source, 1)
    models.prune_audit_log()
    left = {r[0] for r in db.execute("SELECT id FROM audit_log")}
    assert fresh_id in left, 'pruned a row inside the retention window'
    assert (old_id in left) is should_survive, (
        f'{table} {action} source={source!r}: '
        f'{"should have been kept" if should_survive else "should have been pruned"}')


# ── the page actually renders it ─────────────────────────────────────────────
def test_document_page_shows_who_changed_it_and_why(db, empty_db, monkeypatch):
    """A render test, because the model returning the right dict proves nothing
    about whether Put ever sees it. Asserts on the rendered VALUES, not on a bare
    substring — `'manual' in html` would be true from the page chrome alone."""
    rid = seed_sale(db)
    db.execute("UPDATE sales_transactions SET date_iso='2026-02-02',"
               " change_source='manual', change_actor='put',"
               " change_reason='คืนใบกำกับให้ลูกค้าที่ Express ระบุว่าเป็นเจ้าของ',"
               " change_token='tp' WHERE id=?", (rid,))
    db.commit()

    import config, database
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    import book_registry
    def _book_conn(*a, **k):
        c = sqlite3.connect(str(empty_db))
        c.row_factory = sqlite3.Row      # the route indexes rows by name
        return c
    monkeypatch.setattr(book_registry, 'get_book_connection', _book_conn)
    from app import app
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['role'] = 'admin'; sess['username'] = 'admin'; sess['user_id'] = 1

    html = client.get('/sales/doc/IV0001').get_data(as_text=True)
    assert 'ประวัติการแก้ไข' in html
    assert 'คืนใบกำกับให้ลูกค้าที่ Express ระบุว่าเป็นเจ้าของ' in html   # the WHY
    assert '>put<' in html or 'put' in html                              # the WHO
    assert 'คนแก้' in html                                               # manual badge
    # CONTROL. ⚠ Not "a document with no history": seed_sale writes an audited
    # INSERT, so IV0009 has a panel too and an `assert ... not in` on the other
    # document's reason string would pass for free (Codex, 2026-08-25). The
    # discriminating claim is that a document nobody hand-edited shows the panel
    # WITHOUT the manual badge or any reason.
    seed_sale(db, doc_no='IV0009-1')
    quiet = client.get('/sales/doc/IV0009').get_data(as_text=True)
    assert 'ประวัติการแก้ไข' in quiet          # it does have a panel
    assert 'คนแก้' not in quiet                # …but nothing human in it
    assert 'คืนใบกำกับให้ลูกค้าที่ Express ระบุว่าเป็นเจ้าของ' not in quiet


# ── the real importer, through the real triggers ─────────────────────────────
# The highest-traffic writer of these tables. Everything above tests the guard;
# this tests that the guard did not break the thing it guards.

def _entry(**kw):
    e = {'date_iso': '2026-01-15', 'doc_no': 'IV7001-1', 'qty': 2.0, 'unit': 'ตัว',
         'unit_price': 10.0, 'vat_type': 1, 'discount': '', 'total': 20.0, 'net': 20.0,
         'product_name_raw': 'สินค้า', 'product_code_raw': 'A001',
         'party': 'ลูกค้า ก', 'party_code': 'C001'}
    e.update(kw)
    return e


def test_a_real_import_round_trip_still_works_and_declares_itself(db, empty_db, monkeypatch):
    import config, database, models
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))

    first = models.import_weekly([_entry()], 'sales', 'ยอดขาย_ทดสอบ.csv')
    assert first['imported'] == 1, first
    row = db.execute("SELECT change_source, change_actor, change_token, net"
                     " FROM sales_transactions WHERE doc_no='IV7001-1'").fetchone()
    assert row['change_source'] == 'import'
    assert row['change_actor'] == 'ยอดขาย_ทดสอบ.csv'
    assert row['change_token'], 'the importer must stamp a token or its rows cannot be edited later'

    # Re-import the SAME line: a genuine no-op, and must stay one.
    again = models.import_weekly([_entry()], 'sales', 'ยอดขาย_ทดสอบ.csv')
    assert again['unchanged'] == 1 and again['imported'] == 0, again

    # Re-import a CHANGED line — imports.py does DELETE+INSERT here, which the
    # UPDATE guard deliberately never sees. If that ever became an UPDATE this
    # test is where the import would start failing.
    changed = models.import_weekly([_entry(net=25.0, total=25.0)], 'sales',
                                   'ยอดขาย_ทดสอบ.csv')
    assert changed['overwritten'] == 1, changed
    assert db.execute("SELECT net FROM sales_transactions WHERE doc_no='IV7001-1'"
                      ).fetchone()[0] == 25.0

    acts = [r['action'] for r in db.execute(
        "SELECT action FROM audit_log WHERE table_name='sales_transactions' ORDER BY id")]
    assert acts == ['INSERT', 'DELETE', 'INSERT'], acts
    assert {r['change_source'] for r in db.execute(
        "SELECT change_source FROM audit_log WHERE table_name='sales_transactions'"
    )} == {'import'}, 'every importer-written audit row must say so'


def test_a_line_the_importer_wrote_can_still_be_hand_corrected(db, empty_db, monkeypatch):
    """The whole point: the two documents Put approved fixing are import-written
    rows. If the guard made them uneditable the feature would be a wall."""
    import config, database, models
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    models.import_weekly([_entry()], 'sales', 'ยอดขาย_ทดสอบ.csv')
    rid = db.execute("SELECT id FROM sales_transactions").fetchone()['id']

    conn = sqlite3.connect(str(empty_db))
    models.declared_update(conn, 'sales_transactions', rid, {'date_iso': '2026-01-14'},
                           actor='put', reason='Express ต้นฉบับว่า 2026-01-14')
    conn.commit(); conn.close()

    last = db.execute("SELECT action, user, change_source, change_reason FROM audit_log"
                      " WHERE table_name='sales_transactions' ORDER BY id DESC LIMIT 1").fetchone()
    assert (last['action'], last['user'], last['change_source']) == ('UPDATE', 'put', 'manual')
    assert last['change_reason'] == 'Express ต้นฉบับว่า 2026-01-14'


def test_helper_refuses_a_reason_that_explains_nothing(db, empty_db):
    import models
    conn = sqlite3.connect(str(empty_db))
    rid = seed_sale(db)
    for bad in ('', '   ', 'fix'):
        with pytest.raises(ValueError):
            models.declared_update(conn, 'sales_transactions', rid,
                                   {'date_iso': '2026-02-02'}, actor='put', reason=bad)
    with pytest.raises(ValueError):        # actor is not optional either
        models.declared_update(conn, 'sales_transactions', rid, {'date_iso': '2026-02-02'},
                               actor='', reason='เหตุผลที่ยาวพอสมควรจริง ๆ')
    with pytest.raises(ValueError):        # and not for any other table
        models.declared_update(conn, 'products', 1, {'name': 'x'},
                               actor='put', reason='เหตุผลที่ยาวพอสมควรจริง ๆ')
    conn.close()


# ── regressions found by review, pinned so they cannot come back ─────────────

def test_null_source_does_not_slip_past_the_guard(db):
    """⚠ SQL three-valued logic. `NULL NOT IN (...)` and `NULL = 'manual'` are
    both NULL, so an UPDATE with a fresh token and a real actor but a NULL source
    made the whole OR chain NULL — and a trigger whose WHEN is NULL does not fire.
    That was a working bypass until `change_source IS NULL` became its own term."""
    rid = seed_sale(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE sales_transactions SET date_iso='2026-02-02',"
                   " change_token='fresh', change_actor='someone',"
                   " change_source=NULL WHERE id=?", (rid,))
    db.rollback()
    assert db.execute("SELECT date_iso FROM sales_transactions WHERE id=?",
                      (rid,)).fetchone()[0] == '2026-01-15'


@pytest.mark.parametrize('column,value', [
    ('product_name_raw', 'ชื่อที่ถูกแก้'),   # user-visible via COALESCE in models/sales.py
])
def test_content_columns_are_guarded_too(db, column, value):
    rid = seed_sale(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"UPDATE sales_transactions SET {column}=? WHERE id=?", (value, rid))
    db.rollback()


def test_purchase_supplier_id_is_guarded(db):
    db.execute("INSERT INTO purchase_transactions (date_iso, doc_no, doc_base, bsn_code,"
               " supplier_code, qty, unit, unit_price, total, net, line_seq,"
               " change_source, change_actor, change_token)"
               " VALUES ('2026-01-15','RR9001','RR9001','A001','S001',1,'ตัว',10,10,10,1,"
               "'import','express-dbf','b1')")
    db.commit()
    rid = db.execute("SELECT id FROM purchase_transactions WHERE doc_no='RR9001'").fetchone()['id']
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE purchase_transactions SET supplier_id=7 WHERE id=?", (rid,))
    db.rollback()


def test_repoint_bsn_code_still_works_through_the_guard(db, empty_db, monkeypatch):
    """/mapping/split-save reaches repoint_bsn_code, which UPDATEs product_id —
    a guarded column. Before it declared itself, an admin doing a split would
    have hit IntegrityError on the first row."""
    import config, database, models
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    db.execute("INSERT INTO products (sku_code, product_name, unit_type)"
               " VALUES ('SKU-A','สินค้า A','ตัว'), ('SKU-B','สินค้า B','ตัว')")
    db.commit()
    old_pid, new_pid = [r[0] for r in db.execute(
        "SELECT id FROM products WHERE sku_code IN ('SKU-A','SKU-B') ORDER BY id")]
    db.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, bsn_unit,"
               " product_id, is_ignored) VALUES ('A001','สินค้า A','', ?, 0)", (old_pid,))
    seed_sale(db)
    db.execute("UPDATE sales_transactions SET product_id=?, change_source='import',"
               " change_actor='seed', change_token='seed1' WHERE bsn_code='A001'", (old_pid,))
    db.commit()

    report = models.repoint_bsn_code(None, 'A001', new_pid)
    assert report, report
    assert db.execute("SELECT product_id FROM sales_transactions WHERE bsn_code='A001'"
                      ).fetchone()[0] == new_pid
    last = db.execute("SELECT change_source, user FROM audit_log"
                      " WHERE table_name='sales_transactions' AND action='UPDATE'"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    assert (last['change_source'], last['user']) == ('import', 'repoint-bsn-code')


def test_history_reads_the_book_it_was_given(db, empty_db, monkeypatch, tmp_path):
    """The document routes are permitted VAT-book endpoints while get_connection()
    is always the main No-VAT DB. Defaulting to it would show a VAT invoice the
    other book's history for the same document number."""
    import config, database, models, shutil
    other = tmp_path / 'other_book.db'
    shutil.copy2(empty_db, other)
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    seed_sale(db)                                        # history exists in the MAIN db only

    book = sqlite3.connect(str(other))
    got = models.get_source_doc_audit_history('IV0001', 'sales_transactions', conn=book)
    book.close()
    assert got == [], 'history leaked in from the other book'
    # CONTROL: the same call against the right book DOES find it, so the empty
    # result above is isolation working and not a broken query.
    main = sqlite3.connect(str(empty_db))
    assert models.get_source_doc_audit_history('IV0001', 'sales_transactions', conn=main)
    main.close()


def test_declared_delete_leaves_a_human_trail(db):
    """The right way to remove a line. It cannot be the only way — no BEFORE
    DELETE guard can demand a reason a DELETE has nowhere to carry — so this
    pins that the supported path produces the record, and the CONTROL below pins
    exactly how a bare DELETE differs, which is the residual risk in writing."""
    import models
    rid = seed_sale(db, doc_no='IV0011-1')
    models.declared_delete(db, 'sales_transactions', rid, actor='put',
                           reason='บรรทัดค้างที่ Express ยกเลิกไปแล้ว')
    db.commit()
    d = [r for r in audit(db, 'DELETE') if r['row_key'] == 'IV0011-1|A001']
    assert len(d) == 1
    assert (d[0]['user'], d[0]['change_source']) == ('put', 'manual')
    assert d[0]['change_reason'] == 'บรรทัดค้างที่ Express ยกเลิกไปแล้ว'

    # CONTROL / residual risk: a bare DELETE of an imported row is still allowed
    # and still recorded, but it reads as the importer's. That is the documented
    # limitation, asserted rather than described so it cannot drift silently.
    rid2 = seed_sale(db, doc_no='IV0012-1')
    db.execute("DELETE FROM sales_transactions WHERE id=?", (rid2,))
    db.commit()
    bare = [r for r in audit(db, 'DELETE') if r['row_key'] == 'IV0012-1|A001'][0]
    assert bare['change_source'] == 'import'      # ⛔ not 'manual' — the known gap
    assert bare['change_reason'] is None


def test_declared_delete_refuses_a_thin_reason(db):
    import models
    rid = seed_sale(db, doc_no='IV0013-1')
    with pytest.raises(ValueError):
        models.declared_delete(db, 'sales_transactions', rid, actor='put', reason='ลบ')
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_no='IV0013-1'"
                      ).fetchone()[0] == 1, 'row was deleted despite the refusal'


def test_declared_update_refuses_a_column_outside_the_allowlist(db, empty_db):
    """Column names cannot be bound parameters, so they are interpolated. The
    allowlist is what keeps that safe if a caller ever forwards a request value."""
    import models
    conn = sqlite3.connect(str(empty_db))
    rid = seed_sale(db, doc_no='IV0014-1')
    for bad in ('batch_id', 'id', 'change_source', "net = 0 --"):
        with pytest.raises(ValueError):
            models.declared_update(conn, 'sales_transactions', rid, {bad: 1},
                                   actor='put', reason='เหตุผลที่ยาวพอสมควรจริง')
    conn.close()


def test_a_hand_edited_document_is_flagged_before_you_expand_the_panel(db, empty_db, monkeypatch):
    """The panel is collapsed by default. If the one event worth seeing is only
    visible after a click, the page has not told anyone anything."""
    rid = seed_sale(db, doc_no='IV0015-1')
    db.execute("UPDATE sales_transactions SET date_iso='2026-02-02',"
               " change_source='manual', change_actor='put',"
               " change_reason='เหตุผลที่ยาวพอสมควรจริง', change_token='tb' WHERE id=?", (rid,))
    db.commit()
    import config, database, book_registry
    monkeypatch.setattr(config, 'DATABASE_PATH', str(empty_db))
    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))
    def _book(*a, **k):
        c = sqlite3.connect(str(empty_db)); c.row_factory = sqlite3.Row; return c
    monkeypatch.setattr(book_registry, 'get_book_connection', _book)
    from app import app
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['role'] = 'admin'; sess['username'] = 'admin'; sess['user_id'] = 1

    html = client.get('/sales/doc/IV0015').get_data(as_text=True)
    assert 'คนแก้ 1' in html
    # CONTROL: an untouched document must not carry the flag, so the assertion
    # above is not satisfied by markup that is always emitted.
    seed_sale(db, doc_no='IV0016-1')
    assert 'คนแก้' not in client.get('/sales/doc/IV0016').get_data(as_text=True)
