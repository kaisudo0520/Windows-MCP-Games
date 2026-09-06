"""Thin Win32 mouse/keyboard primitives.

Vendored from Windows-MCP ``src/windows_mcp/uia/core.py`` and ``uia/enums.py``
at commit 08ddee78c26182b103d62c1c84c1fbec82a280b2 (MIT, Copyright (c) 2025
JEOMON GEORGE). Those upstream files are in turn derived from
"uiautomation for Python 3" by yinkaisheng
(https://github.com/yinkaisheng/Python-UIAutomation-for-Windows), Apache
License 2.0.

Modifications for sts-game-mcp:
  * Only the functions listed below were kept; everything COM/UIAutomation
    related was dropped so ``comtypes`` is not imported.
  * ``Click``/``PressMouse``/``ReleaseMouse`` keep upstream behaviour but the
    default ``waitTime`` is shorter (0.05s) because the caller re-validates the
    window after every action anyway.
  * ``ReleaseMouse`` gained a ``button`` parameter and ``RightPressMouse`` /
    ``MiddlePressMouse`` were folded into ``PressMouse(button=...)`` to keep
    held-state bookkeeping simple.
  * ``KeyWasPressed`` was added (GetAsyncKeyState bit 0 + bit 15).
  * ``PressKey``/``ReleaseKey`` now pass a real hardware scan code
    (``MapVirtualKeyW``) and only set ``KEYEVENTF_EXTENDEDKEY`` for keys that
    are actually extended. Upstream always set the extended flag with scan
    code 0, which Tk (and LWJGL, which reads the scan code) cannot translate.
"""

import ctypes
import ctypes.wintypes
import math
import time
from enum import IntEnum, IntFlag

OPERATION_WAIT_TIME = 0.05


class MouseEventFlag(IntFlag):
    """MouseEventFlag from Win32."""

    Move = 0x0001
    LeftDown = 0x0002
    LeftUp = 0x0004
    RightDown = 0x0008
    RightUp = 0x0010
    MiddleDown = 0x0020
    MiddleUp = 0x0040
    XDown = 0x0080
    XUp = 0x0100
    Wheel = 0x0800
    HWheel = 0x1000
    MoveNoCoalesce = 0x2000
    VirtualDesk = 0x4000
    Absolute = 0x8000


class KeyboardEventFlag(IntEnum):
    """KeyboardEventFlag from Win32."""

    KeyDown = 0x0000
    ExtendedKey = 0x0001
    KeyUp = 0x0002
    KeyUnicode = 0x0004
    KeyScanCode = 0x0008


class Keys(IntEnum):
    """Subset of Win32 virtual-key codes used by this project."""

    VK_LBUTTON = 0x01
    VK_RBUTTON = 0x02
    VK_MBUTTON = 0x04
    VK_BACK = 0x08
    VK_TAB = 0x09
    VK_RETURN = 0x0D
    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12  # ALT
    VK_PAUSE = 0x13
    VK_ESCAPE = 0x1B
    VK_SPACE = 0x20
    VK_LEFT = 0x25
    VK_UP = 0x26
    VK_RIGHT = 0x27
    VK_DOWN = 0x28
    VK_0 = 0x30
    VK_1 = 0x31
    VK_2 = 0x32
    VK_3 = 0x33
    VK_4 = 0x34
    VK_5 = 0x35
    VK_6 = 0x36
    VK_7 = 0x37
    VK_8 = 0x38
    VK_9 = 0x39
    VK_A = 0x41
    VK_E = 0x45
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    VK_F12 = 0x7B
    VK_SCROLL = 0x91


_user32 = ctypes.windll.user32


def GetCursorPos() -> tuple[int, int]:
    """Get current mouse cursor position (physical pixels)."""
    point = ctypes.wintypes.POINT(0, 0)
    _user32.GetPhysicalCursorPos(ctypes.byref(point))
    return point.x, point.y


def SetCursorPos(x: int, y: int) -> bool:
    """SetCursorPos from Win32. Return True if succeed otherwise False."""
    return bool(_user32.SetCursorPos(x, y))


def mouse_event(dwFlags: int, dx: int, dy: int, dwData: int, dwExtraInfo: int) -> None:
    """mouse_event from Win32."""
    _user32.mouse_event(dwFlags, dx, dy, dwData, dwExtraInfo)


def keybd_event(bVk: int, bScan: int, dwFlags: int, dwExtraInfo: int) -> None:
    """keybd_event from Win32."""
    _user32.keybd_event(bVk, bScan, dwFlags, dwExtraInfo)


def GetScreenSize() -> tuple[int, int]:
    """Return (width, height) of the primary screen."""
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1
    w = _user32.GetSystemMetrics(SM_CXSCREEN)
    h = _user32.GetSystemMetrics(SM_CYSCREEN)
    return w, h


_DOWN_FLAGS = {
    "left": MouseEventFlag.LeftDown,
    "right": MouseEventFlag.RightDown,
    "middle": MouseEventFlag.MiddleDown,
}
_UP_FLAGS = {
    "left": MouseEventFlag.LeftUp,
    "right": MouseEventFlag.RightUp,
    "middle": MouseEventFlag.MiddleUp,
}


def _abs_coords(x: int, y: int) -> tuple[int, int]:
    screenWidth, screenHeight = GetScreenSize()
    return x * 65535 // screenWidth, y * 65535 // screenHeight


def Click(x: int, y: int, button: str = "left", waitTime: float = OPERATION_WAIT_TIME) -> None:
    """Simulate a mouse click at point x, y (upstream ``Click``/``RightClick``/``MiddleClick``)."""
    SetCursorPos(x, y)
    ax, ay = _abs_coords(x, y)
    mouse_event(_DOWN_FLAGS[button] | MouseEventFlag.Absolute, ax, ay, 0, 0)
    time.sleep(0.05)
    mouse_event(_UP_FLAGS[button] | MouseEventFlag.Absolute, ax, ay, 0, 0)
    time.sleep(waitTime)


def PressMouse(
    x: int, y: int, button: str = "left", waitTime: float = OPERATION_WAIT_TIME
) -> None:
    """Press a mouse button at x, y (upstream ``PressMouse``)."""
    SetCursorPos(x, y)
    ax, ay = _abs_coords(x, y)
    mouse_event(_DOWN_FLAGS[button] | MouseEventFlag.Absolute, ax, ay, 0, 0)
    time.sleep(waitTime)


def ReleaseMouse(button: str = "left", waitTime: float = OPERATION_WAIT_TIME) -> None:
    """Release a mouse button at the current cursor position (upstream ``ReleaseMouse``)."""
    x, y = GetCursorPos()
    ax, ay = _abs_coords(x, y)
    mouse_event(_UP_FLAGS[button] | MouseEventFlag.Absolute, ax, ay, 0, 0)
    time.sleep(waitTime)


def MoveToDuration(x: int, y: int, duration: float, waitTime: float = OPERATION_WAIT_TIME) -> None:
    """Move the cursor to x, y over a bounded duration from the current position."""
    curX, curY = GetCursorPos()
    if duration <= 0:
        SetCursorPos(x, y)
        time.sleep(waitTime)
        return

    stepCount = max(2, min(200, math.ceil(duration / 0.01)))
    interval = duration / stepCount
    for i in range(1, stepCount + 1):
        ratio = i / stepCount
        cx = curX + round((x - curX) * ratio)
        cy = curY + round((y - curY) * ratio)
        SetCursorPos(cx, cy)
        time.sleep(interval)
    time.sleep(waitTime)


_EXTENDED_KEYS = {
    Keys.VK_LEFT, Keys.VK_UP, Keys.VK_RIGHT, Keys.VK_DOWN,
    Keys.VK_PAUSE, Keys.VK_LWIN, Keys.VK_RWIN,
}
MAPVK_VK_TO_VSC = 0


def _key_flags(key: int) -> tuple[int, int]:
    """Return (scan_code, extra_flags) so the injected event looks like a physical key."""
    scan = _user32.MapVirtualKeyW(key, MAPVK_VK_TO_VSC)
    extra = KeyboardEventFlag.ExtendedKey if key in _EXTENDED_KEYS else 0
    return scan, extra


def PressKey(key: int, waitTime: float = OPERATION_WAIT_TIME) -> None:
    """Simulate a key down."""
    scan, extra = _key_flags(key)
    keybd_event(key, scan, KeyboardEventFlag.KeyDown | extra, 0)
    time.sleep(waitTime)


def ReleaseKey(key: int, waitTime: float = OPERATION_WAIT_TIME) -> None:
    """Simulate a key up."""
    scan, extra = _key_flags(key)
    keybd_event(key, scan, KeyboardEventFlag.KeyUp | extra, 0)
    time.sleep(waitTime)


def IsKeyPressed(key: int) -> bool:
    """Return True if the key is currently down (GetAsyncKeyState)."""
    state = _user32.GetAsyncKeyState(key)
    return bool(state & 0x8000)


def KeyWasPressed(key: int) -> bool:
    """True if the key is down now OR was pressed since the previous call (bit 0).

    Added for sts-game-mcp: catches keys whose down state is shorter than a
    polling interval. Bit 0 is reset by this call and by any other process
    that queries the same key, so it is a best-effort signal.
    """
    state = _user32.GetAsyncKeyState(key)
    return bool(state & 0x8001)
