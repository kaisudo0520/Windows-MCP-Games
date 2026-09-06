# Third-party notices

## Windows-MCP (upstream)

- Repository: https://github.com/CursorTouch/Windows-MCP
- Source commit: `08ddee78c26182b103d62c1c84c1fbec82a280b2` (2026-09-06, "Update dependencies (#406)"), version 0.8.5
- License: MIT, Copyright (c) 2025 JEOMON GEORGE (full text reproduced in `LICENSE.md`)

Files derived from upstream (each carries a header stating its origin and the
modifications made):

| This project | Upstream file | What was kept / changed |
|---|---|---|
| `src/game_mcp/vendor/screenshot.py` | `src/windows_mcp/desktop/screenshot.py` | Backend registry, MSS backend, Pillow backend, `_is_usable_capture`, `capture()` chain. Removed the dxcam backend; replaced `windows_mcp.uia.Rect` / `GetVirtualScreenRect` with a local `Rect` and a ctypes `GetSystemMetrics` call. |
| `src/game_mcp/vendor/win32_input.py` | `src/windows_mcp/uia/core.py`, `src/windows_mcp/uia/enums.py` | `SetCursorPos`, `GetCursorPos`, `mouse_event`, `keybd_event`, `GetScreenSize`, `Click`, `PressMouse`, `ReleaseMouse`, `PressKey`, `ReleaseKey`, `IsKeyPressed`, `MoveToDuration`, `MouseEventFlag`, `KeyboardEventFlag`, and the virtual-key constants used by this project. |

Everything else in upstream (UI Automation tree, PowerShell, filesystem,
registry, process, clipboard, scrape, notifications, HTTP transports, OAuth,
PostHog telemetry) is intentionally **not** included. The upstream telemetry
module is absent, so no PostHog client is ever created.

## uiautomation for Python 3

`src/windows_mcp/uia/core.py` and `enums.py` in upstream are themselves derived
from "uiautomation for Python 3" by yinkaisheng
(https://github.com/yinkaisheng/Python-UIAutomation-for-Windows), distributed
under the Apache License 2.0. The Win32 input wrappers vendored into
`src/game_mcp/vendor/win32_input.py` fall under that license. A copy of the
Apache License 2.0 is available at https://www.apache.org/licenses/LICENSE-2.0.

## Runtime dependencies

fastmcp (Apache-2.0), mss (MIT), Pillow (MIT-CMU), pywin32 (PSF), psutil (BSD-3).
