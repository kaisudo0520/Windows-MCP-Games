# Phase D real-game input trial — summary for external review (Codex)

Date: 2026-09-07. Scope: first authorized real-input test against the actual
Slay the Spire window (not a probe/test window). This file is a standalone
handoff; `docs/CHANGES.md` has the full code-change log and `README.md` has
the user-facing tool/error reference.

## What was run

Driven through the real `game_mcp` MCP tools (in-process `FastMCP` client, not
a shell macro), one action per step, each preceded by `get_game_screen` and
followed by a fresh capture:

1. `bind_game` → hwnd 1640344, pid 28552, `javaw.exe`.
2. Main menu: `move_mouse` hover on "開始遊戲", then `click` it.
3. `click` through mode select ("標準模式") → character select (Ironclad,
   only unlocked class) → "啟程" (embark).
4. Act 1 map: `click` the first enemy node → battle start → tutorial pages
   1–3 (`click` "下一條" / "我準備好了！").
5. Battle: 4 turns of "click card → click enemy" (Strike/Bash) and
   "click card → click empty field" (Defend, no target needed), `press_key
   e` to end each turn.
6. Cultist defeated (54 → 0 HP). Run stopped at the reward screen
   ("太好了！ 15 金幣 / 將1張卡牌加入牌組中 / 略過獎勵"). **No reward was
   claimed** — the tool never called anything past the last `get_game_screen`
   of that screen, per the task's phase-D scope (stop after the first fight).

Every action's response had `post_action.trustworthy: true`. Steady-state
tool-side timing: capture 80–150 ms, click ~90 ms (`injection_window`), key
~55 ms. 75 PNGs from this run are saved under `captures/` (`d1_*.png` …
`d41_*.png`; not all indices are actions — some are read-only `get_game_screen`
checkpoints).

## Two real-game bugs found and fixed during the trial

1. **LibGDX hover-then-click**: the very first click on the tutorial "下一條"
   arrow was silently ignored — the engine evaluates hover state and the
   click on separate frames, and the cursor landed on the same frame as the
   button-down. Fix: `GAME_MCP_CLICK_SETTLE_MS` (default 60 ms) sleep between
   `SetCursorPos` and the button event, added in `input_service.py`. Retested
   immediately after: the same arrow click succeeded on the first try.

2. **zh-TW IME swallowed keys**: `press_key e` (end turn) was sent, `trustworthy:
   true` came back, but the game did not end the turn — the game window's IME
   was open in native (Chinese) conversion mode, so the injected VK_E arrived
   as `VK_PROCESSKEY`. Fix: `win32.ime_state()` (read-only `WM_IME_CONTROL`
   query via `SendMessageTimeoutW`); `press_key` now taps Shift once when
   native mode is detected (`GAME_MCP_IME_AUTOTOGGLE`, default on), re-queries,
   sends the real key, and reports `IME_ACTIVE` if still native. Shift is a
   tool-internal remediation step, not an AI-selectable key — it never appears
   in `key_whitelist`.

   This fix was verified twice against the real game: once naturally (user
   had switched to English input, `press_key e` worked directly, `ime.
   native_open: false`), and once by deliberately forcing the game's IME back
   into native mode (a Shift tap sent directly, outside the tool) and then
   calling `press_key e` through the tool — it detected `native_open: true`,
   tapped Shift itself, re-checked (`native_open: false`), and delivered the
   key; response showed `"ime": {"shift_tapped": true, "native_open": false}`.

## Environment note, not a defect

Discord's overlay window (layered + hit-test-transparent, `Chrome_WidgetWin_1`
class) sat over the game's client rect for part of this session. It never
blocked capture — it was reported in `warnings` / `window.overlays` and the
image was still returned. Once Discord was closed, `overlays`/`warnings` were
empty on the next capture. This is the intended behavior from the earlier
review round (transparent overlays are reported, not blocking).

## One incident during the session, confirmed harmless

A shell `for` loop used to script several steps at once had a bash `set --`
argument-splitting bug and produced one call with the wrong y-coordinate and
an 800-second sleep. It was killed via `TaskStop` before it fired. The next
`get_game_screen` confirmed the game state was unaffected (energy and enemy
HP unchanged from before the loop). This was a driver-script bug in the
throwaway test harness (`scratchpad/sts_step.py`), not in `game_mcp` itself —
flagging it so a reviewer doesn't go looking for a corresponding code path.

## Test suite status — needs a clean rerun, not yet re-confirmed

The last **clean** full run, right after the IME fixes and their dedicated
unit tests (`tests/test_debug_and_ime.py`) landed, was:

```
uv run ruff check src tests    -> All checks passed!
uv run pytest -q                -> 80 passed in 25.80s
```

A rerun immediately before this summary was written showed
`1 failed, 77 passed, 2 skipped` — `tests/test_integration_window.py::
test_capture_refused_when_occluded` failed, and two focus-dependent tests
skipped (their fixture calls `pytest.skip` when it cannot steal foreground
from the current terminal/IDE window, which is expected when the test is
run while another window — like the terminal itself — is actively focused).
This was not investigated further this session (interrupted by the user
before diagnosis). **Please have Codex — or a human — rerun the suite with
no other window stealing focus during the run** (`uv run pytest -q -p
no:cacheprovider`) and treat a repeat of this specific failure as a real
regression to root-cause, not as the same class of focus flakiness as the
skips.

## What Codex should specifically confirm

- `src/game_mcp/input_service.py`: the IME check/tap/re-check sequence in
  `press_key`, and that `GAME_MCP_CLICK_SETTLE_MS` is applied only to mouse
  actions (`_mouse_action`), not `press_key`.
- `src/game_mcp/win32.py`: `ime_state()` — timeout handling
  (`SendMessageTimeoutW` with `SMTO_ABORTIFHUNG`, 200 ms) if the IME window
  is unresponsive; correctness of `IME_CMODE_NATIVE` bit test.
- `src/game_mcp/capture.py`: `prune_debug_dir()` retention logic (count- and
  age-based), called on every debug dump and once at server start.
- The full suite pass/fail state described above (rerun with focus not
  contended).
- Whether "no reward claimed, run left open at the reward screen" is
  correctly the point where this phase stopped — no further tool calls were
  made after the last capture of that screen.
