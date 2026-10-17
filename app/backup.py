"""app/backup.py

Snapshot the SQLite database to a Hugging Face dataset repo, and restore it
before boot when the server starts with an empty database.

Why: on ephemeral hosting (e.g. Render free tier) the container filesystem is
wiped on every restart/sleep. This module keeps data durable by:

* Restore (once, at startup): if the configured DB file is missing or empty
  (no ``users`` table / no users), download ``latest.snapshot.sqlite.gz``
  from the backup repo and drop it in place — before any engine opens it.
* Backup (periodically, in-process): write a consistent snapshot with
  SQLite's ``VACUUM INTO``, gzip it, upload it to the snapshot repo, refresh
  the ``LATEST`` marker, and prune old timestamped snapshots.

Failure policy: backups are a durability aid, never operational-critical.
No upload failure may propagate: everything is logged and retried on the
next tick. Restore failure at boot is also non-fatal (the app boots on an
empty DB exactly like a fresh deployment).

Configuration (env):
* ``HF_TOKEN``             - a write token for the backup repo (required)
* ``BACKUP_REPO_ID``       - e.g. ``youruser/solar-automation-backup`` (required,
                             created as a *private dataset* repo on demand)
* ``BACKUP_INTERVAL_SECONDS``  - upload period (default 600)
* ``BACKUP_KEEP``          - how many old timestamped snapshots to keep (default 24)

When ``HF_TOKEN`` or ``BACKUP_REPO_ID`` is unset the feature is fully off and
behaviour is unchanged (fresh deployments with no backup stay fresh).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import io
import logging
import os
import sqlite3
import tempfile
from typing import Any, Callable

log = logging.getLogger(__name__)

SNAPSHOT_NAME = "latest.snapshot.sqlite.gz"
MARKER_NAME = "LATEST"


def is_configured() -> bool:
    """Backup/restore is enabled only when both env vars are present."""
    return bool(os.environ.get("HF_TOKEN")) and bool(os.environ.get("BACKUP_REPO_ID"))


def make_api(token: str | None = None) -> Any:
    """Build the HfApi client (injectable in tests)."""
    from huggingface_hub import HfApi

    return HfApi(token=token or os.environ["HF_TOKEN"])


def repo_id() -> str:
    assert os.environ.get("BACKUP_REPO_ID")
    return os.environ["BACKUP_REPO_ID"]


# ---------------------------------------------------------------------------
# Snapshot creation / restore (sqlite file level)
# ---------------------------------------------------------------------------

def snapshot_bytes(engine: Any) -> bytes:
    """A consistent, gzipped snapshot of the whole database file.

    Uses ``VACUUM INTO`` so the copy is transactionally consistent even while
    the app keeps serving (WAL-safe, reader-friendly).
    """
    url = engine.url.render_as_string(hide_password=False)
    if not url.startswith("sqlite"):
        raise RuntimeError("Backups are implemented for SQLite databases only.")
    db_file = str(engine.url.database)
    fd, tmp = tempfile.mkstemp(prefix="aha-snap-", suffix=".sqlite")
    os.close(fd)
    try:
        os.unlink(tmp)  # VACUUM INTO requires the target NOT to exist
        con = sqlite3.connect(db_file)
        try:
            con.execute("VACUUM INTO ?", (tmp,))
        finally:
            con.close()
        with open(tmp, "rb") as f:
            return gzip.compress(f.read(), mtime=0)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def db_looks_empty(db_file: str) -> bool:
    """True when the file is absent, or has no users / no users rows."""
    if not os.path.exists(db_file) or os.path.getsize(db_file) == 0:
        return True
    con = sqlite3.connect(db_file)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "users" not in tables:
            return True
        return (con.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 0) == 0
    except sqlite3.Error:
        return True
    finally:
        con.close()


def restore_from_bytes(data: bytes, db_file: str) -> None:
    """Atomically install a snapshot file at ``db_file`` (caller decided when)."""
    os.makedirs(os.path.dirname(os.path.abspath(db_file)) or ".", exist_ok=True)
    tmp = db_file + ".restore.tmp"
    with open(tmp, "wb") as f:
        f.write(gzip.decompress(data))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, db_file)


def database_file(engine: Any) -> str:
    db_file = str(engine.url.database)
    if engine.url.database is None:
        raise RuntimeError("In-memory SQLite cannot be backed up.")
    return db_file


# ---------------------------------------------------------------------------
# Restore-on-boot
# ---------------------------------------------------------------------------

def restore_if_empty(engine: Any) -> bool:
    """Download and install the latest snapshot when the current DB is empty.

    Never raises: any download problem is logged and boot continues on an
    empty database. Returns True when a restore actually happened.
    """
    if not is_configured():
        return False
    db_file = database_file(engine)
    if not db_looks_empty(db_file):
        return False
    try:
        api = make_api()
        try:
            marker = api.hf_hub_download(
                repo_id=repo_id(), repo_type="dataset", filename=MARKER_NAME
            )
            name = open(marker).read().strip()
        except Exception:  # noqa: BLE001 - no marker yet = first ever deploy
            log.info("backup: no LATEST marker in %s; booting on an empty DB", repo_id())
            return False
        if not name:
            return False
        path = api.hf_hub_download(
            repo_id=repo_id(), repo_type="dataset", filename=name
        )
        with open(path, "rb") as f:
            restore_from_bytes(f.read(), db_file)
        log.info("backup: restored %s from %s", db_file, name)
        return True
    except Exception:  # noqa: BLE001 - availability must not block boot
        log.exception("backup: restore failed; booting on an empty DB")
        return False


# ---------------------------------------------------------------------------
# Periodic upload
# ---------------------------------------------------------------------------

def upload_once(engine: Any, *, keep: int = 24) -> str:
    """Snapshot + upload now. Returns the uploaded filename.

    Raises on failure; callers (the loop) are responsible for swallowing it.
    """
    api = make_api()
    rid = repo_id()
    try:
        api.create_repo(repo_id=rid, repo_type="dataset", private=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 - may be a 409/no-perm variant
        log.debug("backup: create_repo said %r (continuing if it already exists)", exc)

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"snapshots/{ts}.sqlite.gz"
    data = snapshot_bytes(engine)
    api.upload_file(
        path_or_fileobj=io.BytesIO(data), path_in_repo=name,
        repo_id=rid, repo_type="dataset",
        commit_message="Automated database snapshot",
    )
    api.upload_file(
        path_or_fileobj=io.BytesIO(data), path_in_repo=SNAPSHOT_NAME,
        repo_id=rid, repo_type="dataset",
        commit_message="Automated database snapshot (latest)",
    )
    api.upload_file(
        path_or_fileobj=io.BytesIO(name.encode()), path_in_repo=MARKER_NAME,
        repo_id=rid, repo_type="dataset",
        commit_message="Point LATEST at newest snapshot",
    )
    _prune(api, rid, keep)
    return name


def _prune(api: Any, rid: str, keep: int) -> None:
    """Delete old timestamped snapshot files beyond ``keep``, oldest first."""
    try:
        files = sorted(
            f for f in api.list_repo_files(repo_id=rid, repo_type="dataset")
            if f.startswith("snapshots/") and f.endswith(".sqlite.gz")
        )
    except Exception:  # noqa: BLE001
        log.exception("backup: pruning (listing) failed")
        return
    for old in files[: max(0, len(files) - keep)]:
        try:
            api.delete_file(
                path_in_repo=old, repo_id=rid, repo_type="dataset",
                commit_message="Automated snapshot prune",
            )
        except Exception:  # noqa: BLE001
            log.exception("backup: could not delete %s", old)


async def backup_loop(
    engine: Any,
    *,
    interval: float = 600.0,
    keep: int = 24,
    run_upload: Callable[..., Any] | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Upload a snapshot every ``interval`` seconds until cancelled.

    Never raises; every tick is wrapped so a failure just logs.
    """
    do_upload = run_upload or upload_once
    # Baseline copy at start (data already restored / manually created).
    await _tick(engine, do_upload, keep)
    while True:
        await sleep(interval)
        await _tick(engine, do_upload, keep)


async def _tick(engine: Any, do_upload: Callable[..., Any], keep: int) -> None:
    try:
        name = await asyncio.to_thread(do_upload, engine, keep=keep)
        log.info("backup: uploaded %s", name)
    except Exception as exc:  # noqa: BLE001 - retried next tick
        log.warning("backup: upload failed this tick (%r)", exc)


__all__ = [
    "SNAPSHOT_NAME", "MARKER_NAME", "is_configured", "make_api", "repo_id",
    "snapshot_bytes", "db_looks_empty", "restore_from_bytes", "database_file",
    "restore_if_empty", "upload_once", "backup_loop",
]
