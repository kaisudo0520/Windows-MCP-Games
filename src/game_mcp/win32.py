"""Win32 window queries used for binding, validation and occlusion checks.

Everything here is read-only with respect to other applications: no window is
activated, moved, or sent a message. Coordinates are physical pixels, which
requires the process to be DPI aware (see ``ensure_dpi_awareness``).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
from dataclasses import dataclass

import pywintypes
import win32con
import win32gui
import win32process

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi
_imm32 = ctypes.windll.imm32
_imm32.ImmGetDefaultIMEWnd.restype = ctypes.c_void_p
_user32.SendMessageTimeoutW.restype = ctypes.c_size_t

WM_IME_CONTROL = 0x0283
IMC_GETCONVERSIONMODE = 0x0001
IMC_GETOPENSTATUS = 0x0005
IME_CMODE_NATIVE = 0x0001
SMTO_ABORTIFHUNG = 0x0002

_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.GetAncestor.restype = ctypes.c_void_p
_user32.WindowFromPoint.restype = ctypes.c_void_p
_user32.MonitorFromPoint.restype = ctypes.c_void_p

GA_ROOT = 2
DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONULL = 0

_dpi_state: dict[str, str] = {}


def ensure_dpi_awareness() -> str:
    """Make the process per-monitor DPI aware so every coordinate is in physical pixels.

    Must run before any other user32 call in this process. Returns a short
    description of what was achieved, for the status tool.
    """
    if _dpi_state:
        return _dpi_state["mode"]
    mode = "unaware"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4 (Windows 10 1703+)
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            mode = "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    if mode == "unaware":
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
                mode = "per_monitor"
        except (AttributeError, OSError):
            pass
    if mode == "unaware":
        try:
            if _user32.SetProcessDPIAware():
                mode = "system"
        except (AttributeError, OSError):
            pass
    _dpi_state["mode"] = mode
    logger.info("DPI awareness: %s", mode)
    return mode


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
        }


def is_window(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(win32gui.IsIconic(hwnd))


def is_visible(hwnd: int) -> bool:
    return bool(win32gui.IsWindowVisible(hwnd))


def foreground_window() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def root_window(hwnd: int) -> int:
    return int(_user32.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) or 0)


def window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:  # pragma: no cover - defensive, pywin32 raises on dead handles
        return ""


def window_class(hwnd: int) -> str:
    try:
        return win32gui.GetClassName(hwnd) or ""
    except Exception:  # pragma: no cover
        return ""


def window_pid(hwnd: int) -> int:
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except Exception:  # pragma: no cover
        return 0


def window_rect(hwnd: int) -> ScreenRect:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except pywintypes.error as exc:
        raise OSError(f"GetWindowRect failed: {exc}") from exc
    return ScreenRect(left, top, right, bottom)


def client_rect_on_screen(hwnd: int) -> ScreenRect:
    """Client area of *hwnd* in screen (physical pixel) coordinates.

    Raises OSError (not pywintypes.error) when the handle is dead, so callers
    can classify it as WINDOW_GONE.
    """
    try:
        _l, _t, width, height = win32gui.GetClientRect(hwnd)
    except pywintypes.error as exc:
        raise OSError(f"GetClientRect failed: {exc}") from exc
    point = wt.POINT(0, 0)
    if not _user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(point)):
        raise OSError("ClientToScreen failed")
    return ScreenRect(point.x, point.y, point.x + width, point.y + height)


def is_cloaked(hwnd: int) -> bool:
    """True when DWM has cloaked the window (e.g. it lives on another virtual desktop)."""
    cloaked = wt.DWORD(0)
    result = _dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and cloaked.value != 0


def window_dpi(hwnd: int) -> int:
    try:
        dpi = int(_user32.GetDpiForWindow(ctypes.c_void_p(hwnd)))
        if dpi > 0:
            return dpi
    except (AttributeError, OSError):
        pass
    return 96


def window_from_point(x: int, y: int) -> int:
    point = wt.POINT(x, y)
    # WindowFromPoint takes POINT by value; ctypes passes a structure by value fine.
    return int(_user32.WindowFromPoint(point) or 0)


def point_on_a_monitor(x: int, y: int) -> bool:
    point = wt.POINT(x, y)
    return bool(_user32.MonitorFromPoint(point, MONITOR_DEFAULTTONULL))


def virtual_screen() -> ScreenRect:
    left = _user32.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    top = _user32.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    width = _user32.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    height = _user32.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return ScreenRect(left, top, left + width, top + height)


def enum_top_level_windows() -> list[int]:
    handles: list[int] = []

    def _cb(hwnd: int, _extra: object) -> bool:
        handles.append(int(hwnd))
        return True

    win32gui.EnumWindows(_cb, None)
    return handles


@dataclass(frozen=True)
class Occluder:
    hwnd: int
    title: str
    class_name: str
    pid: int
    sample_x: int
    sample_y: int

    def to_dict(self) -> dict[str, object]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class": self.class_name,
            "pid": self.pid,
            "sample_point": [self.sample_x, self.sample_y],
        }


def _sample_points(rect: ScreenRect, step: int) -> list[tuple[int, int]]:
    """Grid over the rect plus its inset corners and center."""
    xs = list(range(rect.left + step // 2, rect.right, step))
    ys = list(range(rect.top + step // 2, rect.bottom, step))
    points = [(x, y) for y in ys for x in xs]
    inset = 1
    points.extend(
        [
            (rect.left + inset, rect.top + inset),
            (rect.right - 1 - inset, rect.top + inset),
            (rect.left + inset, rect.bottom - 1 - inset),
            (rect.right - 1 - inset, rect.bottom - 1 - inset),
            ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2),
        ]
    )
    return points


def find_occluder(hwnd: int, rect: ScreenRect, step: int) -> Occluder | None:
    """Hit-test a grid of points inside *rect*; return the first window that is not *hwnd*.

    Limitation: ``WindowFromPoint`` skips windows that are transparent to hit
    testing (``WS_EX_TRANSPARENT`` layered overlays, some game overlays and
    screen-annotation tools) and it cannot see anything smaller than the grid
    step that falls between sample points. A ``None`` result therefore means
    "no hit-testable window was found on the grid", not "pixel-perfect
    isolation".
    """
    for x, y in _sample_points(rect, step):
        hit = window_from_point(x, y)
        if hit == 0:
            continue
        if root_window(hit) != hwnd:
            return Occluder(
                hwnd=hit,
                title=window_title(hit),
                class_name=window_class(hit),
                pid=window_pid(hit),
                sample_x=x,
                sample_y=y,
            )
    return None


@dataclass(frozen=True)
class AboveWindow:
    """A visible window that sits above the target in z-order and overlaps its rect."""

    hwnd: int
    title: str
    class_name: str
    pid: int
    rect: ScreenRect
    hit_test_transparent: bool  # WS_EX_LAYERED | WS_EX_TRANSPARENT: invisible to WindowFromPoint
    enabled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class": self.class_name,
            "pid": self.pid,
            "rect": self.rect.to_dict(),
            "hit_test_transparent": self.hit_test_transparent,
            "enabled": self.enabled,
        }


def _intersects(a: ScreenRect, b: ScreenRect) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def windows_above(hwnd: int, rect: ScreenRect) -> list[AboveWindow]:
    """Walk the z-order upward from *hwnd* and return visible windows overlapping *rect*.

    Complements ``find_occluder``: this sees disabled windows and hit-test
    transparent overlays (e.g. Discord/Steam overlays) that ``WindowFromPoint``
    skips. Cloaked and zero-area windows are ignored.
    """
    found: list[AboveWindow] = []
    other = win32gui.GetWindow(hwnd, win32con.GW_HWNDPREV)
    guard = 0
    while other and guard < 4096:
        guard += 1
        try:
            if win32gui.IsWindowVisible(other) and not is_cloaked(other):
                left, top, right, bottom = win32gui.GetWindowRect(other)
                wrect = ScreenRect(left, top, right, bottom)
                if wrect.width > 0 and wrect.height > 0 and _intersects(wrect, rect):
                    ex = win32gui.GetWindowLong(other, win32con.GWL_EXSTYLE)
                    transparent = bool(ex & win32con.WS_EX_LAYERED) and bool(ex & win32con.WS_EX_TRANSPARENT)
                    found.append(
                        AboveWindow(
                            hwnd=int(other),
                            title=window_title(other),
                            class_name=window_class(other),
                            pid=window_pid(other),
                            rect=wrect,
                            hit_test_transparent=transparent,
                            enabled=bool(win32gui.IsWindowEnabled(other)),
                        )
                    )
        except pywintypes.error:
            pass  # window vanished mid-walk
        other = win32gui.GetWindow(other, win32con.GW_HWNDPREV)
    return found


@dataclass(frozen=True)
class ImeState:
    ime_window: int
    open: bool | None  # None = could not query (no IME window / timeout)
    conversion_mode: int | None
    layout: str

    @property
    def native_open(self) -> bool:
        return bool(self.open) and self.conversion_mode is not None and bool(self.conversion_mode & IME_CMODE_NATIVE)

    def to_dict(self) -> dict[str, object]:
        return {
            "ime_window": self.ime_window,
            "open": self.open,
            "conversion_mode": self.conversion_mode,
            "native_open": self.native_open,
            "keyboard_layout": self.layout,
        }


def _ime_query(ime_wnd: int, what: int) -> int | None:
    result = ctypes.c_size_t(0)
    ok = _user32.SendMessageTimeoutW(
        ctypes.c_void_p(ime_wnd), WM_IME_CONTROL, ctypes.c_size_t(what), ctypes.c_size_t(0),
        SMTO_ABORTIFHUNG, 200, ctypes.byref(result),
    )
    return int(result.value) if ok else None


def ime_state(hwnd: int) -> ImeState:
    """Query the IME open/conversion state of *hwnd*'s thread (read-only WM_IME_CONTROL queries).

    An IME open in native mode (e.g. Microsoft Bopomofo in Chinese mode) turns
    injected letter/digit keys into VK_PROCESSKEY, so the game never sees them.
    """
    try:
        thread_id, _pid = win32process.GetWindowThreadProcessId(hwnd)
        layout = hex(_user32.GetKeyboardLayout(thread_id) & 0xFFFFFFFF)
    except Exception:
        layout = "unknown"
    ime_wnd = int(_imm32.ImmGetDefaultIMEWnd(ctypes.c_void_p(hwnd)) or 0)
    if not ime_wnd:
        return ImeState(0, None, None, layout)
    is_open = _ime_query(ime_wnd, IMC_GETOPENSTATUS)
    mode = _ime_query(ime_wnd, IMC_GETCONVERSIONMODE)
    return ImeState(ime_wnd, None if is_open is None else bool(is_open), mode, layout)


def point_hits_window(hwnd: int, x: int, y: int) -> tuple[bool, int]:
    """Return (True, hit) when the top-most hit-testable window at (x, y) belongs to *hwnd*."""
    hit = window_from_point(x, y)
    return (hit != 0 and root_window(hit) == hwnd), hit
