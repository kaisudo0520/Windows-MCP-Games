"""Re-validate the bound window before every capture and every input.

The checks run in a fixed order so the error code names the *first* reason the
screen cannot be trusted: binding alive -> not minimized -> not cloaked ->
foreground -> on screen -> not occluded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from game_mcp import win32
from game_mcp.binding import Binding, check_binding_alive
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError


@dataclass(frozen=True)
class WindowState:
    hwnd: int
    client_rect: win32.ScreenRect
    dpi: int
    foreground_hwnd: int
    checked_at: float  # time.monotonic()
    occlusion_checked: bool
    occlusion_check_ms: float
    overlays: tuple[win32.AboveWindow, ...] = ()

    @property
    def is_foreground(self) -> bool:
        return self.foreground_hwnd == self.hwnd

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "client_rect_screen": self.client_rect.to_dict(),
            "client_size": [self.client_rect.width, self.client_rect.height],
            "dpi": self.dpi,
            "dpi_scale": round(self.dpi / 96, 4),
            "is_foreground": self.is_foreground,
            "foreground_hwnd": self.foreground_hwnd,
            "occlusion_checked": self.occlusion_checked,
            "overlays": [o.to_dict() for o in self.overlays],
        }


def _check_on_screen(rect: win32.ScreenRect) -> None:
    if rect.width <= 0 or rect.height <= 0:
        raise GameMcpError(ErrorCode.OFFSCREEN, "Client area has zero size.", rect.to_dict())
    vs = win32.virtual_screen()
    inside_virtual = (
        rect.left >= vs.left and rect.top >= vs.top and rect.right <= vs.right and rect.bottom <= vs.bottom
    )
    corners = [
        (rect.left, rect.top),
        (rect.right - 1, rect.top),
        (rect.left, rect.bottom - 1),
        (rect.right - 1, rect.bottom - 1),
    ]
    if not inside_virtual or not all(win32.point_on_a_monitor(x, y) for x, y in corners):
        raise GameMcpError(
            ErrorCode.OFFSCREEN,
            "Part of the client area is outside every monitor; the capture would contain undefined pixels.",
            {"client_rect": rect.to_dict(), "virtual_screen": vs.to_dict()},
        )


def validate_window(
    binding: Binding | None,
    settings: Settings,
    *,
    require_foreground: bool = True,
    check_occlusion: bool = True,
) -> WindowState:
    """Run every safety check and return the current geometry, or raise GameMcpError."""
    if binding is None:
        raise GameMcpError(ErrorCode.NOT_BOUND, "No game window is bound. Call bind_game first.")
    check_binding_alive(binding)
    hwnd = binding.hwnd

    if win32.is_minimized(hwnd):
        raise GameMcpError(ErrorCode.MINIMIZED, "The game window is minimized.")
    if win32.is_cloaked(hwnd):
        raise GameMcpError(
            ErrorCode.CLOAKED,
            "The game window is cloaked by DWM (probably on another virtual desktop).",
        )

    foreground = win32.foreground_window()
    if require_foreground and foreground != hwnd:
        raise GameMcpError(
            ErrorCode.NOT_FOREGROUND,
            "The game window is not the foreground window. This tool never steals focus; "
            "the operator must bring the game to the front.",
            {
                "foreground_hwnd": foreground,
                "foreground_title": win32.window_title(foreground) if foreground else "",
                "foreground_pid": win32.window_pid(foreground) if foreground else 0,
            },
        )

    try:
        rect = win32.client_rect_on_screen(hwnd)
    except OSError as exc:
        raise GameMcpError(ErrorCode.WINDOW_GONE, f"Could not read client rect: {exc}") from exc
    _check_on_screen(rect)

    occlusion_ms = 0.0
    overlays: tuple[win32.AboveWindow, ...] = ()
    if check_occlusion:
        started = time.perf_counter()
        above = win32.windows_above(hwnd, rect)
        opaque = [w for w in above if not w.hit_test_transparent]
        occluder = None if opaque else win32.find_occluder(hwnd, rect, settings.occlusion_grid_px)
        occlusion_ms = (time.perf_counter() - started) * 1000
        if opaque:
            raise GameMcpError(
                ErrorCode.OCCLUDED,
                "A window above the game in z-order overlaps its client area.",
                {"method": "z_order", "occluder": opaque[0].to_dict(), "client_rect": rect.to_dict()},
            )
        if occluder is not None:
            raise GameMcpError(
                ErrorCode.OCCLUDED,
                "Another window covers part of the game's client area.",
                {"method": "hit_test", "occluder": occluder.to_dict(), "client_rect": rect.to_dict()},
            )
        # Hit-test transparent overlays (Discord/Steam overlays, annotation tools)
        # are normally invisible but can draw into the captured pixels at any time.
        # They are reported, not blocked.
        overlays = tuple(w for w in above if w.hit_test_transparent)

    return WindowState(
        hwnd=hwnd,
        client_rect=rect,
        dpi=win32.window_dpi(hwnd),
        foreground_hwnd=foreground,
        checked_at=time.monotonic(),
        occlusion_checked=check_occlusion,
        occlusion_check_ms=occlusion_ms,
        overlays=overlays,
    )
