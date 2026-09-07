# CommunicationMod protocol (verified facts)

Source of truth: <https://github.com/ForgottenArbiter/CommunicationMod> — README and
`src/main/java/communicationmod/CommunicationMod.java`. Verified 2026-09-07.

This records only what **advisor mode** depends on. In advisor mode the mod is a pure
one-way sensor: it pushes game state, our `sts-state-dump` writes it to disk, and a human
plays while an AI advisor reads the file. Nothing we run sends a command to the game.

## Dependencies

- ModTheSpire + BaseMod. The README does not pin a supported game version; this machine
  runs Slay the Spire v2.3.4 (2022-12-18) and needs on-machine confirmation that the mod
  loads. (Not attempted here — no game launch.)

## Config file (`config.properties`)

The mod stores its settings in a ModTheSpire SpireConfig properties file. The README points
at the ModTheSpire wiki for the directory and does not quote a path verbatim; the
conventional location on Windows is:

```
%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
```

(Confirm on-machine; the exact folder is whatever ModTheSpire's SpireConfig uses.)

Keys, verified from the Java source (`CommunicationMod.java`):

| Key | Java constant | Meaning | Default |
|---|---|---|---|
| `command` | `COMMAND_OPTION = "command"` | External program the mod launches | (none) |
| `runAtGameStart` | `GAME_START_OPTION = "runAtGameStart"` | Start the external process at game start | `false` |

`.properties` files escape `\` as `\\` and `:` as `\:` (see the README's own example
`command=python C\:\\Path\\To\\Script\\main.py`). A `command` that runs the dump through the
project venv and archives every push as a sample while playing manually:

```properties
# %LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
command=N\:\\projects\\github\\Windows-MCP-Game\\.venv\\Scripts\\sts-state-dump.exe --record-samples N\:\\projects\\github\\Windows-MCP-Game\\captures\\state_samples
runAtGameStart=true
```

Unescaped, that command is:

```
N:\projects\github\Windows-MCP-Game\.venv\Scripts\sts-state-dump.exe --record-samples N:\projects\github\Windows-MCP-Game\captures\state_samples
```

`latest.json` / `latest.meta.json` are written to `--out` (default
`%LOCALAPPDATA%\sts-game-mcp\state`, overridable with the `GAME_MCP_STATE_DIR` env var). The
advisor MCP server (`sts-state-mcp`) reads from the same directory.

## Process launch and lifecycle (verified)

- **Working directory:** the README does not specify a working directory for the launched
  command. The dump therefore never relies on the current directory — its output path comes
  from `--out` (absolute) or the `%LOCALAPPDATA%` default, and the `command=` line above uses
  absolute paths. Give `--record-samples` an absolute path too.
- **Kill / restart:** the only documented termination is the handshake timeout — "If 'ready'
  is not received before a specified timeout, the external process will be terminated." The
  README documents no automatic restart on errors or on a game restart. So the dump prints
  `ready` immediately at startup (before reading anything) to avoid being killed, and it is
  fine for it to simply exit on stdin EOF.

## Handshake

> "After starting the external process, CommunicationMod waits for the process to send
> 'ready' on stdout. If 'ready' is not received before a specified timeout, the external
> process will be terminated." (README)

The README body says `ready`; the FAQ shows `"Ready\n"`. The Java handler
(`readMessageBlocking`) only checks that a first line arrived (`if (message == null)`); it
does **not** string-compare the content, so casing is not enforced. The dump sends lowercase
`ready\n` exactly once at startup (see `protocol.ALLOWED_OUTBOUND`). If a future mod version
enforces casing, this is the one line to revisit.

## State push format (verified)

> "Whenever the state of the game is determined to be stable (no longer changing without
> external input), CommunicationMod sends a message containing the JSON representation of
> the current game state to the external process's stdin." (README)

- **One line per push, newline-terminated.** "all messages are assumed to be ended by a new
  line '\n'"; each push is a single continuous JSON object with no literal newlines inside,
  so `one push = one line`. The dump reads stdin line by line accordingly.
- Pushes happen only when the state is stable (no animation / pending input); the dump stamps
  each snapshot with `received_unix` / `received_wall` so a reader can tell fresh from stale.

## Charset of the pushes (verified in `DataWriter.java`)

The mod writes each push with `message.getBytes()` **without a charset**, i.e. the game
JVM's default charset. On a zh-TW Windows box with the bundled Java 8 that is normally
cp950 (MS950); with `-Dfile.encoding=UTF-8` or a newer JVM it is UTF-8. Card, relic and
event names in the JSON are localized, so non-ASCII bytes do occur. The dump therefore
reads stdin as bytes and decodes each line with strict UTF-8 first; on failure it decodes
with both lenient UTF-8 and the locale code page and keeps the result with fewer
replacement characters (one warning in `bridge.log` with the hex around the bad byte); pass
`--stdin-encoding cp950` (or `utf-8`) to force a codec if names come out garbled.
Verified on this machine (2026-09-07): the pushes are MS950/cp950 (`a5 b4 c0 bb` = 打擊) and
decode correctly through the fallback.

## What the dump sends back

`ready` once at startup, and `STATE` whenever the MCP server places `state.request` in the
dump directory. `STATE` is a read-only query: `CommandExecutor.executeStateCommand()` only
sets `CommunicationMod.mustSendGameState = true`, so the mod re-sends the current state; it
is listed as available at any time and does not consume a turn (verified 2026-09-07 in
`CommandExecutor.java`). No action command (`PLAY` / `END` / `CHOOSE` / `CLICK` / `KEY` /
`POTION` / `START` / `PROCEED` / ...) exists. Every stdout write goes through
`protocol.write_line`, whose allowlist is `{"ready", "STATE"}`; any other string raises
`RuntimeError`. Asserted in `tests/test_bridge_protocol.py` and `tests/test_state_refresh.py`.

Why `STATE` is needed: the mod's first push of every combat happens before the monsters'
intents are created (`monster.intent` is still the `DEBUG` placeholder and `intentDmg` is -1),
and no further push happens until the player acts. A `STATE` re-send after that returns the
real intents; the MCP server requests it automatically.

## Deviations from the design brief

None affecting behaviour. The dump also honours `GAME_MCP_STATE_DIR` as the `--out` default
(not only the MCP server) so the two processes stay pointed at the same directory when the
env var is set; the documented default is unchanged when it is unset.
