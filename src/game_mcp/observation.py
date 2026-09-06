"""Observation ids tie every input to the screenshot the AI actually looked at.

Only the most recent observation is ever valid. It becomes stale when:
  * a newer observation is issued,
  * its TTL elapses,
  * the client rect moved or resized since it was taken,
  * any input action ran (the screen may have changed).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from game_mcp import win32
from game_mcp.errors import ErrorCode, GameMcpError


@dataclass(frozen=True)
class Observation:
    id: str
    hwnd: int
    client_rect: win32.ScreenRect
    dpi: int
    image_size: tuple[int, int]
    scale: float
    created_monotonic: float
    created_wall: float
    ttl_s: float

    @property
    def expires_monotonic(self) -> float:
        return self.created_monotonic + self.ttl_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.id,
            "hwnd": self.hwnd,
            "client_rect_screen": self.client_rect.to_dict(),
            "client_size": [self.client_rect.width, self.client_rect.height],
            "image_size": list(self.image_size),
            "scale": self.scale,
            "dpi": self.dpi,
            "captured_at_unix": self.created_wall,
            "valid_for_s": self.ttl_s,
        }


class ObservationStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Observation | None = None
        self._invalidated_reason: str | None = None

    def issue(
        self,
        *,
        hwnd: int,
        client_rect: win32.ScreenRect,
        dpi: int,
        image_size: tuple[int, int],
        scale: float,
        ttl_s: float,
    ) -> Observation:
        obs = Observation(
            id=uuid.uuid4().hex[:12],
            hwnd=hwnd,
            client_rect=client_rect,
            dpi=dpi,
            image_size=image_size,
            scale=scale,
            created_monotonic=time.monotonic(),
            created_wall=time.time(),
            ttl_s=ttl_s,
        )
        with self._lock:
            self._current = obs
            self._invalidated_reason = None
        return obs

    def current(self) -> Observation | None:
        with self._lock:
            return self._current

    def invalidate(self, reason: str) -> None:
        with self._lock:
            if self._current is not None:
                self._current = None
                self._invalidated_reason = reason

    def require(
        self, observation_id: str, *, client_rect_now: win32.ScreenRect, hwnd_now: int | None = None
    ) -> Observation:
        """Return the observation if it is still the valid current one, else raise OBSERVATION_STALE."""
        with self._lock:
            current = self._current
            invalidated_reason = self._invalidated_reason
        if not observation_id:
            raise GameMcpError(ErrorCode.OBSERVATION_STALE, "observation_id is required.", {"reason": "missing"})
        if current is None:
            raise GameMcpError(
                ErrorCode.OBSERVATION_STALE,
                "No valid observation. Call get_game_screen and use the new observation_id.",
                {"reason": invalidated_reason or "none_issued", "given": observation_id},
            )
        if current.id != observation_id:
            raise GameMcpError(
                ErrorCode.OBSERVATION_STALE,
                "observation_id is not the most recent one.",
                {"reason": "superseded", "given": observation_id, "current": current.id},
            )
        if time.monotonic() > current.expires_monotonic:
            self.invalidate("expired")
            raise GameMcpError(
                ErrorCode.OBSERVATION_STALE,
                "The observation is older than its TTL; capture a new screen.",
                {"reason": "expired", "age_s": round(time.monotonic() - current.created_monotonic, 3)},
            )
        if hwnd_now is not None and hwnd_now != current.hwnd:
            self.invalidate("rebound")
            raise GameMcpError(
                ErrorCode.OBSERVATION_STALE,
                "The observation belongs to a different window than the current binding.",
                {"reason": "rebound", "observed_hwnd": current.hwnd, "current_hwnd": hwnd_now},
            )
        if client_rect_now != current.client_rect:
            self.invalidate("window_moved_or_resized")
            raise GameMcpError(
                ErrorCode.OBSERVATION_STALE,
                "The game window moved or resized since the observation was taken.",
                {
                    "reason": "window_moved_or_resized",
                    "observed_rect": current.client_rect.to_dict(),
                    "current_rect": client_rect_now.to_dict(),
                },
            )
        return current
