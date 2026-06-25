"""Phase 3 — 5-role model: users.role CHECK extended.

Task 3.1: verify migration 117 adds 'shareholder' + 'general' to the CHECK
          and that a bogus role is still rejected.
"""
import sqlite3


def test_users_role_check_allows_new_roles(tmp_db):
    conn = sqlite3.connect(tmp_db)
    # both new roles must be insertable; a bogus role must still be rejected
    conn.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES('_sh','x','sh','shareholder')")
    conn.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES('_ge','x','ge','general')")
    conn.commit()
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES('_bad','x','b','wizard')")
