"""Tests for db_backup.py — auto-snapshot before imports + restore + prune.

The helper is pure (takes explicit db_path/backup_dir), so these run against
tiny throwaway SQLite files in tmp — no app, no live DB.
"""
import gzip
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import db_backup


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


# ── create_backup ────────────────────────────────────────────────────────────

def test_create_backup_writes_gzipped_snapshot(tmp_path):
    db = tmp_path / "inventory.db"
    bdir = tmp_path / "backups"
    _make_db(str(db), ["a", "b", "c"])

    info = db_backup.create_backup("unified", db_path=str(db), backup_dir=str(bdir))

    assert os.path.exists(info["path"])
    assert info["name"].startswith("auto-unified-")
    assert info["name"].endswith(".db.gz")
    # it's a real gzip of a real SQLite db with the same data
    raw = tmp_path / "decoded.db"
    with gzip.open(info["path"], "rb") as fz, open(raw, "wb") as fo:
        fo.write(fz.read())
    assert _count(str(raw)) == 3


def test_create_backup_captures_committed_wal_data(tmp_path):
    """The .backup API must include data sitting in the WAL (shutil.copy of the
    bare .db file would miss it)."""
    db = tmp_path / "inventory.db"
    bdir = tmp_path / "backups"
    _make_db(str(db), ["x"])
    # write more rows in WAL mode WITHOUT checkpointing
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("y",), ("z",)])
    conn.commit()
    info = db_backup.create_backup("unified", db_path=str(db), backup_dir=str(bdir))
    conn.close()

    raw = tmp_path / "decoded.db"
    with gzip.open(info["path"], "rb") as fz, open(raw, "wb") as fo:
        fo.write(fz.read())
    assert _count(str(raw)) == 3   # x, y, z — WAL rows present


def test_create_backup_missing_db_returns_none(tmp_path):
    bdir = tmp_path / "backups"
    info = db_backup.create_backup("unified",
                                   db_path=str(tmp_path / "nope.db"),
                                   backup_dir=str(bdir))
    assert info is None   # nothing to back up; import must still proceed


def test_create_backup_refuses_when_disk_low(tmp_path, monkeypatch):
    """A backup must never fill the disk: below MIN_FREE_BYTES it raises (the
    caller turns that into a warning and lets the import proceed)."""
    db = tmp_path / "inventory.db"
    bdir = tmp_path / "backups"
    _make_db(str(db), ["a"])
    from types import SimpleNamespace
    monkeypatch.setattr(
        db_backup.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=100, used=95, free=5 * 1024 * 1024))  # 5MB free
    with pytest.raises(RuntimeError):
        db_backup.create_backup("unified", db_path=str(db), backup_dir=str(bdir))
    # safe wrapper swallows it into (None, error)
    info, err = db_backup.safe_create_backup("unified", db_path=str(db),
                                             backup_dir=str(bdir))
    assert info is None and err


# ── list_backups ─────────────────────────────────────────────────────────────

def test_list_backups_newest_first_with_metadata(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for ts in ("20260601_100000", "20260603_090000", "20260602_120000"):
        (bdir / f"auto-unified-{ts}.db.gz").write_bytes(b"x")
    # a non-auto file must be ignored
    (bdir / "inventory-pre-upload-20260603.db").write_bytes(b"x")

    rows = db_backup.list_backups(backup_dir=str(bdir))
    names = [r["name"] for r in rows]
    assert names == [
        "auto-unified-20260603_090000.db.gz",
        "auto-unified-20260602_120000.db.gz",
        "auto-unified-20260601_100000.db.gz",
    ]
    assert rows[0]["reason"] == "unified"
    assert isinstance(rows[0]["created_at"], datetime)


# ── prune ────────────────────────────────────────────────────────────────────

def _touch_backup(bdir, reason, dt):
    bdir.mkdir(exist_ok=True)
    name = f"auto-{reason}-{dt.strftime('%Y%m%d_%H%M%S')}.db.gz"
    (bdir / name).write_bytes(b"x")
    return name


def test_prune_deletes_older_than_keep_days(tmp_path):
    bdir = tmp_path / "backups"
    now = datetime(2026, 6, 10, 12, 0, 0)
    fresh = _touch_backup(bdir, "unified", now - timedelta(days=1))
    old = _touch_backup(bdir, "unified", now - timedelta(days=9))

    deleted = db_backup.prune_backups(backup_dir=str(bdir), keep_days=7,
                                      max_keep=100, now=now)
    assert old in deleted and fresh not in deleted
    assert (bdir / fresh).exists() and not (bdir / old).exists()


def test_prune_caps_count_even_when_recent(tmp_path):
    bdir = tmp_path / "backups"
    now = datetime(2026, 6, 10, 12, 0, 0)
    names = [_touch_backup(bdir, "unified", now - timedelta(hours=i))
             for i in range(6)]   # all within keep_days
    deleted = db_backup.prune_backups(backup_dir=str(bdir), keep_days=7,
                                      max_keep=3, now=now)
    # newest 3 kept, oldest 3 deleted
    remaining = sorted(p.name for p in bdir.iterdir())
    assert len(remaining) == 3
    assert names[0] in remaining   # newest (i=0)
    assert names[-1] in deleted    # oldest (i=5)


def test_prune_always_keeps_newest(tmp_path):
    bdir = tmp_path / "backups"
    now = datetime(2026, 6, 10, 12, 0, 0)
    only = _touch_backup(bdir, "unified", now - timedelta(days=30))   # ancient
    deleted = db_backup.prune_backups(backup_dir=str(bdir), keep_days=7,
                                      max_keep=10, now=now)
    assert only not in deleted   # floor: never delete the last one
    assert (bdir / only).exists()


def test_prune_never_touches_live_db_or_foreign_files(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "inventory.db").write_bytes(b"live")
    (bdir / "something-else.db").write_bytes(b"x")
    _touch_backup(bdir, "unified", datetime(2020, 1, 1))
    db_backup.prune_backups(backup_dir=str(bdir), keep_days=7, max_keep=10,
                            now=datetime(2026, 6, 10))
    assert (bdir / "inventory.db").exists()
    assert (bdir / "something-else.db").exists()


# ── restore ──────────────────────────────────────────────────────────────────

def test_restore_swaps_db_and_snapshots_current(tmp_path):
    db = tmp_path / "inventory.db"
    bdir = tmp_path / "backups"
    _make_db(str(db), ["a", "b"])                      # current = 2 rows
    snap = db_backup.create_backup("unified", db_path=str(db), backup_dir=str(bdir))
    # mutate current AFTER the snapshot (simulate a bad import adding rows)
    conn = sqlite3.connect(str(db))
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("bad1",), ("bad2",)])
    conn.commit()
    conn.close()
    assert _count(str(db)) == 4

    db_backup.restore_backup(snap["name"], db_path=str(db), backup_dir=str(bdir))

    assert _count(str(db)) == 2                        # rolled back to snapshot
    # a pre-restore safety snapshot of the 4-row state was taken
    pre = [r for r in db_backup.list_backups(backup_dir=str(bdir))
           if r["reason"] == "pre-restore"]
    assert len(pre) == 1


def test_restore_removes_stale_wal_shm_sidecars(tmp_path):
    db = tmp_path / "inventory.db"
    bdir = tmp_path / "backups"
    _make_db(str(db), ["a"])
    snap = db_backup.create_backup("unified", db_path=str(db), backup_dir=str(bdir))
    # leave orphan sidecars that, if applied to the new file, would corrupt it
    (tmp_path / "inventory.db-wal").write_bytes(b"stale")
    (tmp_path / "inventory.db-shm").write_bytes(b"stale")

    db_backup.restore_backup(snap["name"], db_path=str(db), backup_dir=str(bdir))

    assert not (tmp_path / "inventory.db-wal").exists()
    assert not (tmp_path / "inventory.db-shm").exists()
    assert _count(str(db)) == 1


def test_restore_rejects_unknown_or_foreign_name(tmp_path):
    db = tmp_path / "inventory.db"
    bdir = tmp_path / "backups"
    _make_db(str(db), ["a"])
    bdir.mkdir()
    with pytest.raises(ValueError):
        db_backup.restore_backup("../../etc/passwd", db_path=str(db),
                                 backup_dir=str(bdir))
    with pytest.raises(ValueError):
        db_backup.restore_backup("auto-unified-20260601_100000.db.gz",
                                 db_path=str(db), backup_dir=str(bdir))  # missing
