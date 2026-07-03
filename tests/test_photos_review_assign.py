"""Tests for /photos/review/assign (blueprints/products.py — moved from
app.py in the Phase 4 structural refactor).

Covers the orphan-on-commit-failure bug: if shutil.move() succeeds but
conn.commit() then fails (disk full, DB locked), the photo file had already
left the _review/ queue with no product_images row ever committed — silently
orphaned. The fix moves the file back on commit failure and re-raises so the
caller sees a real error instead of a silent success.

IMPORTANT: _REVIEW_ROOT_REL / _PHOTOS_ROOT_REL point at the real
Design/photos tree by default — every test here monkeypatches both to a
tmp_path so we never touch real product photos.
"""
import os
import sqlite3

import pytest


def _make_client(tmp_review_dir, tmp_photos_dir, monkeypatch):
    import app as app_module
    import blueprints.products as products_module
    monkeypatch.setattr(products_module, '_REVIEW_ROOT_REL', tmp_review_dir)
    monkeypatch.setattr(products_module, '_PHOTOS_ROOT_REL', tmp_photos_dir)
    app_module.app.config['TESTING'] = True
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'
    return c, app_module


def _insert_product(tmp_db, sku_code):
    conn = sqlite3.connect(tmp_db)
    try:
        cur = conn.execute(
            "INSERT INTO products (product_name, sku_code) VALUES (?, ?)",
            (f"test product {sku_code}", sku_code)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class _CommitFailConn:
    """Wraps a real sqlite3 connection so .commit() raises but everything
    else (execute/close/row_factory-backed cursors) behaves normally."""

    def __init__(self, real_conn):
        self._real = real_conn

    def commit(self):
        raise sqlite3.OperationalError("simulated: disk full")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_commit_failure_moves_file_back_no_orphan(tmp_db, tmp_path, monkeypatch):
    """TDD target: on unfixed code this fails because the file stays moved
    into the photos tree with no committed product_images row (orphan)."""
    review_dir = str(tmp_path / "_review")
    photos_dir = str(tmp_path / "photos")
    os.makedirs(review_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)

    src_name = "sample.jpg"
    src_abs = os.path.join(review_dir, src_name)
    with open(src_abs, "wb") as f:
        f.write(b"fake-jpg-bytes")

    product_id = _insert_product(tmp_db, "TEST-ORPHAN-001")

    client, app_module = _make_client(review_dir, photos_dir, monkeypatch)

    import database
    real_get_connection = database.get_connection

    def _fail_commit_get_connection():
        return _CommitFailConn(real_get_connection())

    import blueprints.products as products_module
    monkeypatch.setattr(products_module, 'get_connection', _fail_commit_get_connection)

    with pytest.raises(sqlite3.OperationalError):
        client.post('/photos/review/assign', data={
            'src': src_name,
            'sku_id': str(product_id),
            'role': 'single',
        })

    # (a) file is back at its source path in the review queue
    assert os.path.isfile(src_abs), "source file should be moved back after commit failure"

    # (b) no product_images row exists (insert was never committed)
    conn = sqlite3.connect(tmp_db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM product_images WHERE sku_id=?", (product_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "product_images row must not exist when commit failed"

    # the file must not have been left behind anywhere under photos_dir either
    stray = [f for _, _, files in os.walk(photos_dir) for f in files]
    assert stray == [], f"file must not be stranded in the photos tree: {stray}"


def test_success_path_still_moves_and_commits(tmp_db, tmp_path, monkeypatch):
    """Sanity check: the happy path is unaffected by the fix."""
    review_dir = str(tmp_path / "_review")
    photos_dir = str(tmp_path / "photos")
    os.makedirs(review_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)

    src_name = "sample2.jpg"
    src_abs = os.path.join(review_dir, src_name)
    with open(src_abs, "wb") as f:
        f.write(b"fake-jpg-bytes-2")

    product_id = _insert_product(tmp_db, "TEST-OK-002")

    client, _app_module = _make_client(review_dir, photos_dir, monkeypatch)

    resp = client.post('/photos/review/assign', data={
        'src': src_name,
        'sku_id': str(product_id),
        'role': 'single',
    })
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True
    assert not os.path.isfile(src_abs)

    conn = sqlite3.connect(tmp_db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM product_images WHERE sku_id=?", (product_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
