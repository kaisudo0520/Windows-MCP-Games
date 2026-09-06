# Change summary (for code review)

Upstream: CursorTouch/Windows-MCP @ `08ddee78c26182b103d62c1c84c1fbec82a280b2` (v0.8.5, MIT).
This project is a new package `game_mcp` that vendors two upstream pieces and adds a
game-scoped policy layer on top. Nothing from upstream's tool registry, UIA tree,
PowerShell/filesystem/registry/process/clipboard/scrape/notification services,
HTTP transports, OAuth, or PostHog telemetry is present.

## Vendored (mechanical copies with documented edits)

| File | From | Edits |
|---|---|---|
| `src/game_mcp/vendor/screenshot.py` | `desktop/screenshot.py` | dxcam backend removed; `uia.Rect`/`GetVirtualScreenRect` replaced with local ctypes; env var renamed `GAME_MCP_SCREENSHOT_BACKEND`; `mss.MSS` preferred over deprecated `mss.mss`. |
| `src/game_mcp/vendor/win32_input.py` | `uia/core.py`, `uia/enums.py` | Only `SetCursorPos/GetCursorPos/mouse_event/keybd_event/GetScreenSize/Click/PressMouse/ReleaseMouse/MoveToDuration/PressKey/ReleaseKey/IsKeyPressed` + flag enums + VK subset. `Click`/`PressMouse`/`ReleaseMouse` take a `button`; `PressKey`/`ReleaseKey` send a real scan code via `MapVirtualKeyW` and set `KEYEVENTF_EXTENDEDKEY` only for extended keys (upstream sent scan 0 + extended for everything). |

## New modules

| File | Responsibility |
|---|---|
| `errors.py` | `ErrorCode` enum, `GameMcpError`, `error_response()`. |
| `config.py` | `Settings` frozen dataclass from `GAME_MCP_*` env vars. |
| `win32.py` | DPI awareness (PMv2 → PM → system), read-only window queries, DWM cloak check, monitor containment, grid occlusion sampling via `WindowFromPoint` + `GetAncestor(GA_ROOT)`. |
| `binding.py` | Candidate discovery (visible top-level windows filtered by process name and title/path), `Binding` with PID + exe + create_time, `check_binding_alive()` (PID reuse detection). |
| `validation.py` | Ordered checks: alive → minimized → cloaked → foreground → on-screen → occlusion; returns `WindowState`. |
| `observation.py` | Single current `Observation`; `require()` raises `OBSERVATION_STALE` with a reason. |
| `capture.py` | validate → vendor capture of client rect → size check → post-validate → fit/encode (png/jpeg) → issue observation → optional debug dump; per-stage timings. |
| `safety.py` | `HeldInputs` (release-all), `StopLatch` (tool vs operator origin, operator stop not tool-resumable by default), `OperatorWatcher` thread (Scroll Lock key / top-left corner polling, upgrades a tool stop to an operator stop). |
| `input_service.py` | `click`, `move_mouse`, `press_key` with the full protocol (precheck, point hit-test, invalidate observation, final focus gate, inject, post-verify, timings). One action at a time (`BUSY`). |
| `server.py` | FastMCP with exactly 9 tools; `GameSession` holds state; `get_game_screen` returns `ToolResult([ImageContent, TextContent], structured_content=meta)`. |
| `__main__.py` | argparse CLI, stderr logging, DPI awareness before anything else, stdio only, releases held inputs on exit. |

## Tests

- `tests/test_observation.py`, `test_safety.py`, `test_input_logic.py`, `test_operator_watcher.py`, `test_review_fixes.py` (unit, fakes).
- `tests/test_integration_window.py` (marked `integration`): tkinter probe window; 25 boundary scenarios.
- `tests/test_stdio_server.py` (marked `integration`): real subprocess over stdio.

## Review round 1 (Fable subagent + Codex, 2026-09-07) and what changed

| Finding | Fix |
|---|---|
| Operator stop dropped while a tool stop was active | `OperatorWatcher.poll()` engages whenever origin != operator; `engage()` upgrades tool -> operator |
| Stop latch not re-checked at injection | `_final_gate` checks latch + foreground + target ownership right before injecting |
| Post-grab validation skipped occlusion | post-validate runs with `check_occlusion=True`; errors carry `phase: post_capture` |
| `pywintypes.error` escaped as generic error | `win32.client_rect_on_screen` / `window_rect` convert it to `OSError` -> `WINDOW_GONE` |
| Pause key has no sustained down state | default key is Scroll Lock; watcher uses `KeyWasPressed` (bit 0 + bit 15) |
| Capture/bind not serialized with actions; observation ignored HWND | one `op_lock` shared by capture, bind, input (`BUSY` otherwise); `ObservationStore.require(hwnd_now=)` |
| `WindowFromPoint` blind to disabled / hit-test-transparent windows | `win32.windows_above` z-order walk: opaque -> `OCCLUDED (z_order)`, transparent -> reported in `window.overlays` + `warnings` |
| Click did not confirm cursor placement; hover crossed other windows; no post hit-test | `_place_cursor` verifies `GetCursorPos`; hover jumps directly; `post_action.target_still_ours` + `trustworthy` |
| `release_all` cleared state before releasing | entries removed only after a successful release; failures listed in `failed` |
| Unknown emergency key silently fell back | `OperatorWatcher` raises `ValueError`; server exits 2 |
| Invalid `button` misreported | new `INVALID_ARGUMENT` code |
| Tests did not prove pixel content / z-order / post-capture / lock | added in `tests/test_integration_window.py` and `tests/test_review_fixes.py` |

## Real-game input trial (phase D, 2026-09-07) and what it changed

- Menu navigation, map node, tutorial pages, card play (click card -> click enemy / click field) and `E` end-turn all worked through the real tools.
- LibGDX hitboxes evaluate hover and click per frame: a click on the tutorial arrow was ignored once when cursor placement and button-down landed in the same frame. Fix: `GAME_MCP_CLICK_SETTLE_MS` (default 60) pause between placing the cursor and pressing.
- The zh-TW IME was open in native mode on the game thread, so injected letter keys became `VK_PROCESSKEY`. Fix: `win32.ime_state()` (read-only `WM_IME_CONTROL` queries with `SendMessageTimeoutW`), `press_key` taps Shift once when native mode is detected (`GAME_MCP_IME_AUTOTOGGLE`), re-queries, and reports `IME_ACTIVE` if still native. Shift is a tool-internal remediation, never an AI-selectable key.
- Debug images: `GAME_MCP_DEBUG_DIR` now has retention (`prune_debug_dir`): newest `GAME_MCP_DEBUG_KEEP_FILES` kept after each dump, files older than `GAME_MCP_DEBUG_MAX_AGE_H` deleted at server start. Project `captures/` is git-ignored.

Not fixed (documented instead): held inputs are not released if the process is killed with `TerminateProcess`; `_abs_coords` normalizes to the primary monitor (harmless because `MOUSEEVENTF_MOVE` is never set).

## Deliberate design decisions a reviewer may question

- Foreground is required for **capture**, not only for input, because a screen-rectangle grab of a background window would include whatever is drawn on top.
- The tool never calls `SetForegroundWindow`; tests do, only on their own probe windows.
- Operator emergency stop is polled with `GetAsyncKeyState`/`GetCursorPos`, not a low-level hook, to avoid a message-pump thread and hook-related instability. Default key is Scroll Lock because Pause emits make+break together on PC keyboards.
- Z-order overlap is checked before the hit-test grid; hit-test transparent overlays are reported rather than blocked because Discord/Steam overlays are permanently present on many machines.
- `escape` is excluded from the default key whitelist; the AI can still reach menus by clicking, so this is a speed bump, not a guarantee.
- No drag tool in v1 (card play in Slay the Spire works as click card → click target).
- Observation TTL default 20 s; AI round trips are typically longer than the game's animation time, so the TTL is deliberately generous and geometry/focus are re-checked anyway.
