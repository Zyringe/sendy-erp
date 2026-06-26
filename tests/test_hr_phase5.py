"""Phase 5 — Self-service leave management.

Task 5.1: Migration 118 — add approved_by + approved_at columns to leave_requests.
"""
import sqlite3

import pytest


def test_leave_requests_has_approval_columns(tmp_db):
    """leave_requests must have approved_by and approved_at TEXT columns."""
    cols = {r[1] for r in sqlite3.connect(tmp_db).execute("PRAGMA table_info(leave_requests)")}
    assert {'approved_by', 'approved_at'} <= cols
