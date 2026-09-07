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

## Advisor mode (state dump) round 1

A new, self-contained path that does **not** touch the game. CommunicationMod pushes game
state; a one-way dump writes it to disk; a small read-only MCP server serves it to an AI
advisor; a human plays. The desktop-input server (`server.py`, 9 tools) is unchanged — this
round adds only the `game_mcp.bridge` package and two console scripts, and touches no
existing module (`server.py` / `capture.py` / `config.py` have no diff vs HEAD). See
`docs/COMMUNICATIONMOD_PROTOCOL.md` for the verified mod protocol and a sample
`config.properties`.

New files:

| File | Responsibility |
|---|---|
| `bridge/protocol.py` | Outbound allowlist (`write_line`, `ALLOWED_OUTBOUND = {"ready", "STATE"}` — handshake and the read-only re-send query, never an action), inbound line parsing, immutable `Snapshot`, `SnapshotStore` (seq continues from disk). |
| `bridge/request.py` | `state.request` marker in the dump dir: the MCP server places it, the dump's `RequestWatcher` thread consumes it and writes `STATE` (coalesced, min 250 ms apart). |
| `bridge/persist.py` | Atomic `latest.json` + sidecar `latest.meta.json` (`{seq, received_wall, received_unix}`) writes (temp + `os.replace`), `--record-samples` writer, `%LOCALAPPDATA%` default dir. |
| `bridge/__main__.py` | `sts-state-dump`: print `ready` once, read stdin, record + persist each push, archive samples; stderr + rotating `bridge.log` in the out dir; EOF exits 0. |
| `bridge/mcp_server.py` | `sts-state-mcp`: independent read-only FastMCP server with `get_game_state` and `wait_for_state` reading the dump dir; imports nothing from `server.py`. |

Changed: `pyproject.toml` adds `sts-state-dump` and `sts-state-mcp` console scripts. New
config examples `docs/codex-config.advisor.example.toml` (Codex CLI, `~/.codex/config.toml`)
and `docs/claude-desktop-config.advisor.example.json` (Claude Desktop) point at `sts-state-mcp`.

Tests (all non-integration): `test_bridge_protocol.py`, `test_bridge_persist.py`,
`test_state_mcp.py`. Existing tests untouched.

The one safety property: the dump may write only `ready` and `STATE` to the game (asserted in
`test_bridge_protocol.py` / `test_state_refresh.py`); any other outbound string raises. `STATE`
only makes the mod re-send the current state (`CommandExecutor.executeStateCommand` sets
`mustSendGameState = true`, nothing else), so the dump still cannot act on the game.

Deliberate decisions a reviewer may question:

- **Read-only sensor.** The allowlist is `{"ready", "STATE"}`; no action command exists.
  `STATE` was added in round 3 because the mod's first push of every combat precedes intent
  creation (`intent: "DEBUG"`); the MCP server requests one re-send in that case.
- **Two files, `latest.json` is the truth.** `latest.json` is self-contained and swapped in
  atomically, so any successful parse is a consistent snapshot; the reader serves it directly
  and only retries transient open/parse failures. `latest.meta.json` is written *after* a
  successful `latest.json` write (so it never leads the state on disk) and exists only as the
  cheap change signal that `wait_for_state` polls.
- **`wait_for_state` stays short.** Default `timeout_s` is 55 s (clamped 1..600) so a single
  call fits comfortably under an unknown client tool-call timeout (e.g. Claude Desktop); on
  `timed_out: true` the caller just calls again with the same `since_seq`.
- **Shared dir via env.** Both the dump (`--out`) and the MCP server (`--out` /
  `GAME_MCP_STATE_DIR`) default to `%LOCALAPPDATA%\sts-game-mcp\state`; setting
  `GAME_MCP_STATE_DIR` moves both together.

### Advisor mode round 2 (Opus review, 2026-09-07) and what changed

| Finding | Fix |
|---|---|
| stdin decoded with the locale codec, no error handling: a charset mismatch with the game JVM (which writes `getBytes()` with its default charset) raised `UnicodeDecodeError` and killed the dump; the mod does not restart it | `protocol.LineDecoder`: stdin read as bytes (`sys.stdin.buffer`), strict UTF-8 first, then the locale code page with replacement characters (warned once); `--stdin-encoding CODEC` forces one. Undecodable bytes are skipped, never fatal |
| `os.replace` over `latest.json` fails with `PermissionError` (WinError 5) while the reader holds it open; neither side retried (measured ~2.6% reader failures under stress) | Writer retries the replace 10x with 15 ms backoff; reader retries transient open/parse failures 5x20 ms and distinguishes them from an absent file |
| `latest.meta.json` was written even when `latest.json` failed, so meta led the state and the reader reported `inconsistent` instead of the last good state | Meta is written only after a successful state write; the reader no longer requires meta/state seq equality at all (see above) |
| Reader starved under rapid pushes (the meta==state seq check almost never held with a busy writer; found by the new stress test) | Reader serves any parseable `latest.json`; reasons are now `no_state_dir` / `no_state_yet` / `unreadable` |
| `seq` restarted at 1 with every dump process, so after a game restart a `wait_for_state(since_seq=N)` caller never resolved | Dump continues from the `seq` in an existing `latest.meta.json`; `wait_for_state` also wakes when `received_unix` changes with the same seq; both tools return `received_unix` |
| Codex example config was JSON, but Codex CLI configures MCP servers in TOML | `docs/codex-config.advisor.example.toml` replaces `docs/mcp-config.advisor.example.json` |
| Stale `.dump-*.tmp` after a hard kill; corrupt `latest.json` swallowed silently | Startup sweep of temp files; transient/corrupt reads logged at DEBUG |
| (user request) `--record-samples` grew without bound | `persist.prune_samples`: newest `--samples-keep` (default 500) kept after every write, files older than `--samples-max-age-h` (default 72) deleted at startup, same policy as `GAME_MCP_DEBUG_*` for screenshots |
| (first real run, 2026-09-07) The mod pushes cp950 bytes (`"name":"` `a5 b4 c0 bb` = 打擊; game JRE 1.8.0_144 reports `file.encoding=MS950`), so every push hits the locale fallback. Names decoded correctly (verified by code point); an earlier "garbled" reading was a terminal display artefact | The fallback warning logs the hex around the first undecodable byte; the fallback now decodes with both lenient UTF-8 and the locale codec and keeps the result with fewer U+FFFD, so one stray byte in an otherwise UTF-8 line cannot wipe every CJK name |

Tests: `tests/test_bridge_review_fixes.py` (decoder fallback, replace retry, meta ordering,
seq continuity across two dump runs, transient open failure retry, 1.5 s concurrent
writer/reader stress asserting no torn state, timestamp-change wake-up).

### Advisor mode round 3 (real-game finding, 2026-09-07)

| Finding | Fix |
|---|---|
| Every combat's first push (turn 1, before the player acts) carried `intent: "DEBUG"` and `move_adjusted_damage: -1` for all monsters; nothing re-pushed until the player acted, so the advisor read placeholder intents (samples seq 8/22/28/34/50) | Allowlist gains `STATE` (verified in `CommandExecutor.java`: only sets `mustSendGameState`, available at any time, consumes no turn). `bridge/request.py`: the MCP server drops `state.request` in the dump dir, the dump's watcher thread sends `STATE`, the mod re-sends with live intents. `get_game_state` auto-requests once when a live monster still shows `DEBUG` (or on `refresh=true`), waits up to 3 s, and reports `refresh: {requested, reason, resolved}`; on timeout the marker is removed and the last state is served |

Tests: `tests/test_state_refresh.py` (allowlist accepts `STATE` only in exact form, watcher
sends one `STATE` per marker and ignores marker content, dump end-to-end answers a request,
auto-refresh on DEBUG, no request without DEBUG, timeout clears the marker, explicit refresh).

### Advisor mode round 4 (token cost, 2026-09-07)

| Finding | Fix |
|---|---|
| The mod's state is 8–13 KB per push (full deck with UUIDs, whole map, every relic/potion field, piles verbatim); the user measured 1–2% of a 5-hour Codex quota per game turn | `bridge/compact.py`: pure projection `compact_state()` (no mutation, never raises on odd shapes). `get_game_state` / `wait_for_state` gain `view` (`compact` default, `full` = raw). Compact keeps screen, floor/act/class/ascension, hp, gold, act boss, relics (name + counter), potions (empty slots dropped), deck size; in combat the hand (1-based screen index, name, id, cost, type, needs_target, upgrades, playable/exhausts/ethereal only when relevant), living monsters (index, hp, block, intent, damage x hits, powers), pile name counts; out of combat the deck as name counts; MAP only `next_nodes`; EVENT text capped at 600 chars; card lists elsewhere compacted. Measured on 70 real samples: combat 12.6 KB → 1.0 KB (8%), map/event/reward screens 4–7% |

Tests: `tests/test_compact.py`.

Follow-up: the mod's player object has no Watcher stance (verified in `GameStateConverter.java`:
hp/block/energy/powers/orbs only), so `compact_state` adds `player.stance = "unknown (...)"` when
`class == "WATCHER"` so the advisor asks instead of assuming Neutral.

## Deliberate design decisions a reviewer may question

- Foreground is required for **capture**, not only for input, because a screen-rectangle grab of a background window would include whatever is drawn on top.
- The tool never calls `SetForegroundWindow`; tests do, only on their own probe windows.
- Operator emergency stop is polled with `GetAsyncKeyState`/`GetCursorPos`, not a low-level hook, to avoid a message-pump thread and hook-related instability. Default key is Scroll Lock because Pause emits make+break together on PC keyboards.
- Z-order overlap is checked before the hit-test grid; hit-test transparent overlays are reported rather than blocked because Discord/Steam overlays are permanently present on many machines.
- `escape` is excluded from the default key whitelist; the AI can still reach menus by clicking, so this is a speed bump, not a guarantee.
- No drag tool in v1 (card play in Slay the Spire works as click card → click target).
- Observation TTL default 20 s; AI round trips are typically longer than the game's animation time, so the TTL is deliberately generous and geometry/focus are re-checked anyway.
