"""Filesystem persistence for the state dump (all best-effort; a disk error is
logged, never fatal).

Writes two files into the output directory:

* ``latest.json``      -- ``{seq, received_wall, received_unix, state}``, the newest push.
* ``latest.meta.json`` -- ``{seq, received_wall, received_unix}``, so a reader can detect
  a change cheaply without parsing the whole state.

``latest.json`` is written first; ``latest.meta.json`` is written *only if that succeeds*,
so the meta seq never leads the state on disk and a reader keeps serving the previous good
state after a failed write. ``os.replace`` is retried on Windows because a reader that has
the file open (CPython ``open`` lacks ``FILE_SHARE_DELETE``) can briefly block the swap.
``--record-samples`` additionally writes each push to ``<wall>_<seq>.json``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any

from game_mcp.bridge.protocol import Snapshot

logger = logging.getLogger(__name__)

LATEST_NAME = "latest.json"
META_NAME = "latest.meta.json"
LOG_NAME = "bridge.log"
_TMP_PREFIX = ".dump-"
_TMP_SUFFIX = ".tmp"
_REPLACE_ATTEMPTS = 10
_REPLACE_BACKOFF_S = 0.015


def _local_appdata() -> str:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


def default_state_dir() -> str:
    return os.path.join(_local_appdata(), "sts-game-mcp", "state")


def meta_payload(snap: Snapshot) -> dict[str, Any]:
    return {"seq": snap.seq, "received_wall": snap.received_wall_iso, "received_unix": snap.received_unix}


def latest_payload(snap: Snapshot) -> dict[str, Any]:
    return {
        "seq": snap.seq,
        "received_wall": snap.received_wall_iso,
        "received_unix": snap.received_unix,
        "state": snap.state,
    }


def _replace_with_retry(tmp: str, path: str) -> bool:
    """os.replace(tmp, path), retried on Windows sharing/permission errors."""
    last_exc: OSError | None = None
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp, path)
            return True
        except OSError as exc:  # PermissionError (WinError 5) is an OSError subclass
            last_exc = exc
            if attempt < _REPLACE_ATTEMPTS:
                time.sleep(_REPLACE_BACKOFF_S)
    logger.warning("os.replace -> %s failed after %d attempts: %s", path, _REPLACE_ATTEMPTS, last_exc)
    return False


def atomic_write_json(path: str, payload: dict[str, Any]) -> bool:
    """Write ``payload`` as JSON to ``path`` atomically. Returns True on success.

    Writes a temp file in the same directory, fsyncs it, then ``os.replace``s it into
    place (with retry). The temp file is removed on any failure so no ``.tmp`` litter
    is left behind.
    """
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        logger.exception("could not create state directory %s", directory)
        return False
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        logger.exception("temp write failed for %s", path)
        _remove_quietly(tmp)
        return False
    if _replace_with_retry(tmp, path):
        return True
    _remove_quietly(tmp)
    return False


def write_latest(out_dir: str, snap: Snapshot) -> bool:
    """Write latest.json, then latest.meta.json only if the state write succeeded.

    Skipping the meta write on a failed state write keeps meta.seq from ever leading
    the state on disk. Returns True only if both files were written.
    """
    if not atomic_write_json(os.path.join(out_dir, LATEST_NAME), latest_payload(snap)):
        logger.warning("latest.json write failed for seq %d; not writing meta", snap.seq)
        return False
    return atomic_write_json(os.path.join(out_dir, META_NAME), meta_payload(snap))


def read_last_seq(out_dir: str) -> int:
    """Return the seq recorded in an existing latest.meta.json, else 0.

    Lets the dump continue the sequence across a process restart (the mod restarts
    the command when the game restarts) so waiters holding a high since_seq still
    resolve. Absent file -> 0 silently; present-but-unreadable -> 0 with a warning.
    """
    path = os.path.join(out_dir, META_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        logger.warning("existing %s is unreadable; restarting seq at 1", path)
        return 0
    seq = data.get("seq") if isinstance(data, dict) else None
    if isinstance(seq, int) and seq >= 0:
        return seq
    logger.warning("existing %s has no valid seq; restarting seq at 1", path)
    return 0


def sweep_temp_files(out_dir: str) -> int:
    """Remove stale ``.dump-*.tmp`` files (e.g. left by a hard kill). Returns the count."""
    removed = 0
    try:
        names = os.listdir(out_dir)
    except OSError:
        return 0
    for name in names:
        if name.startswith(_TMP_PREFIX) and name.endswith(_TMP_SUFFIX):
            if _remove_quietly(os.path.join(out_dir, name)):
                removed += 1
    if removed:
        logger.info("swept %d stale temp file(s) from %s", removed, out_dir)
    return removed


def _remove_quietly(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        logger.warning("could not remove temp file %s", path)
        return False


def _sample_files(directory: str) -> list[tuple[float, str]]:
    """(mtime, path) for every sample JSON in *directory*, oldest first."""
    entries: list[tuple[float, str]] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    entries.sort()
    return entries


def prune_samples(directory: str | None, *, keep_files: int, max_age_h: float, by_age: bool) -> int:
    """Delete sample files beyond the retention policy; returns the number removed.

    The newest ``keep_files`` are always kept; with ``by_age`` files older than
    ``max_age_h`` hours are deleted too. Best-effort: failures are logged, never raised.
    """
    if not directory or not os.path.isdir(directory):
        return 0
    entries = _sample_files(directory)
    doomed: list[str] = []
    if by_age:
        cutoff = time.time() - max_age_h * 3600
        doomed.extend(path for mtime, path in entries if mtime < cutoff)
    if len(entries) > keep_files:
        doomed.extend(path for _mtime, path in entries[: len(entries) - keep_files])
    removed = sum(1 for path in dict.fromkeys(doomed) if _remove_quietly(path))
    if removed:
        logger.info("pruned %d state sample(s) from %s", removed, directory)
    return removed


def _sample_stamp(iso_wall: str) -> str:
    """Filesystem-safe stamp from an ISO-8601 wall time (drop ':' and '+')."""
    return iso_wall.replace(":", "").replace("+", "p")


def record_sample(directory: str, snap: Snapshot) -> str | None:
    """Write one push to ``directory/<wall time>_<seq>.json``. Returns the path, or None."""
    try:
        os.makedirs(directory, exist_ok=True)
        name = f"{_sample_stamp(snap.received_wall_iso)}_{snap.seq:06d}.json"
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(latest_payload(snap), fh, ensure_ascii=False, indent=2)
        return path
    except OSError:
        logger.exception("could not write state sample for seq %d", snap.seq)
        return None
