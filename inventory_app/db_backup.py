"""Auto-snapshot the SQLite DB before imports, with restore + retention.

Why: imports (especially the staff-run ones on /import-data and /marketplace)
write straight to prod. A wrong file can corrupt the ledger/stock. We snapshot
the whole DB right before each import commits, so an admin can roll the entire
DB back to the last good point. Snapshots are gzipped SQLite ``.backup()`` copies
(the online-backup API — captures WAL pages a bare file copy would miss) kept on
the persistent volume; old ones are pruned automatically.

Pure module: every function takes an explicit ``db_path`` / ``backup_dir`` so it
is unit-testable without the app. Callers in app.py pass ``config.DATABASE_PATH``
and ``default_backup_dir(config.DATABASE_PATH)`` (= ``<volume>/backups``).

Filename: ``auto-<reason>-<YYYYMMDD_HHMMSS>.db.gz`` (reason ∈ unified / weekly /
marketplace / pre-restore). Foreign files in the dir are never touched.
"""
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

PREFIX = "auto-"
SUFFIX = ".db.gz"
# reason is [a-z0-9_-]+ (no '/' or '.', so a name can't path-traverse); '_'
# added to allow reasons like 'marketplace_upload'. The trailing
# <8digits>_<6digits> is the timestamp.
_NAME_RE = re.compile(r"^auto-(?P<reason>[a-z0-9_-]+)-(?P<ts>\d{8}_\d{6})\.db\.gz$")

DEFAULT_KEEP_DAYS = 7
# Hard count cap (the live driver of disk use). 434MB Railway volume; the live DB
# has grown to ~174MB and each gzipped snapshot is ~20MB, so keep only the newest
# 2 (~40MB) → DB + backups ≈ 49% of the volume, leaving headroom for the restore
# temp and a full-replace upload stash. This is a LEAN rolling safety net: just
# the immediate rollback point + one prior. The deep backup history lives
# off-volume on the owner's machine (scripts/backup_prod_kit.sh pulls verified,
# WAL-safe kits to ~/sendy-prod-backups/), so prod doesn't carry it. Raise if the
# volume grows.
DEFAULT_MAX_KEEP = 2
# Never write a snapshot when the volume is this close to full — a backup must
# not be the thing that fills the disk and breaks the app.
MIN_FREE_BYTES = 60 * 1024 * 1024


def default_backup_dir(db_path):
    """Backups live next to the live DB → on Railway that's the persistent
    volume (DATA_DIR), so they survive redeploys."""
    return os.path.join(os.path.dirname(db_path), "backups")


def disk_usage_mb(path):
    """(total, used, free) in MB for the filesystem holding ``path``."""
    u = shutil.disk_usage(path)
    mb = 1024 * 1024
    return {"total": u.total // mb, "used": u.used // mb, "free": u.free // mb}


def create_backup(reason, *, db_path, backup_dir,
                  keep_days=DEFAULT_KEEP_DAYS, max_keep=DEFAULT_MAX_KEEP,
                  now=None):
    """Snapshot ``db_path`` → ``backup_dir/auto-<reason>-<ts>.db.gz`` (gzipped
    online backup), then prune. Returns an info dict, or None if there is no DB
    to back up (so an import can still proceed on a fresh install)."""
    if not os.path.exists(db_path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    # Reclaim space from old snapshots first, then refuse if the volume is still
    # too full (caught by the caller → warned, import proceeds without a point).
    prune_backups(backup_dir=backup_dir, keep_days=keep_days,
                  max_keep=max_keep, now=now)
    free = shutil.disk_usage(backup_dir).free
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"พื้นที่ดิสก์เหลือน้อย ({free // (1024 * 1024)}MB) — ข้ามการสำรอง")

    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    name = f"{PREFIX}{reason}-{ts}{SUFFIX}"
    final = os.path.join(backup_dir, name)

    # The uncompressed .backup temp (~size of the live DB) goes to the system
    # temp dir — NOT the small data volume — so only the gzip lands on the volume.
    fd, tmp_db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Compress to a unique .part temp INSIDE backup_dir, then atomically publish
    # to `final`. A crash/kill/IOError mid-write leaves only a .part — which never
    # matches _NAME_RE, so list_backups/prune ignore it — never a corrupt snapshot
    # that counts toward max_keep, is downloadable, or could be restored.
    pfd, part = tempfile.mkstemp(suffix=SUFFIX + ".part", dir=backup_dir)
    os.close(pfd)
    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)          # online backup — includes WAL pages
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp_db, "rb") as fi, gzip.open(part, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        os.replace(part, final)          # atomic publish on the same filesystem
        part = None
    finally:
        for leftover in (tmp_db, part):
            if leftover and os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    info = {"path": final, "name": name, "reason": reason,
            "size": os.path.getsize(final)}
    prune_backups(backup_dir=backup_dir, keep_days=keep_days,
                  max_keep=max_keep, now=now)   # enforce cap incl. the new one
    return info


def safe_create_backup(reason, *, db_path, backup_dir, **kw):
    """create_backup that never raises — returns (info_or_None, error_or_None).
    For call sites where a backup-infra failure must not abort the import."""
    try:
        return create_backup(reason, db_path=db_path, backup_dir=backup_dir, **kw), None
    except Exception as e:                      # disk full, permission, etc.
        return None, str(e)


def list_backups(*, backup_dir):
    """All auto-* snapshots, newest first, with reason / created_at / size."""
    if not os.path.isdir(backup_dir):
        return []
    out = []
    for fn in os.listdir(backup_dir):
        m = _NAME_RE.match(fn)
        if not m:
            continue
        try:
            created = datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        path = os.path.join(backup_dir, fn)
        out.append({"name": fn, "path": path, "reason": m.group("reason"),
                    "created_at": created, "size": os.path.getsize(path)})
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out


def prune_backups(*, backup_dir, keep_days=DEFAULT_KEEP_DAYS,
                  max_keep=DEFAULT_MAX_KEEP, now=None):
    """Delete auto-* snapshots older than ``keep_days`` OR beyond the newest
    ``max_keep`` (whichever applies). Always keeps the single newest. Never
    touches the live DB or any non-auto file. Returns the deleted names."""
    now = now or datetime.now()
    backups = list_backups(backup_dir=backup_dir)   # newest first
    deleted = []
    for rank, b in enumerate(backups):
        if rank == 0:
            continue                                # floor: keep newest
        age_days = (now - b["created_at"]).total_seconds() / 86400.0
        if age_days > keep_days or rank >= max_keep:
            try:
                os.remove(b["path"])
                deleted.append(b["name"])
            except OSError:
                pass
    return deleted


# Held full-replace upload stashes written by /admin/upload-db onto the volume
# (pending-<ts>.db). Same digit-only-timestamp shape as the backup names → a
# foreign or path-traversing name can never match.
_PENDING_RE = re.compile(r"^pending-\d{8}_\d{6}\.db$")


def sweep_pending_uploads(pending_dir, *, keep_name=None):
    """Delete stale held upload stashes in ``pending_dir`` (the uncompressed
    'pending-<ts>.db' copies the full-replace warning step writes to the volume).
    These linger when an upload is abandoned and have filled the volume before.
    Keeps ``keep_name`` (the in-flight stash) if given. Only ever removes files
    matching ``_PENDING_RE`` — never the live DB, backups, or anything else.
    Returns (deleted_names, freed_bytes)."""
    if not os.path.isdir(pending_dir):
        return [], 0
    deleted, freed = [], 0
    for fn in os.listdir(pending_dir):
        if not _PENDING_RE.match(fn) or fn == keep_name:
            continue
        p = os.path.join(pending_dir, fn)
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            deleted.append(fn)
            freed += sz
        except OSError:
            pass
    return deleted, freed


def delete_backup(name, *, backup_dir):
    """Delete one auto-* snapshot by name (manual cleanup from /admin/backups).
    Path-safe like restore_backup: ``name`` must match the snapshot pattern and
    resolve to a file directly inside ``backup_dir`` (no '/', no '..' escape).
    Returns freed bytes. Raises ValueError on a bad / escaping / missing name."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid backup name: {name!r}")
    path = os.path.join(backup_dir, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(backup_dir):
        raise ValueError("backup path escapes backup_dir")
    if not os.path.exists(path):
        raise ValueError(f"backup not found: {name}")
    sz = os.path.getsize(path)
    os.remove(path)
    return sz


def _remove_sidecars(db_path):
    """Delete the WAL/SHM sidecar files for ``db_path`` (if present)."""
    for side in ("-wal", "-shm"):
        p = db_path + side
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def restore_backup(name, *, db_path, backup_dir):
    """Replace the live DB with snapshot ``name``. Snapshots the current state
    first (reason='pre-restore') so a wrong restore is itself reversible, then
    atomically swaps the file in and clears stale -wal/-shm sidecars.

    ⚠ Reverts the WHOLE DB to that snapshot — not just one import. Caller must
    restrict this to admins and should restart workers afterward."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid backup name: {name!r}")
    src_gz = os.path.join(backup_dir, name)
    # belt-and-braces: the resolved path must stay directly inside backup_dir
    if os.path.dirname(os.path.abspath(src_gz)) != os.path.abspath(backup_dir):
        raise ValueError("backup path escapes backup_dir")
    if not os.path.exists(src_gz):
        raise ValueError(f"backup not found: {name}")

    dst_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    # restore decompresses to a full-size temp next to the live DB (needed for an
    # atomic swap) — make sure the volume can hold it before we start.
    if os.path.exists(db_path):
        needed = os.path.getsize(db_path) + 40 * 1024 * 1024
        if shutil.disk_usage(dst_dir).free < needed:
            raise RuntimeError(
                "พื้นที่ดิสก์ไม่พอสำหรับการกู้คืน — ต้องการพื้นที่ชั่วคราวเท่าขนาดฐานข้อมูล")
        create_backup("pre-restore", db_path=db_path, backup_dir=backup_dir)

    fd, tmp_db = tempfile.mkstemp(suffix=".db", dir=dst_dir)
    os.close(fd)
    try:
        with gzip.open(src_gz, "rb") as fi, open(tmp_db, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        probe = sqlite3.connect(tmp_db)            # confirm it's a usable DB
        try:
            probe.execute("PRAGMA schema_version")
        finally:
            probe.close()
        # Clear the OLD db's WAL/SHM sidecars BEFORE the swap. Under gunicorn -w 2
        # a fresh connection arriving between the swap and a later unlink would
        # pair the NEW (restored) main file with the stale OLD -wal still at the
        # path and silently re-apply pre-restore frames over the restore
        # (integrity_check still returns 'ok'). Removing them first closes that hole.
        _remove_sidecars(db_path)
        os.replace(tmp_db, db_path)                # atomic on same filesystem
        tmp_db = None
    finally:
        if tmp_db and os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass

    _remove_sidecars(db_path)                      # belt-and-braces after the swap
    return {"restored": name}
