"""Validated, single-step input: click, hover, key press.

Every action follows the same protocol:

1. refuse if the stop latch is engaged;
2. re-validate binding, minimized/cloaked, foreground, on-screen, occlusion;
3. require the caller's ``observation_id`` to be the current, unexpired one
   taken for the same window at the same client geometry;
4. map image pixels -> client pixels -> screen pixels and hit-test the exact
   target point;
5. invalidate the observation (the screen is about to change);
6. place the cursor and confirm it really landed there (ClipCursor or a
   failed SetCursorPos would otherwise redirect the click);
7. final gate: stop latch + foreground + target hit-test again, then inject;
8. verify afterwards that focus, geometry and the target point's ownership
   survived, note whether a stop landed mid-action, and report timings.

Race window: between step 7's checks and the Win32 calls a few hundred
microseconds pass in which another window could take focus or appear; the
button hold inside the click adds ~50 ms more. Both are measured and reported
(``precheck_to_input_gap`` and ``injection_window``). They cannot be closed
from user mode, so ``post_action.focus_kept`` / ``target_still_ours`` = false
means the input *may* have gone elsewhere and the caller must look at a fresh
screen before acting again.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Literal

from game_mcp import win32
from game_mcp.binding import Binding
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.observation import Observation, ObservationStore
from game_mcp.safety import HeldInputs, StopLatch
from game_mcp.validation import WindowState, validate_window
from game_mcp.vendor import win32_input as w32

logger = logging.getLogger(__name__)

MouseButton = Literal["left", "right"]

_NAMED_KEYS: dict[str, int] = {
    "enter": int(w32.Keys.VK_RETURN),
    "return": int(w32.Keys.VK_RETURN),
    "space": int(w32.Keys.VK_SPACE),
    "escape": int(w32.Keys.VK_ESCAPE),
    "esc": int(w32.Keys.VK_ESCAPE),
    "tab": int(w32.Keys.VK_TAB),
    "backspace": int(w32.Keys.VK_BACK),
    "up": int(w32.Keys.VK_UP),
    "down": int(w32.Keys.VK_DOWN),
    "left": int(w32.Keys.VK_LEFT),
    "right": int(w32.Keys.VK_RIGHT),
}


def key_to_vk(name: str) -> int | None:
    key = name.strip().lower()
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) == 1 and (key.isdigit() or "a" <= key <= "z"):
        return ord(key.upper())
    return None


def _point_covered_error(hwnd: int, screen_pt: tuple[int, int], when: str) -> GameMcpError:
    _hits, hit_hwnd = win32.point_hits_window(hwnd, *screen_pt)
    return GameMcpError(
        ErrorCode.OCCLUDED,
        f"The target point is covered by another window ({when}).",
        {
            "screen_point": list(screen_pt),
            "hit_hwnd": hit_hwnd,
            "hit_title": win32.window_title(hit_hwnd),
            "phase": when,
        },
    )


class InputService:
    def __init__(
        self,
        settings: Settings,
        store: ObservationStore,
        held: HeldInputs,
        latch: StopLatch,
        op_lock: threading.Lock | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._held = held
        self._latch = latch
        #: Shared with capture/bind so that no screen can be issued while an action runs.
        self._op_lock = op_lock or threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _map_point(self, obs: Observation, x: int, y: int) -> tuple[tuple[int, int], tuple[int, int]]:
        img_w, img_h = obs.image_size
        if not (isinstance(x, int) and isinstance(y, int)) or isinstance(x, bool) or isinstance(y, bool):
            raise GameMcpError(ErrorCode.INVALID_COORDINATES, "x and y must be integers.")
        if not (0 <= x < img_w and 0 <= y < img_h):
            raise GameMcpError(
                ErrorCode.INVALID_COORDINATES,
                "Point is outside the observed image.",
                {"x": x, "y": y, "image_size": [img_w, img_h]},
            )
        cx = min(obs.client_rect.width - 1, round(x / obs.scale))
        cy = min(obs.client_rect.height - 1, round(y / obs.scale))
        return (cx, cy), (obs.client_rect.left + cx, obs.client_rect.top + cy)

    def _precheck(self, binding: Binding | None, observation_id: str) -> tuple[WindowState, Observation, float]:
        started = time.perf_counter()
        self._latch.require_running()
        state = validate_window(binding, self._settings, require_foreground=True, check_occlusion=True)
        obs = self._store.require(observation_id, client_rect_now=state.client_rect, hwnd_now=state.hwnd)
        return state, obs, (time.perf_counter() - started) * 1000

    def _place_cursor(self, screen_pt: tuple[int, int]) -> None:
        """Move the cursor and confirm it landed; abort before any button event otherwise."""
        w32.SetCursorPos(*screen_pt)
        cur = w32.GetCursorPos()
        if abs(cur[0] - screen_pt[0]) > 1 or abs(cur[1] - screen_pt[1]) > 1:
            raise GameMcpError(
                ErrorCode.INPUT_FAILED,
                "The cursor could not be placed on the target (cursor clipped or SetCursorPos refused); nothing was sent.",
                {"requested": list(screen_pt), "actual": list(cur)},
            )

    def _final_gate(self, hwnd: int, screen_pt: tuple[int, int] | None) -> float:
        """Last checks right before injection: stop latch, foreground, target ownership."""
        self._latch.require_running()
        if win32.foreground_window() != hwnd:
            raise GameMcpError(ErrorCode.NOT_FOREGROUND, "Focus was lost right before input; nothing was sent.")
        if screen_pt is not None:
            hits, _hit = win32.point_hits_window(hwnd, *screen_pt)
            if not hits:
                raise _point_covered_error(hwnd, screen_pt, "final_gate")
        return time.perf_counter()

    def _postcheck(self, binding: Binding, state: WindowState, screen_pt: tuple[int, int] | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "window_alive": False,
            "focus_kept": False,
            "geometry_unchanged": False,
            "stopped_during_action": self._latch.state().stopped,
        }
        try:
            after = validate_window(binding, self._settings, require_foreground=False, check_occlusion=False)
            result["window_alive"] = True
            result["focus_kept"] = after.is_foreground
            result["geometry_unchanged"] = after.client_rect == state.client_rect
            if not after.is_foreground:
                result["foreground_hwnd"] = after.foreground_hwnd
                result["foreground_title"] = win32.window_title(after.foreground_hwnd)
        except GameMcpError as exc:
            result["error"] = exc.to_dict()
        if screen_pt is not None:
            cursor = w32.GetCursorPos()
            result["cursor_screen"] = list(cursor)
            result["cursor_at_target"] = abs(cursor[0] - screen_pt[0]) <= 1 and abs(cursor[1] - screen_pt[1]) <= 1
            hits, hit_hwnd = win32.point_hits_window(state.hwnd, *screen_pt)
            result["target_still_ours"] = hits
            if not hits:
                result["target_hit_hwnd"] = hit_hwnd
        result["trustworthy"] = bool(
            result["window_alive"]
            and result["focus_kept"]
            and result["geometry_unchanged"]
            and not result["stopped_during_action"]
            and (screen_pt is None or result.get("target_still_ours", False))
        )
        return result

    def _tap(self, vk: int) -> None:
        """Press and release one key with held-state bookkeeping."""
        self._held.note_key_down(vk)
        w32.PressKey(vk, waitTime=0.03)
        w32.ReleaseKey(vk, waitTime=0.02)
        self._held.note_key_up(vk)

    def _run_locked(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if not self._op_lock.acquire(blocking=False):
            raise GameMcpError(ErrorCode.BUSY, "Another capture or input action is still running.")
        try:
            return fn()
        finally:
            self._op_lock.release()

    @staticmethod
    def _timings(total_started: float, validate_ms: float, gate_at: float, input_started: float, input_done: float, post_ms: float) -> dict[str, float]:
        return {
            "validate": round(validate_ms, 2),
            "precheck_to_input_gap": round((input_started - gate_at) * 1000, 3),
            "injection_window": round((input_done - gate_at) * 1000, 2),
            "input": round((input_done - input_started) * 1000, 2),
            "post_verify": round(post_ms, 2),
            "total": round((time.perf_counter() - total_started) * 1000, 2),
        }

    def _mouse_action(
        self,
        binding: Binding | None,
        x: int,
        y: int,
        observation_id: str,
        action: str,
        inject: Callable[[tuple[int, int]], None],
        extra: dict[str, Any],
        next_hint: str,
    ) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            total_started = time.perf_counter()
            state, obs, validate_ms = self._precheck(binding, observation_id)
            client_pt, screen_pt = self._map_point(obs, x, y)
            hits, _hit = win32.point_hits_window(state.hwnd, *screen_pt)
            if not hits:
                raise _point_covered_error(state.hwnd, screen_pt, "precheck")
            self._store.invalidate("input_performed")
            self._place_cursor(screen_pt)
            time.sleep(self._settings.click_settle_s)  # let the game register the hover first
            gate_at = self._final_gate(state.hwnd, screen_pt)
            input_started = time.perf_counter()
            try:
                inject(screen_pt)
            except GameMcpError:
                raise
            except Exception as exc:
                released = self._held.release_all()
                raise GameMcpError(ErrorCode.INPUT_FAILED, f"{action} failed: {exc}", {"released": released}) from exc
            input_done = time.perf_counter()
            post_started = time.perf_counter()
            post = self._postcheck(binding, state, screen_pt)
            post_ms = (time.perf_counter() - post_started) * 1000
            result: dict[str, Any] = {
                "ok": True,
                "action": action,
                "image_point": [x, y],
                "client_point": list(client_pt),
                "screen_point": list(screen_pt),
                "observation_id_used": obs.id,
                "observation_invalidated": True,
                "post_action": post,
                "timing_ms": self._timings(total_started, validate_ms, gate_at, input_started, input_done, post_ms),
                "next": next_hint,
            }
            result.update(extra)
            return result

        return self._run_locked(_do)

    # ------------------------------------------------------------------ actions

    def click(self, binding: Binding | None, x: int, y: int, observation_id: str, button: MouseButton = "left") -> dict[str, Any]:
        if button not in ("left", "right"):
            raise GameMcpError(ErrorCode.INVALID_ARGUMENT, "button must be 'left' or 'right'.", {"button": button})

        def _inject(screen_pt: tuple[int, int]) -> None:
            self._held.note_button_down(button)
            w32.Click(screen_pt[0], screen_pt[1], button=button, waitTime=0.02)
            self._held.note_button_up(button)

        return self._mouse_action(
            binding, x, y, observation_id, "click", _inject, {"button": button},
            "Call get_game_screen to obtain a fresh observation_id before the next action.",
        )

    def move_mouse(self, binding: Binding | None, x: int, y: int, observation_id: str) -> dict[str, Any]:
        def _inject(screen_pt: tuple[int, int]) -> None:
            # Direct jump (no interpolated path): the cursor never travels across
            # other windows on its way to the target.
            w32.MoveToDuration(screen_pt[0], screen_pt[1], duration=0, waitTime=0.02)

        return self._mouse_action(
            binding, x, y, observation_id, "move_mouse", _inject, {},
            "Call get_game_screen to see the hover result.",
        )

    def press_key(self, binding: Binding | None, key: str, observation_id: str) -> dict[str, Any]:
        name = key.strip().lower()
        if name not in self._settings.key_whitelist:
            raise GameMcpError(
                ErrorCode.KEY_NOT_ALLOWED,
                f"Key '{key}' is not in the whitelist.",
                {"allowed": self._settings.key_whitelist},
            )
        vk = key_to_vk(name)
        if vk is None:
            raise GameMcpError(ErrorCode.KEY_NOT_ALLOWED, f"Key '{key}' has no virtual-key mapping.")

        def _do() -> dict[str, Any]:
            total_started = time.perf_counter()
            state, obs, validate_ms = self._precheck(binding, observation_id)
            ime = win32.ime_state(state.hwnd)
            ime_toggled = False
            if ime.native_open and self._settings.ime_autotoggle:
                # Tool-internal remediation, not an AI-selectable key: tap Shift so the IME
                # switches to alphanumeric, then re-query to confirm.
                self._store.invalidate("input_performed")
                self._final_gate(state.hwnd, None)
                self._tap(int(w32.Keys.VK_SHIFT))
                time.sleep(0.08)
                ime = win32.ime_state(state.hwnd)
                ime_toggled = True
            if ime.native_open and not self._settings.allow_ime_native:
                raise GameMcpError(
                    ErrorCode.IME_ACTIVE,
                    "The game window's IME is still open in native mode; the key would be swallowed. "
                    "Switch the game's input method to English manually and retry.",
                    {**ime.to_dict(), "shift_tapped": ime_toggled},
                )
            self._store.invalidate("input_performed")
            gate_at = self._final_gate(state.hwnd, None)
            input_started = time.perf_counter()
            try:
                self._tap(vk)
            except Exception as exc:
                released = self._held.release_all()
                raise GameMcpError(ErrorCode.INPUT_FAILED, f"Key press failed: {exc}", {"released": released}) from exc
            input_done = time.perf_counter()
            post_started = time.perf_counter()
            post = self._postcheck(binding, state, None)
            post_ms = (time.perf_counter() - post_started) * 1000
            return {
                "ok": True,
                "action": "press_key",
                "key": name,
                "vk": vk,
                "ime": {**ime.to_dict(), "shift_tapped": ime_toggled},
                "observation_id_used": obs.id,
                "observation_invalidated": True,
                "post_action": post,
                "timing_ms": self._timings(total_started, validate_ms, gate_at, input_started, input_done, post_ms),
                "next": "Call get_game_screen to obtain a fresh observation_id before the next action.",
            }

        return self._run_locked(_do)
