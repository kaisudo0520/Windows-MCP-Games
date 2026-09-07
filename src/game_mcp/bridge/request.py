"""Read-only ``STATE`` re-send requests between the MCP server and the dump.

The MCP server cannot talk to the dump directly (the dump's stdio belongs to the mod),
so it asks for a fresh push by creating ``state.request`` in the dump directory. A
daemon thread in the dump polls for that file, removes it, and writes ``STATE`` to the
mod, which then re-sends the current state. ``STATE`` is a query the mod answers by
re-sending; it never plays, ends, chooses or clicks (see ``protocol.ALLOWED_OUTBOUND``).

Why this exists: CommunicationMod pushes the first state of a combat before the
monsters' intents are created, so that push carries ``intent: "DEBUG"`` and nothing
re-pushes until the player acts. A ``STATE`` request after the intents exist fixes it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TextIO

from game_mcp.bridge import protocol

logger = logging.getLogger(__name__)

REQUEST_NAME = "state.request"
_POLL_S = 0.2
_MIN_INTERVAL_S = 0.25


def request_path(out_dir: str) -> str:
    return os.path.join(out_dir, REQUEST_NAME)


def place_request(out_dir: str) -> bool:
    """Create the request marker. Returns True on success (idempotent if it exists)."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(request_path(out_dir), "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
        return True
    except OSError as exc:
        logger.warning("could not place %s in %s: %s", REQUEST_NAME, out_dir, exc)
        return False


def clear_request(out_dir: str) -> bool:
    """Remove the request marker if present. Returns True if it was removed."""
    try:
        os.remove(request_path(out_dir))
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not remove %s: %s", request_path(out_dir), exc)
        return False


class RequestWatcher:
    """Daemon thread: when ``state.request`` appears, consume it and send ``STATE``.

    Only this thread writes to the mod after the handshake, so stdout writes are never
    interleaved. Requests closer together than ``min_interval_s`` are coalesced.
    """

    def __init__(self, out_dir: str, stdout: TextIO, *, poll_s: float = _POLL_S, min_interval_s: float = _MIN_INTERVAL_S) -> None:
        self._out_dir = out_dir
        self._stdout = stdout
        self._poll_s = poll_s
        self._min_interval_s = min_interval_s
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._run, name="state-request-watcher", daemon=True)
        self.sent = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._shutdown.set()
        self._thread.join(timeout_s)

    def _run(self) -> None:
        last_sent = 0.0
        while not self._shutdown.is_set():
            if clear_request(self._out_dir):
                wait = self._min_interval_s - (time.monotonic() - last_sent)
                if wait > 0:
                    time.sleep(wait)
                try:
                    protocol.write_line(self._stdout, "STATE")
                    self.sent += 1
                    last_sent = time.monotonic()
                    logger.info("sent STATE re-send request to the mod (#%d)", self.sent)
                except (OSError, ValueError) as exc:  # closed pipe: the mod is gone
                    logger.warning("could not send STATE: %s", exc)
                    return
            self._shutdown.wait(self._poll_s)
