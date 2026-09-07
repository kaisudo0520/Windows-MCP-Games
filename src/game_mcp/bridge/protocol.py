"""Outbound allowlist, inbound line parsing, and the immutable snapshot store.

The one safety property of the dump lives here: the process may write exactly two
strings to the mod's stdout, ``ready`` (the startup handshake) and ``STATE`` (a query
that makes the mod re-send the current state). Every stdout write is routed through
:func:`write_line`, which raises ``RuntimeError`` for anything else. Advisor mode
never sends an action command (``PLAY``/``END``/``CHOOSE``/``CLICK``/``KEY``/...), so
nothing this process writes can drive the game.
"""

from __future__ import annotations

import json
import locale
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TextIO

logger = logging.getLogger(__name__)

#: The complete set of strings the dump may ever send to the mod: the ``ready``
#: handshake and the ``STATE`` query (the mod answers by re-sending the current state;
#: it does not play, end, choose or click). No action command is ever allowed. This is
#: the single choke point that keeps the dump read-only.
ALLOWED_OUTBOUND = frozenset({"ready", "STATE"})


def write_line(stream: TextIO, line: str) -> None:
    """Write one protocol line to the mod, plus a newline, and flush.

    Raises ``RuntimeError`` for any line not on :data:`ALLOWED_OUTBOUND`.
    """
    if line not in ALLOWED_OUTBOUND:
        raise RuntimeError(
            f"Refusing to write non-allowlisted line to the game: {line!r}. "
            f"Advisor mode is read-only; the only allowed outbound lines are {sorted(ALLOWED_OUTBOUND)}."
        )
    stream.write(line + "\n")
    stream.flush()


class LineDecoder:
    """Decode raw stdin bytes from the mod without ever raising.

    CommunicationMod writes with the game JVM's default charset (``getBytes()`` with no
    charset), which may be UTF-8 or the Windows locale code page (cp950 on a zh-TW box).
    Auto mode tries strict UTF-8 first and falls back to the locale code page with
    replacement characters; a forced encoding is used with replacement characters. The
    fallback is logged once at WARNING so a charset mismatch is visible in ``bridge.log``.
    """

    def __init__(self, forced: str | None = None) -> None:
        self._forced = forced
        self._fallback = locale.getpreferredencoding(False) or "utf-8"
        self._warned = False

    @property
    def fallback(self) -> str:
        return self._fallback

    def decode(self, raw: bytes | str) -> str:
        if isinstance(raw, str):
            return raw
        if self._forced:
            return raw.decode(self._forced, errors="replace")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return self._decode_lenient(raw, exc)

    def _decode_lenient(self, raw: bytes, exc: UnicodeDecodeError) -> str:
        """Pick whichever of lenient UTF-8 / locale codec loses the fewest characters.

        A single stray byte in an otherwise UTF-8 line must not turn every CJK name into
        U+FFFD (which is what a whole-line locale fallback does), and a genuinely
        locale-encoded line must not be mangled by lenient UTF-8.
        """
        utf8 = raw.decode("utf-8", errors="replace")
        local = raw.decode(self._fallback, errors="replace")
        chosen, codec = (utf8, "utf-8") if utf8.count("\ufffd") <= local.count("\ufffd") else (local, self._fallback)
        if not self._warned:
            self._warned = True
            logger.warning(
                "stdin line is not valid UTF-8 (%s); decoded with %s (replacing bad bytes; utf-8 lost %d, %s lost %d). "
                "Set --stdin-encoding to match the game JVM charset. Bytes around the error (hex): %s",
                exc,
                codec,
                utf8.count("\ufffd"),
                self._fallback,
                local.count("\ufffd"),
                _hex_around(raw, exc.start),
            )
        return chosen


def _hex_around(raw: bytes, index: int, span: int = 24) -> str:
    """Hex of the bytes around ``index`` (the first undecodable byte), for diagnosis."""
    lo = max(0, index - span)
    return f"offset {index}: " + raw[lo:index + span].hex(" ")


def parse_state_line(line: str) -> dict[str, Any] | None:
    """Parse one pushed line as a JSON object.

    Returns the object, or ``None`` (logged, never raised) when the line is blank,
    not valid JSON, or not a JSON object. Malformed lines are skipped so the dump
    never crashes on unexpected input.
    """
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError as exc:  # json.JSONDecodeError is a subclass of ValueError
        logger.warning("skipping malformed JSON line (%d bytes): %s", len(text), exc)
        return None
    if not isinstance(obj, dict):
        logger.warning("skipping non-object JSON push of type %s", type(obj).__name__)
        return None
    return obj


@dataclass(frozen=True)
class Snapshot:
    """One state push from the mod. Immutable; the store replaces, never mutates."""

    seq: int
    received_unix: float
    received_wall_iso: str
    state: dict[str, Any]


class SnapshotStore:
    """Holds the latest snapshot. ``record`` bumps ``seq`` monotonically from ``start_seq + 1``.

    ``start_seq`` lets a restarted dump continue the sequence left on disk, so a reader
    waiting on a high ``since_seq`` still resolves after the game (and the dump) restarts.
    """

    def __init__(self, start_seq: int = 0) -> None:
        if start_seq < 0:
            raise ValueError("start_seq must be >= 0")
        self._lock = threading.Lock()
        self._latest: Snapshot | None = None
        self._seq = start_seq

    def record(self, state: dict[str, Any]) -> Snapshot:
        with self._lock:
            self._seq += 1
            now = time.time()
            snap = Snapshot(
                seq=self._seq,
                received_unix=now,
                received_wall_iso=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                state=state,
            )
            self._latest = snap
            return snap

    def latest(self) -> Snapshot | None:
        with self._lock:
            return self._latest

    def current_seq(self) -> int:
        with self._lock:
            return self._seq
