"""Lifecycle tests for review_rules v2 (doc-keyed, suspicious-only).

Old batch-scoped tests (scan_batch, mark_doc, get_batch_review,
get_sales_batches, pending_review_count) removed in Task 7 cutover.
scan_all / scan_docs / scan_after_import / get_review_feed are tested
in test_review_doc_eval.py.
"""
import sqlite3
import os
import pytest


class TestMigration:
    def test_tables_created_by_migration(self, empty_db):
        """Applying mig 099 creates v2 review tables.

        Assertions check the v2 schema: txn_review_docs has doc_base TEXT
        PRIMARY KEY and free_goods_note; no review_status column.
        """
        mig_path = os.path.join(
            os.path.dirname(__file__), '..', 'data', 'migrations',
            '099_txn_review_v2.sql'
        )
        conn = sqlite3.connect(empty_db)
        conn.execute("DROP TABLE IF EXISTS txn_review_flags")
        conn.execute("DROP TABLE IF EXISTS txn_review_docs")
        conn.commit()
        with open(mig_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        conn.executescript(sql)

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'txn_review_docs' in tables
        assert 'txn_review_flags' in tables

        # v2: doc_base is the PRIMARY KEY (no batch_id column)
        cols_docs = {r[1] for r in conn.execute(
            "PRAGMA table_info(txn_review_docs)"
        ).fetchall()}
        assert 'doc_base' in cols_docs
        assert 'free_goods_note' in cols_docs
        assert 'review_status' not in cols_docs
        assert 'batch_id' not in cols_docs

        # flags reference doc_base, not doc_review_id
        cols_flags = {r[1] for r in conn.execute(
            "PRAGMA table_info(txn_review_flags)"
        ).fetchall()}
        assert 'doc_base' in cols_flags
        assert 'doc_review_id' not in cols_flags

        conn.close()
