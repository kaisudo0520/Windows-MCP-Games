"""Stop latch, operator emergency stop, and held-input release.

Two independent ways to stop:

* ``stop_input`` tool  -> latch origin "tool".  Cleared by ``resume_input``.
* Operator action      -> latch origin "operator". Triggered by pressing the
  emergency key (default Scroll Lock) or, if enabled, parking the physical
  cursor in the primary screen's top-left corner. An operator stop always
  overrides a tool stop, and by default it can only be cleared by restarting
  the server, so the AI cannot resume on its own.

Whenever the latch engages, every mouse button / key this process pressed is
released first. Upstream Windows-MCP has no equivalent fail-safe (it is not
pyautogui based), so this module is new rather than vendored.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.vendor import win32_input as w32

logger = logging.getLogger(__name__)

_EMERGENCY_KEYS = {
    "pause": w32.Keys.VK_PAUSE,
    "scrolllock": w32.Keys.VK_SCROLL,
    "f12": w32.Keys.VK_F12,
}


class HeldInputs:
    """Tracks buttons/keys this process pressed so they can always be released."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buttons: set[str] = set()
        self._keys: set[int] = set()

    def note_button_down(self, button: str) -> None:
        with self._lock:
            self._buttons.add(button)

    def note_button_up(self, button: str) -> None:
        with self._lock:
            self._buttons.discard(button)

    def note_key_down(self, vk: int) -> None:
        with self._lock:
            self._keys.add(vk)

    def note_key_up(self, vk: int) -> None:
        with self._lock:
            self._keys.discard(vk)

    def release_all(self) -> dict[str, list[Any]]:
        """Release everything still held. Never raises.

        An entry is dropped from the held set only after its release call
        succeeded, so a failed release stays visible in ``snapshot()`` and is
        retried by the next stop/shutdown.
        """
        with self._lock:
            buttons = sorted(self._buttons)
            keys = sorted(self._keys)
        released: dict[str, list[Any]] = {"buttons": [], "keys": [], "failed": []}
        for button in buttons:
            try:
                w32.ReleaseMouse(button, waitTime=0)
            except Exception:  # pragma: no cover - best effort
                logger.exception("release mouse %s failed", button)
                released["failed"].append(button)
                continue
            self.note_button_up(button)
            released["buttons"].append(button)
        for vk in keys:
            try:
                w32.ReleaseKey(vk, waitTime=0)
            except Exception:  # pragma: no cover
                logger.exception("release key %s failed", vk)
                released["failed"].append(vk)
                continue
            self.note_key_up(vk)
            released["keys"].append(vk)
        return released

    def snapshot(self) -> dict[str, list[Any]]:
        with self._lock:
            return {"buttons": sorted(self._buttons), "keys": sorted(self._keys)}


@dataclass(frozen=True)
class StopState:
    stopped: bool
    origin: str | None  # "tool" | "operator" | None
    reason: str | None
    since_unix: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "origin": self.origin,
            "reason": self.reason,
            "since_unix": self.since_unix,
        }


class StopLatch:
    def __init__(self, held: HeldInputs, settings: Settings) -> None:
        self._lock = threading.Lock()
        self._held = held
        self._settings = settings
        self._state = StopState(False, None, None, None)

    def state(self) -> StopState:
        with self._lock:
            return self._state

    def engage(self, origin: str, reason: str) -> StopState:
        released = self._held.release_all()
        with self._lock:
            # An operator stop must not be downgraded to a tool stop; a tool stop
            # is upgraded to an operator stop.
            if self._state.stopped and self._state.origin == "operator" and origin == "tool":
                return self._state
            self._state = StopState(True, origin, reason, time.time())
            state = self._state
        logger.warning("input STOPPED origin=%s reason=%s released=%s", origin, reason, released)
        return state

    def resume(self) -> StopState:
        with self._lock:
            state = self._state
            if not state.stopped:
                return state
            if state.origin == "operator" and not self._settings.allow_tool_resume_after_operator_stop:
                raise GameMcpError(
                    ErrorCode.STOPPED,
                    "Input was stopped by the operator. A tool call cannot clear that; restart the "
                    "MCP server (or set GAME_MCP_ALLOW_TOOL_RESUME_AFTER_OPERATOR_STOP=1 before start).",
                    state.to_dict(),
                )
            self._state = StopState(False, None, None, None)
            logger.warning("input RESUMED (previous stop origin=%s)", state.origin)
            return self._state

    def require_running(self) -> None:
        state = self.state()
        if state.stopped:
            raise GameMcpError(ErrorCode.STOPPED, "Input is stopped.", state.to_dict())


class OperatorWatcher(threading.Thread):
    """Polls the physical keyboard/cursor for the operator's emergency stop."""

    POLL_S = 0.03

    def __init__(self, latch: StopLatch, settings: Settings) -> None:
        super().__init__(name="operator-watcher", daemon=True)
        self._latch = latch
        self._settings = settings
        self._stop_event = threading.Event()
        if settings.emergency_key not in _EMERGENCY_KEYS:
            raise ValueError(
                f"GAME_MCP_EMERGENCY_KEY={settings.emergency_key!r} is not supported; "
                f"choose one of {sorted(_EMERGENCY_KEYS)}"
            )
        self.emergency_vk = _EMERGENCY_KEYS[settings.emergency_key]
        self.triggered_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    def describe(self) -> dict[str, Any]:
        return {
            "emergency_key": self._settings.emergency_key,
            "corner_failsafe": self._settings.corner_failsafe,
            "alive": self.is_alive(),
            "triggered_count": self.triggered_count,
        }

    def _check_once(self) -> str | None:
        # KeyWasPressed also catches a tap shorter than one poll interval.
        if w32.KeyWasPressed(int(self.emergency_vk)):
            return f"emergency key '{self._settings.emergency_key}' pressed"
        if self._settings.corner_failsafe:
            x, y = w32.GetCursorPos()
            if x == 0 and y == 0:
                return "cursor parked in top-left corner"
        return None

    def poll(self) -> bool:
        """One watcher iteration. Returns True if an operator stop was engaged.

        A pending tool stop does not mask the operator: the latch is upgraded
        to origin "operator" so that ``resume_input`` can no longer clear it.
        """
        reason = self._check_once()
        if reason is None:
            return False
        if self._latch.state().origin == "operator":
            return False
        self.triggered_count += 1
        self._latch.engage("operator", reason)
        return True

    def run(self) -> None:  # pragma: no cover - thread loop, exercised by integration tests
        while not self._stop_event.is_set():
            try:
                self.poll()
            except Exception:
                logger.exception("operator watcher poll failed")
            self._stop_event.wait(self.POLL_S)
