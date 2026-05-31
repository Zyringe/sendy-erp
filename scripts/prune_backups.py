#!/usr/bin/env python3
"""Prune Sendy DB backups so they stop eating disk.

Retention (Rule A):
  * keep every backup from the last ``--keep-days`` days (default 7); delete older
  * within the window, collapse each milestone *family* (the filename with digit
    runs stripped) to its single newest copy. Daily snapshots
    (``inventory-YYYY-MM-DD.db``) are kept individually and never collapsed.
  * always delete junk markers: CORRUPTED / dryrun / partial_recovery
  * floor: never delete the single newest backup in a directory
  * NEVER touch the live ``inventory.db`` (+ its -wal/-shm) or any git-tracked file
  * ``-wal`` / ``-shm`` sidecars follow their primary: kept if the primary is kept,
    deleted if the primary is deleted or missing (orphan)

Dry-run by default. Pass --apply to actually delete.

  python scripts/prune_backups.py                # preview both backup dirs
  python scripts/prune_backups.py --apply        # delete per policy
  python scripts/prune_backups.py --keep-days 14 # more conservative
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DAY = 86400
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = [
    REPO_ROOT / 'data' / 'backups',
    REPO_ROOT / 'inventory_app' / 'instance',
]
LIVE_DB = 'inventory.db'  # the working database in instance/ — never a candidate

JUNK_RE = re.compile(r'CORRUPTED|dryrun|partial_recovery', re.I)
DAILY_RE = re.compile(r'^inventory-\d{4}-\d{2}-\d{2}\.db$')
SIDECAR_RE = re.compile(r'-(wal|shm)$')


# ── pure helpers ──────────────────────────────────────────────────────────────

def is_sidecar(name):
    return bool(SIDECAR_RE.search(name))


def primary_of(name):
    return SIDECAR_RE.sub('', name)


def is_daily(name):
    return bool(DAILY_RE.match(name))


def family_label(name):
    """Digit runs collapsed to '#', so same-operation snapshots share a label."""
    return re.sub(r'\d+', '#', name)


def select_for_deletion(primaries, now_ts, keep_days=7):
    """Decide which *primary* backups (non-sidecar) to delete.

    ``primaries`` is an iterable of ``(name, mtime_ts)``. Returns a set of names.
    """
    items = list(primaries)
    cutoff = now_ts - keep_days * DAY
    to_delete = set()
    recent = []
    for name, mt in items:
        if mt < cutoff:
            to_delete.add(name)
        else:
            recent.append((name, mt))

    kept = set()
    groups = {}
    for name, mt in recent:
        if JUNK_RE.search(name):
            to_delete.add(name)
        elif is_daily(name):
            kept.add(name)
        else:
            groups.setdefault(family_label(name), []).append((name, mt))

    for fs in groups.values():
        fs.sort(key=lambda x: x[1], reverse=True)
        kept.add(fs[0][0])
        for name, _ in fs[1:]:
            to_delete.add(name)

    # floor: keep the single newest backup even if everything is old/redundant
    if not kept and items:
        nonjunk = [it for it in items if not JUNK_RE.search(it[0])]
        pool = nonjunk or items
        newest = max(pool, key=lambda x: x[1])[0]
        kept.add(newest)
        to_delete.discard(newest)

    return to_delete


# ── filesystem planning ───────────────────────────────────────────────────────

def git_tracked_names(directory):
    """Basenames of git-tracked files in ``directory`` (best-effort)."""
    try:
        out = subprocess.run(
            ['git', 'ls-files', str(directory)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return set()
    return {os.path.basename(line) for line in out.splitlines() if line.strip()}


def plan_dir(directory, now_ts, keep_days=7, protected=()):
    """Return ``(to_delete, to_keep)`` lists of Paths for one directory."""
    directory = Path(directory)
    protected = set(protected) | git_tracked_names(directory)
    files = [f for f in directory.iterdir() if f.is_file()]

    primaries, sidecars = [], []
    for f in files:
        if f.name in protected or f.suffix == '.csv':
            continue
        (sidecars if is_sidecar(f.name) else primaries).append(f)

    del_names = select_for_deletion(
        [(f.name, f.stat().st_mtime) for f in primaries], now_ts, keep_days)
    keep_names = {f.name for f in primaries if f.name not in del_names}

    to_delete = [f for f in primaries if f.name in del_names]
    for s in sidecars:
        parent = primary_of(s.name)
        if parent in protected or parent in keep_names:
            continue           # sidecar of the live DB or a kept backup -> keep
        to_delete.append(s)    # primary deleted or orphan -> delete

    deleting = set(to_delete)
    to_keep = [f for f in files if f not in deleting]
    return to_delete, to_keep


# ── CLI ───────────────────────────────────────────────────────────────────────

def _mb(paths):
    return sum(p.stat().st_size for p in paths) / 1048576


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='actually delete (default: dry-run preview)')
    ap.add_argument('--keep-days', type=int, default=7,
                    help='keep backups newer than this many days (default 7)')
    ap.add_argument('dirs', nargs='*', type=Path, default=None,
                    help='backup directories (default: data/backups + instance)')
    args = ap.parse_args(argv)

    dirs = args.dirs if args.dirs else DEFAULT_DIRS
    now_ts = time.time()
    grand_del = []

    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            print('skip (not a dir): %s' % d)
            continue
        to_delete, to_keep = plan_dir(d, now_ts, keep_days=args.keep_days,
                                      protected={LIVE_DB})
        grand_del += to_delete
        print('\n=== %s ===' % d)
        print('  KEEP   : %3d files, %8.1f MB' % (len(to_keep), _mb(to_keep)))
        print('  DELETE : %3d files, %8.1f MB' % (len(to_delete), _mb(to_delete)))
        for f in sorted(to_delete, key=lambda p: p.name)[:8]:
            print('     - %s' % f.name)
        if len(to_delete) > 8:
            print('     ... and %d more' % (len(to_delete) - 8))

    print('\nTOTAL delete: %d files, %.2f GB' % (
        len(grand_del), sum(p.stat().st_size for p in grand_del) / 1073741824))

    if not args.apply:
        print('\n(dry-run — nothing deleted. Re-run with --apply to delete.)')
        return 0

    freed = sum(p.stat().st_size for p in grand_del)
    for p in grand_del:
        p.unlink()
    print('\nDeleted %d files, freed %.2f GB.' % (len(grand_del), freed / 1073741824))
    return 0


if __name__ == '__main__':
    sys.exit(main())
