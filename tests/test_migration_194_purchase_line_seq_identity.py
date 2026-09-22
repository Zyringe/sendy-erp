"""Migration 194: make stored purchase identity match the DBF/text writers."""
import datetime
import os
import sqlite3

from express_dbf_source import build_purchase_entries


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIGRATION = os.path.join(
    REPO, "data", "migrations", "194_purchase_line_seq_identity.sql"
)
ROLLBACK = os.path.join(
    REPO, "data", "migrations", "194_purchase_line_seq_identity.rollback.sql"
)


def _sql(path):
    with open(path, encoding="utf-8") as src:
        return src.read()


def _purchase_row(conn, *, doc_no, code, seq, qty, product_id, actor):
    return conn.execute(
        """
        INSERT INTO purchase_transactions
            (date_iso, doc_no, doc_base, product_id, bsn_code,
             product_name_raw, supplier, supplier_code, qty, unit,
             unit_price, vat_type, discount, total, net, synced_to_stock,
             line_seq, change_source, change_actor, change_token)
        VALUES
            ('2026-09-21', ?, ?, ?, ?, ?, 'supplier', 'SUP-637', ?, 'ตัว',
             10, 0, '', ?, ?, 1, ?, 'import', ?, ?)
        """,
        (
            doc_no,
            doc_no,
            product_id,
            code,
            f"product {code}",
            qty,
            qty * 10,
            qty * 10,
            seq,
            actor,
            f"seed-{actor}",
        ),
    ).lastrowid


def _seed_pre194(conn):
    pids = {}
    for code, cost in (("P-A", 11.25), ("P-B", 22.5), ("P-K", 33.75)):
        pids[code] = conn.execute(
            "INSERT INTO products (product_name, unit_type, cost_price, opening_cost) "
            "VALUES (?, 'ตัว', ?, ?)",
            (f"product {code}", cost, cost),
        ).lastrowid

    purchase_ids = [
        _purchase_row(
            conn, doc_no="HP637M", code="P-A", seq=3, qty=30,
            product_id=pids["P-A"], actor="seed-a3",
        ),
        _purchase_row(
            conn, doc_no="HP637M", code="P-B", seq=2, qty=20,
            product_id=pids["P-B"], actor="seed-b2",
        ),
        _purchase_row(
            conn, doc_no="HP637M", code="P-A", seq=1, qty=10,
            product_id=pids["P-A"], actor="seed-a1",
        ),
        # Far side of line_seq <> ROW_NUMBER(): this group is already canonical.
        _purchase_row(
            conn, doc_no="HP637KEEP", code="P-K", seq=1, qty=5,
            product_id=pids["P-K"], actor="seed-k1",
        ),
        _purchase_row(
            conn, doc_no="HP637KEEP", code="P-K", seq=2, qty=6,
            product_id=pids["P-K"], actor="seed-k2",
        ),
    ]

    txn_ids = []
    for code, seq, qty in (("P-A", 3, 30), ("P-B", 2, 20), ("P-A", 1, 10)):
        txn_ids.append(
            conn.execute(
                """
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at,
                     source_bsn_code, source_line_seq)
                VALUES (?, 'IN', ?, 'unit', 'HP637M', 'BSN ซื้อ',
                        '2026-09-21 00:00:00', ?, ?)
                """,
                (pids[code], qty, code, seq),
            ).lastrowid
        )

    # Dormant rollback data from mig 156 also carries purchase source identity.
    conn.execute(
        """
        INSERT INTO migration_156_deleted_ledger
            (id, product_id, txn_type, quantity_change, unit_mode,
             reference_no, note, created_at, source_bsn_code, source_line_seq)
        VALUES (963700, ?, 'IN', 20, 'unit', 'HP637M', 'BSN ซื้อ',
                '2026-09-21 00:00:00', 'P-B', 2)
        """,
        (pids["P-B"],),
    )

    # Far side of the identity join: a source key absent from purchase rows.
    unmatched_txn = conn.execute(
        """
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode,
             reference_no, note, created_at, source_bsn_code, source_line_seq)
        VALUES (?, 'ADJUST', 7, 'unit', 'HP637OTHER', 'control',
                '2026-09-21 00:00:00', 'P-K', 9)
        """,
        (pids["P-K"],),
    ).lastrowid
    conn.commit()
    return {
        "pids": pids,
        "purchase_ids": purchase_ids,
        "txn_ids": txn_ids,
        "unmatched_txn": unmatched_txn,
    }


def _raw_builder_rows():
    header = [{
        "DOCNUM": "HP637M",
        "RECTYP": "1",
        "SUPCOD": "SUP-637",
        "FLGVAT": 0,
        "DOCDAT": datetime.date(2026, 9, 21),
    }]
    rows = []
    for seq, code, qty in ((3, "P-A", 30), (2, "P-B", 20), (1, "P-A", 10)):
        rows.append({
            "DOCNUM": "HP637M",
            "SEQNUM": seq,
            "STKCOD": code,
            "STKDES": f"product {code}",
            "TRNQTY": qty,
            "TQUCOD": "ตัว",
            "UNITPR": 10,
            "DISC": "",
            "TRNVAL": qty * 10,
            "NETVAL": qty * 10,
        })
    return build_purchase_entries(
        header, rows, [{"SUPCOD": "SUP-637", "SUPNAM": "supplier"}]
    )


def _rows(conn, table, where="1", params=()):
    return [tuple(row) for row in conn.execute(
        f"SELECT * FROM {table} WHERE {where} ORDER BY id", params
    ).fetchall()]


def _business_payload(conn):
    """Columns migration 194 must not move, compared row-for-row."""
    return {
        "purchase": [tuple(r) for r in conn.execute(
            "SELECT id, product_id, qty, unit, unit_price, total, net, "
            "synced_to_stock FROM purchase_transactions ORDER BY id"
        )],
        "transactions": [tuple(r) for r in conn.execute(
            "SELECT id, product_id, txn_type, quantity_change, unit_mode, "
            "reference_no, note, created_at FROM transactions ORDER BY id"
        )],
        "stock": [tuple(r) for r in conn.execute(
            "SELECT product_id, quantity FROM stock_levels ORDER BY product_id"
        )],
        "cost": [tuple(r) for r in conn.execute(
            "SELECT id, cost_price, opening_cost FROM products ORDER BY id"
        )],
        "cost_ledger": [tuple(r) for r in conn.execute(
            "SELECT * FROM product_cost_ledger ORDER BY id"
        )],
    }


def test_migration_output_equals_builder_in_both_directions(empty_db_conn):
    conn = empty_db_conn
    seeded = _seed_pre194(conn)
    payload_before = _business_payload(conn)

    conn.executescript(_sql(MIGRATION))

    expected = {
        (e["doc_no"], e["product_code_raw"], e["qty"], e["line_seq"])
        for e in _raw_builder_rows()
    }
    stored = {
        tuple(r) for r in conn.execute(
            "SELECT doc_no, bsn_code, qty, line_seq "
            "FROM purchase_transactions WHERE doc_no='HP637M'"
        )
    }
    assert len(expected) == 3
    assert len(stored) == 3
    assert stored - expected == set(), "migration produced a row the builder would not"
    assert expected - stored == set(), "builder produced a row the migration missed"

    txn_sources = [tuple(r) for r in conn.execute(
        "SELECT source_bsn_code, quantity_change, source_line_seq "
        "FROM transactions WHERE reference_no='HP637M' ORDER BY quantity_change"
    )]
    assert len(txn_sources) == 3
    assert txn_sources == [("P-A", 10, 1), ("P-B", 20, 1), ("P-A", 30, 2)]
    assert conn.execute(
        "SELECT source_line_seq FROM migration_156_deleted_ledger WHERE id=963700"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT source_line_seq FROM transactions WHERE id=?",
        (seeded["unmatched_txn"],),
    ).fetchone()[0] == 9

    # Counts first: only the two non-canonical purchase/source rows were written.
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_194_purchase_line_seq"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_194_transaction_source_line_seq"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_194_mig156_source_line_seq"
    ).fetchone()[0] == 1
    assert _business_payload(conn) == payload_before


def test_rollback_restores_rows_byte_for_byte(empty_db_conn):
    conn = empty_db_conn
    seeded = _seed_pre194(conn)
    pt_before = _rows(conn, "purchase_transactions", "doc_no LIKE 'HP637%'")
    txn_before = _rows(
        conn, "transactions", "id IN (?,?,?,?)",
        (*seeded["txn_ids"], seeded["unmatched_txn"]),
    )
    mig156_before = _rows(conn, "migration_156_deleted_ledger", "id=963700")
    payload_before = _business_payload(conn)

    conn.executescript(_sql(MIGRATION))
    snapshot_counts = tuple(conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM migration_194_purchase_line_seq),
               (SELECT COUNT(*) FROM migration_194_transaction_source_line_seq),
               (SELECT COUNT(*) FROM migration_194_mig156_source_line_seq)
        """
    ).fetchone())
    assert snapshot_counts == (2, 2, 1)

    conn.executescript(_sql(ROLLBACK))

    assert _rows(conn, "purchase_transactions", "doc_no LIKE 'HP637%'") == pt_before
    assert _rows(
        conn, "transactions", "id IN (?,?,?,?)",
        (*seeded["txn_ids"], seeded["unmatched_txn"]),
    ) == txn_before
    assert _rows(conn, "migration_156_deleted_ledger", "id=963700") == mig156_before
    assert _business_payload(conn) == payload_before
    remaining = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE name LIKE 'migration_194_%'"
    ).fetchone()[0]
    assert remaining == 0


def test_forward_is_rerunnable_without_rewriting_business_rows(empty_db_conn):
    conn = empty_db_conn
    seeded = _seed_pre194(conn)
    pt_before = _rows(conn, "purchase_transactions", "doc_no LIKE 'HP637%'")
    txn_before = _rows(
        conn, "transactions", "id IN (?,?,?,?)",
        (*seeded["txn_ids"], seeded["unmatched_txn"]),
    )
    conn.executescript(_sql(MIGRATION))
    migrated_purchase = _rows(conn, "purchase_transactions", "doc_no LIKE 'HP637%'")
    migrated_transactions = _rows(
        conn, "transactions", "reference_no LIKE 'HP637%'"
    )
    migrated_mig156 = _rows(conn, "migration_156_deleted_ledger", "id=963700")
    audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    conn.executescript(_sql(MIGRATION))

    assert _rows(
        conn, "purchase_transactions", "doc_no LIKE 'HP637%'"
    ) == migrated_purchase
    assert _rows(
        conn, "transactions", "reference_no LIKE 'HP637%'"
    ) == migrated_transactions
    assert _rows(
        conn, "migration_156_deleted_ledger", "id=963700"
    ) == migrated_mig156
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_count

    # The re-run must NOT drop its own rollback evidence: dropping and
    # recreating the snapshots would leave them empty (the second run's remap
    # is empty by construction) and silently disarm the rollback below.
    kept_snapshots = tuple(conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM migration_194_purchase_line_seq),
               (SELECT COUNT(*) FROM migration_194_transaction_source_line_seq),
               (SELECT COUNT(*) FROM migration_194_mig156_source_line_seq)
        """
    ).fetchone())
    assert kept_snapshots == (2, 2, 1)

    conn.executescript(_sql(ROLLBACK))
    assert _rows(conn, "purchase_transactions", "doc_no LIKE 'HP637%'") == pt_before
    assert _rows(
        conn, "transactions", "id IN (?,?,?,?)",
        (*seeded["txn_ids"], seeded["unmatched_txn"]),
    ) == txn_before


def test_runner_migrates_worktree_snapshot_without_stock_cost_or_quantity_move(tmp_db):
    import database

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        changed_before = [tuple(r) for r in conn.execute(
            """
            WITH ranked AS (
                SELECT id, doc_no, bsn_code, product_id, line_seq AS old_seq,
                       ROW_NUMBER() OVER (
                           PARTITION BY doc_no, bsn_code ORDER BY line_seq, id
                       ) AS new_seq
                  FROM purchase_transactions
            )
            SELECT id, doc_no, bsn_code, product_id, old_seq, new_seq
              FROM ranked WHERE old_seq <> new_seq ORDER BY id
            """
        )]
        assert len(changed_before) == 47
        payload_before = _business_payload(conn)
    finally:
        conn.close()

    # The real runner supplies the migration actor required by the #590 guard.
    database.init_db(db_path=tmp_db)

    conn = sqlite3.connect(tmp_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM applied_migrations "
            "WHERE filename='194_purchase_line_seq_identity.sql'"
        ).fetchone()[0] == 1
        counts = tuple(conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM migration_194_purchase_line_seq),
                   (SELECT COUNT(*) FROM migration_194_transaction_source_line_seq),
                   (SELECT COUNT(*) FROM migration_194_mig156_source_line_seq)
            """
        ).fetchone())
        assert counts == (47, 47, 0)
        scope = tuple(conn.execute(
            """
            SELECT COUNT(DISTINCT p.doc_no), COUNT(DISTINCT p.product_id)
              FROM purchase_transactions p
              JOIN migration_194_purchase_line_seq s ON s.id = p.id
            """
        ).fetchone())
        assert scope == (25, 41)
        assert conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT line_seq,
                       ROW_NUMBER() OVER (
                           PARTITION BY doc_no, bsn_code ORDER BY line_seq, id
                       ) AS wanted
                  FROM purchase_transactions
            ) WHERE line_seq <> wanted
            """
        ).fetchone()[0] == 0
        assert _business_payload(conn) == payload_before
    finally:
        conn.close()
