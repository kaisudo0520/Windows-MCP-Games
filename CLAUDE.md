# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sts-game-mcp`: a stdio-only MCP server (package `game_mcp`, `src/` layout) that captures and drives exactly one bound Slay the Spire window on Windows 10. It is a heavily reduced derivative of CursorTouch/Windows-MCP @ `08ddee78`; only the MSS/Pillow screenshot backends and a handful of Win32 input primitives were vendored (`src/game_mcp/vendor/`, see `THIRD_PARTY_NOTICES.md`). Everything else (shell, filesystem, registry, UIA tree, HTTP transports, telemetry) is deliberately absent and must not be reintroduced.

Windows-only: imports `pywin32`, `ctypes.wintypes`, and `tkinter` (tests). Nothing here runs on Linux/macOS.

## Commands

Uses `uv` with Python 3.12 (`.python-version`). Always run through `uv run`.

```powershell
uv sync --extra dev                    # create .venv from uv.lock (pytest + ruff included)
uv run ruff check src tests            # lint (E/F/W, line-length 100, E501 ignored; vendor/ exempt)
uv run pytest -m "not integration"     # unit tests only; safe to run anywhere, uses fakes/monkeypatch
uv run pytest -m integration           # opens a tkinter probe window, moves the REAL cursor and clicks in it
uv run pytest -q -p no:cacheprovider   # full suite (last clean run: 80 passed)
uv run pytest tests/test_safety.py::test_tool_stop_and_resume   # single test
uv run sts-game-mcp --log-level DEBUG  # start the server (stdio; logs go to stderr)
uv run python -m game_mcp              # same entry point without the console script
```

Integration-test caveats:
- They steal foreground to their own probe window. Two focus-dependent tests `pytest.skip` if the terminal/IDE keeps focus. Do not touch the mouse or keyboard while they run.
- `test_stdio_server.py` launches the real server as a subprocess and asserts that exactly 9 tools are exposed. Adding or renaming a tool requires updating `EXPECTED_TOOLS` there and the tool table in `README.md`.
- `GAME_MCP_CORNER_FAILSAFE=0` is set in that test; a cursor parked at (0,0) would otherwise engage the operator stop.

## Branch `jev-bot`: the `sts_bot` package

This branch adds a second package, `src/sts_bot/`, that replaces screenshots and desktop clicks with the CommunicationMod protocol (the game launches `sts-bot run` as a subprocess; JSON states arrive on stdin, commands leave on stdout). `game_mcp` stays untouched and is reused for `StopLatch` / `OperatorWatcher` (Scroll Lock and top-left-corner emergency stop) via `game_mcp.safety`. Setup and protocol notes: `docs/BRIDGE.md`.

- **stdout is the wire.** Nothing in `sts_bot` may `print`; log to stderr. The `ready` handshake is written before anything slow (tk, threads) in `__main__.cmd_run`.
- **One gate for commands**: `dispatch.CommandDispatcher.check` (known verb -> latch not stopped -> `ready_for_command` -> verb in `available_commands`; `state`/`wait` are read-only and bypass the latter three). Everything sent or rejected is recorded to `events.jsonl` by `recorder.RunRecorder`.
- **Threading**: `Bridge.start_reader` reads stdin on a daemon thread and queues `Message`/`ProtocolError`/EOF; the owner of the main thread (tk `after` loop or `run_headless`) drains via `Bridge.process_pending`. All view and state mutation happens on that one thread.
- **Policies** implement `policy.Policy.decide(Snapshot) -> command | None`; only `NullPolicy` exists. Jev-backed policies go here later; keep them free of I/O to the mod so they stay replayable from `events.jsonl`.
- **Indices**: `CardView.index` is 1-based (matches `PLAY n`); `MonsterView.target_index` is 0-based (matches the mod's TargetIndex). Do not mix them up in a policy.
- Tests `tests/test_bot_*.py` run on plain Python 3.12+ with only pytest (in-memory `BytesIO` streams, no pywin32): `python -m pytest tests/test_bot_*.py -q`. `tests/conftest.py` adds `src/` to `sys.path`.

## Architecture (`game_mcp`, the MCP server)

Request path for every tool call: `server.py` (FastMCP, 9 tools, `GameSession` state) -> `validation.validate_window()` -> either `capture.capture_game_screen()` or `input_service.InputService`. All three of capture, bind and input take `GameSession.op_lock` non-blocking; a second concurrent call gets `BUSY` rather than waiting. Never issue an observation from pixels grabbed while an action is in flight.

Core invariants that shape the code (break any of these and the safety story in `README.md` "安全邊界" is false):

- **Observe-then-act with single-use ids.** `get_game_screen` issues one `Observation` (`observation.py` keeps only the latest). Every `click`/`move_mouse`/`press_key` must present that id; `ObservationStore.require()` rejects it with `OBSERVATION_STALE` + `details.reason` if it is missing, superseded, expired (TTL default 20 s), the client rect moved, the HWND changed, input already ran, or a stop happened. The id is invalidated *before* injection, so a failed action still consumes it.
- **Validation order is fixed** in `validation.py`: binding alive (HWND + PID + exe name + process create time, `binding.check_binding_alive`) -> not minimized -> not DWM-cloaked -> foreground -> all four client-rect corners on a monitor -> not occluded. The error code names the first failing check. Foreground is required for *capture* too, because MSS/Pillow grab a screen rectangle, not a window.
- **Occlusion is two-stage** (`win32.windows_above` z-order walk, then `win32.find_occluder` grid `WindowFromPoint` sampling). Opaque windows above -> `OCCLUDED`; `WS_EX_LAYERED|WS_EX_TRANSPARENT` overlays (Discord/Steam) are *reported* in `window.overlays`/`warnings`, not blocked. Capture re-runs full validation after the grab (`phase: post_capture`).
- **Input protocol** (`input_service.py` module docstring lists all 8 steps): precheck -> map image px -> client px -> screen px (`Observation.scale`, DPI) -> point-level hit test -> invalidate observation -> `_place_cursor` and confirm with `GetCursorPos` -> `_final_gate` (stop latch + foreground + target ownership) -> inject -> `_postcheck` producing `post_action.trustworthy`. `GAME_MCP_CLICK_SETTLE_MS` applies only to mouse actions (LibGDX needs a hover frame before the press). `press_key` runs the IME check/Shift-tap/re-check sequence; Shift is tool-internal and never whitelisted.
- **Stop latch has two origins** (`safety.py`). `stop_input` -> origin `tool`, cleared by `resume_input`. `OperatorWatcher` thread (30 ms poll of `GetAsyncKeyState` for Scroll Lock + cursor at (0,0)) -> origin `operator`, which upgrades a tool stop and cannot be cleared by any tool call unless `GAME_MCP_ALLOW_TOOL_RESUME_AFTER_OPERATOR_STOP=1`. Engaging the latch first calls `HeldInputs.release_all()`, which drops an entry only after its release succeeded.
- **The server never changes desktop state it does not own**: no `SetForegroundWindow`, no launching, no window moves. Tests may focus their own probe window only.

Supporting modules: `config.Settings` (frozen dataclass, every `GAME_MCP_*` env var read once at startup with clamped defaults; the README env table is the reference), `errors.ErrorCode` (the only error vocabulary; tools return `{ok:false, error:{code,message,details}}` via `error_response`), `win32.py` (all read-only Win32 queries plus DPI awareness, which `__main__` sets before anything else in the process).

## Conventions

- Structured errors only: raise `GameMcpError(ErrorCode.X, msg, details)`; tool wrappers in `server.py` convert them. Never let `pywintypes.error` escape (convert to `OSError` -> `WINDOW_GONE`, as `win32.client_rect_on_screen` does).
- New knobs go into `config.Settings` as `GAME_MCP_*` env vars with a conservative default, plus a row in the README env table and, if user-facing, `docs/mcp-config.example.json`.
- Unit tests monkeypatch `game_mcp.vendor.win32_input` (`ReleaseMouse`, `ReleaseKey`, `KeyWasPressed`) and `win32` functions rather than touching the desktop. Anything that moves the real cursor belongs in `test_integration_window.py` under `pytestmark = pytest.mark.integration`.
- Vendored files under `src/game_mcp/vendor/` are mechanical copies with a header listing every edit; keep changes there minimal and document them in that header and in `THIRD_PARTY_NOTICES.md` / `docs/CHANGES.md`.
- `docs/CHANGES.md` is the running code-review log (review findings -> fixes, deliberate design decisions). Update it when changing behaviour a reviewer would question. `docs/STATE_SOURCE_OPTIONS.md` is an unimplemented design discussion (CommunicationMod-based state source), not current behaviour.
- README and docs are written in Traditional Chinese. In `game_mcp`, code, docstrings and log messages are English. In `sts_bot`, identifiers stay English but docstrings, comments, UI text, log lines and rejection reasons are Traditional Chinese, because the operator reads them live; tests assert on those Chinese strings.
