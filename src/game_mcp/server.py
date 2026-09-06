"""FastMCP server exposing exactly nine game-scoped tools.

Nothing else is registered: no shell, filesystem, registry, clipboard,
process, browser or network tool exists in this process.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from game_mcp import __version__, win32
from game_mcp.binding import Binding, bind, list_candidates
from game_mcp.capture import capture_game_screen, prune_debug_dir
from game_mcp.config import Settings, load_settings
from game_mcp.errors import ErrorCode, GameMcpError, error_response
from game_mcp.input_service import InputService
from game_mcp.observation import ObservationStore
from game_mcp.safety import HeldInputs, OperatorWatcher, StopLatch
from game_mcp.validation import validate_window

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
This server drives ONE bound Slay the Spire window. Workflow:
1. list_game_windows -> bind_game (pass hwnd if more than one candidate).
2. get_game_screen -> returns the client-area image plus an observation_id.
3. Exactly one action (click / move_mouse / press_key) referencing that
   observation_id. The id is single-use; take a new screen before acting again.
Coordinates are pixels of the returned image, origin top-left.
The server never steals focus, never launches apps, and refuses to act when the
game is minimized, not in the foreground, or covered by another window.
Only whitelisted keys can be pressed. stop_input halts all input until
resume_input; a stop triggered by the human operator (Scroll Lock key or cursor
in the top-left corner) cannot be cleared by a tool call. After every action,
read post_action.trustworthy: if false, the input may have gone elsewhere.
"""


class GameSession:
    """All mutable server state, guarded for the threaded FastMCP tool runner."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.store = ObservationStore()
        self.held = HeldInputs()
        self.latch = StopLatch(self.held, self.settings)
        #: One lock for capture, bind and input so an observation can never be
        #: issued for pixels grabbed while an action was in flight.
        self.op_lock = threading.Lock()
        self.inputs = InputService(self.settings, self.store, self.held, self.latch, self.op_lock)
        self.watcher: OperatorWatcher | None = None
        self._binding_lock = threading.Lock()
        self._binding: Binding | None = None
        self.started_at = time.time()
        self.dpi_mode = win32.ensure_dpi_awareness()
        # Fixed cleanup point: stale debug images are removed every time the server starts.
        self.debug_pruned_at_start = prune_debug_dir(self.settings, by_age=True, by_count=True)

    @property
    def binding(self) -> Binding | None:
        with self._binding_lock:
            return self._binding

    @binding.setter
    def binding(self, value: Binding | None) -> None:
        with self._binding_lock:
            self._binding = value

    def start_watcher(self) -> None:
        if self.watcher is None:
            self.watcher = OperatorWatcher(self.latch, self.settings)  # raises on a bad key name
            self.watcher.start()

    def run_exclusive(self, fn):
        """Run *fn* under the shared operation lock, or raise BUSY without waiting."""
        if not self.op_lock.acquire(blocking=False):
            raise GameMcpError(ErrorCode.BUSY, "Another capture or input action is still running.")
        try:
            return fn()
        finally:
            self.op_lock.release()

    def shutdown(self) -> None:
        released = self.held.release_all()
        if released["buttons"] or released["keys"]:
            logger.warning("released held inputs on shutdown: %s", released)
        if self.watcher is not None:
            self.watcher.stop()


def _ro(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _rw(title: str, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=False, idempotentHint=idempotent, openWorldHint=False)


def build_server(session: GameSession | None = None) -> tuple[FastMCP, GameSession]:
    session = session or GameSession()
    mcp = FastMCP(name="sts-game-mcp", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(name="list_game_windows", description="List top-level windows that match the configured game process/title filters. Read-only; never activates anything.", annotations=_ro("list_game_windows"))
    def list_game_windows() -> dict[str, Any]:
        candidates = list_candidates(session.settings)
        return {
            "ok": True,
            "count": len(candidates),
            "candidates": [c.to_dict() for c in candidates],
            "filters": {
                "process_names": session.settings.process_names,
                "title_substrings": session.settings.title_substrings,
                "path_substrings": session.settings.path_substrings,
            },
        }

    @mcp.tool(name="bind_game", description="Bind to the game window by HWND + PID + process identity. Omit hwnd when exactly one candidate exists; with several candidates you must pass one explicitly. Does not focus or move the window.", annotations=_rw("bind_game", idempotent=True))
    def bind_game(hwnd: int | None = None) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            binding = bind(session.settings, hwnd)
            session.binding = binding
            session.store.invalidate("rebound")
            state = validate_window(binding, session.settings, require_foreground=False, check_occlusion=False)
            return {"ok": True, "binding": binding.to_dict(), "window": state.to_dict()}

        try:
            return session.run_exclusive(_do)
        except GameMcpError as exc:
            return error_response(exc)

    @mcp.tool(name="get_game_screen", description="Capture ONLY the bound game's client area and return it as an image plus JSON metadata containing observation_id, image size, capture time and per-stage timings. Refuses when the game is minimized, not foreground, off-screen, cloaked, or covered by another window.", annotations=_ro("get_game_screen"))
    def get_game_screen() -> ToolResult:
        try:
            result = session.run_exclusive(
                lambda: capture_game_screen(session.binding, session.settings, session.store)
            )
        except GameMcpError as exc:
            payload = error_response(exc)
            return ToolResult(content=[json.dumps(payload, ensure_ascii=False)], structured_content=payload)
        meta = result.metadata()
        return ToolResult(
            content=[Image(data=result.png, format=result.image_format), json.dumps(meta, ensure_ascii=False)],
            structured_content=meta,
        )

    @mcp.tool(name="click", description="Single mouse click at image pixel (x, y) of the observation identified by observation_id. Re-validates focus, geometry and occlusion first; the observation_id is consumed.", annotations=_rw("click"))
    def click(x: int, y: int, observation_id: str, button: str = "left") -> dict[str, Any]:
        try:
            return session.inputs.click(session.binding, x, y, observation_id, button)  # type: ignore[arg-type]
        except GameMcpError as exc:
            return error_response(exc)

    @mcp.tool(name="move_mouse", description="Move/hover the cursor to image pixel (x, y) without clicking (e.g. to read a card tooltip). Consumes the observation_id.", annotations=_rw("move_mouse"))
    def move_mouse(x: int, y: int, observation_id: str) -> dict[str, Any]:
        try:
            return session.inputs.move_mouse(session.binding, x, y, observation_id)
        except GameMcpError as exc:
            return error_response(exc)

    @mcp.tool(name="press_key", description="Press and release one whitelisted key (see get_status for the list). No modifiers, no combinations. Consumes the observation_id.", annotations=_rw("press_key"))
    def press_key(key: str, observation_id: str) -> dict[str, Any]:
        try:
            return session.inputs.press_key(session.binding, key, observation_id)
        except GameMcpError as exc:
            return error_response(exc)

    @mcp.tool(name="stop_input", description="Immediately release any held mouse button/key and refuse all further input until resume_input is called. Screens can still be captured.", annotations=_rw("stop_input", idempotent=True))
    def stop_input(reason: str = "requested by tool caller") -> dict[str, Any]:
        state = session.latch.engage("tool", reason)
        session.store.invalidate("stopped")
        return {"ok": True, "stop": state.to_dict(), "held_after_release": session.held.snapshot()}

    @mcp.tool(name="resume_input", description="Re-enable input after stop_input. Fails if the stop was triggered by the human operator; that requires a server restart.", annotations=_rw("resume_input", idempotent=True))
    def resume_input() -> dict[str, Any]:
        try:
            state = session.latch.resume()
            return {"ok": True, "stop": state.to_dict()}
        except GameMcpError as exc:
            return error_response(exc)

    @mcp.tool(name="get_status", description="Report binding, current window state (without requiring foreground), stop latch, key whitelist, emergency-stop configuration and current observation.", annotations=_ro("get_status"))
    def get_status() -> dict[str, Any]:
        binding = session.binding
        window: dict[str, Any] | None = None
        window_error: dict[str, Any] | None = None
        if binding is not None:
            try:
                window = validate_window(binding, session.settings, require_foreground=False, check_occlusion=True).to_dict()
            except GameMcpError as exc:
                window_error = exc.to_dict()
        current = session.store.current()
        return {
            "ok": True,
            "version": __version__,
            "dpi_awareness": session.dpi_mode,
            "binding": binding.to_dict() if binding else None,
            "ime": win32.ime_state(binding.hwnd).to_dict() if binding and window else None,
            "window": window,
            "window_error": window_error,
            "stop": session.latch.state().to_dict(),
            "operator_stop": session.watcher.describe() if session.watcher else None,
            "held_inputs": session.held.snapshot(),
            "current_observation": current.to_dict() if current else None,
            "key_whitelist": session.settings.key_whitelist,
            "observation_ttl_s": session.settings.observation_ttl_s,
            "screenshot_backend": session.settings.screenshot_backend,
            "debug_dir": session.settings.debug_dir,
            "debug_retention": {
                "keep_files": session.settings.debug_keep_files,
                "max_age_h": session.settings.debug_max_age_h,
                "pruned_at_start": session.debug_pruned_at_start,
            },
            "uptime_s": round(time.time() - session.started_at, 1),
        }

    return mcp, session
