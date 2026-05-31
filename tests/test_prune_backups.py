"""Unit tests for scripts/prune_backups.py — the backup rotation tool.

Retention policy under test (Rule A):
  - keep all backups within the last `keep_days` (default 7); delete older ones
  - within the window, collapse each milestone "family" (digit-stripped label)
    to its single newest copy; daily snapshots (inventory-YYYY-MM-DD.db) are
    each kept individually and never collapsed
  - junk markers (CORRUPTED / dryrun / partial_recovery) are always deleted
  - floor: never delete the single newest backup, even if everything is old
  - the live inventory.db + its -wal/-shm sidecars and any git-tracked file
    are never candidates; sidecars follow their primary's fate
"""
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prune_backups as pb

DAY = 86400
NOW = 1_800_000_000  # fixed reference epoch for deterministic tests


def ts(days_ago):
    return NOW - days_ago * DAY


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_is_sidecar_and_primary():
    assert pb.is_sidecar('inventory.db-wal')
    assert pb.is_sidecar('inventory-2026-05-25.db-shm')
    assert not pb.is_sidecar('inventory.db')
    assert pb.primary_of('inventory.db-wal') == 'inventory.db'
    assert pb.primary_of('inventory-2026-05-25.db-shm') == 'inventory-2026-05-25.db'


def test_is_daily():
    assert pb.is_daily('inventory-2026-05-25.db')
    assert not pb.is_daily('inventory_pre_normalize_20260531_202550.db')
    assert not pb.is_daily('inventory.db')


# ── select_for_deletion ─────────────────────────────────────────────────────────

def test_old_deleted_recent_daily_kept():
    primaries = [
        ('inventory-2026-05-20.db', ts(12)),   # old -> delete
        ('inventory-2026-05-31.db', ts(1)),    # recent daily -> keep
        ('inventory-2026-05-30.db', ts(2)),    # recent daily -> keep
    ]
    out = pb.select_for_deletion(primaries, NOW, keep_days=7)
    assert out == {'inventory-2026-05-20.db'}


def test_family_collapse_keeps_only_newest():
    primaries = [
        ('inventory_pre_normalize_20260531_100000.db', ts(2)),
        ('inventory_pre_normalize_20260531_120000.db', ts(1)),   # newest of family
        ('inventory_pre_normalize_20260531_110000.db', ts(1.5)),
    ]
    out = pb.select_for_deletion(primaries, NOW, keep_days=7)
    assert out == {
        'inventory_pre_normalize_20260531_100000.db',
        'inventory_pre_normalize_20260531_110000.db',
    }


def test_distinct_milestones_both_kept():
    # different labels (one has an extra token) must NOT collapse together
    primaries = [
        ('inventory-pre-mig088-2026-05-29-174943.db', ts(2)),
        ('inventory-pre-mig091-linecseq-2026-05-30-032143.db', ts(1)),
    ]
    out = pb.select_for_deletion(primaries, NOW, keep_days=7)
    assert out == set()


def test_junk_always_deleted_even_if_newest():
    primaries = [
        ('inventory_CORRUPTED_pre_recovery_20260530.db', ts(0)),  # newest but junk
        ('inventory-2026-05-30.db', ts(2)),
    ]
    out = pb.select_for_deletion(primaries, NOW, keep_days=7)
    assert 'inventory_CORRUPTED_pre_recovery_20260530.db' in out
    assert 'inventory-2026-05-30.db' not in out


def test_floor_keeps_newest_when_all_old():
    primaries = [
        ('inventory-2026-05-01.db', ts(40)),
        ('inventory-2026-05-10.db', ts(30)),   # newest -> floor keep
    ]
    out = pb.select_for_deletion(primaries, NOW, keep_days=7)
    assert out == {'inventory-2026-05-01.db'}


def test_floor_prefers_nonjunk():
    primaries = [
        ('inventory_dryrun-2026-05-10.db', ts(20)),       # newest but junk
        ('inventory-2026-05-01.db', ts(30)),              # only non-junk
    ]
    out = pb.select_for_deletion(primaries, NOW, keep_days=7)
    assert 'inventory_dryrun-2026-05-10.db' in out
    assert 'inventory-2026-05-01.db' not in out


# ── plan_dir (filesystem, sidecars, protection) ───────────────────────────────

def _touch(p: Path, mtime, size=16):
    p.write_bytes(b'x' * size)
    import os
    os.utime(p, (mtime, mtime))


def test_plan_dir_protects_live_db_and_tracked(tmp_path):
    live = tmp_path / 'inventory.db'
    _touch(live, ts(0))
    _touch(tmp_path / 'inventory.db-wal', ts(0))
    _touch(tmp_path / 'inventory.db-shm', ts(0))
    tracked_csv = tmp_path / 'material_values.csv'
    _touch(tracked_csv, ts(40))                      # old, but .csv -> protected
    _touch(tmp_path / 'inventory-2026-05-01.db', ts(40))   # old -> delete
    _touch(tmp_path / 'inventory-2026-05-31.db', ts(1))    # recent -> keep

    to_delete, to_keep = pb.plan_dir(tmp_path, NOW, keep_days=7,
                                     protected={'inventory.db'})
    del_names = {p.name for p in to_delete}
    keep_names = {p.name for p in to_keep}
    assert 'inventory-2026-05-01.db' in del_names
    assert {'inventory.db', 'inventory.db-wal', 'inventory.db-shm',
            'material_values.csv', 'inventory-2026-05-31.db'} <= keep_names
    assert not (del_names & {'inventory.db', 'inventory.db-wal',
                             'inventory.db-shm', 'material_values.csv'})


def test_plan_dir_sidecars_follow_primary_and_orphans(tmp_path):
    # kept primary -> its sidecar kept; deleted primary -> its sidecar deleted;
    # orphan sidecar (no primary) -> deleted
    _touch(tmp_path / 'inventory-2026-05-31.db', ts(1))
    _touch(tmp_path / 'inventory-2026-05-31.db-wal', ts(1))
    _touch(tmp_path / 'inventory-2026-05-01.db', ts(40))
    _touch(tmp_path / 'inventory-2026-05-01.db-shm', ts(40))
    _touch(tmp_path / 'inventory-ghost.db-wal', ts(40))   # orphan, no primary

    to_delete, to_keep = pb.plan_dir(tmp_path, NOW, keep_days=7,
                                     protected={'inventory.db'})
    del_names = {p.name for p in to_delete}
    keep_names = {p.name for p in to_keep}
    assert 'inventory-2026-05-31.db-wal' in keep_names
    assert 'inventory-2026-05-01.db' in del_names
    assert 'inventory-2026-05-01.db-shm' in del_names
    assert 'inventory-ghost.db-wal' in del_names
