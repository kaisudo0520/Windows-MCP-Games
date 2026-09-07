"""``sts-state-mcp``: a tiny read-only MCP server for advisor mode.

Serves the game state written by ``sts-state-dump`` (``latest.json`` / ``latest.meta.json``
in the dump directory). Two read-only tools, no screenshot, no input, no other tools.
Completely independent of the desktop-input server (``game_mcp.server``); it imports
nothing from it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from game_mcp import __version__
from game_mcp.bridge import request
from game_mcp.bridge.compact import compact_state
from game_mcp.bridge.persist import LATEST_NAME, META_NAME, default_state_dir

logger = logging.getLogger(__name__)

_TORN_READ_ATTEMPTS = 5
_TORN_READ_SLEEP_S = 0.02
_POLL_S = 0.25
_REFRESH_TIMEOUT_S = 3.0
_REFRESH_POLL_S = 0.05
DEBUG_INTENT = "DEBUG"

INSTRUCTIONS = """\
Read-only view of Slay the Spire state for advisor mode. A separate dumper
(sts-state-dump) writes the game's state to disk whenever it is stable; this server
reads it. Use get_game_state for the current state, or wait_for_state(since_seq) to
block until a newer state appears. This server never touches the game; the human plays.
"""


def _ro(title: str) -> ToolAnnotations:
    # Same shape as game_mcp.server._ro, copied deliberately so this file imports nothing from it.
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


_ABSENT = "absent"
_FAILED = "failed"
_OK = "ok"


def _read_json(path: str) -> tuple[str, Any]:
    """Return ``(status, data)``: ``ok`` with the parsed value, ``absent`` when the file
    does not exist, or ``failed`` for a transient open/parse error (a writer may be
    swapping the file in, or a reader-side sharing violation on Windows)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return _OK, json.load(fh)
    except FileNotFoundError:
        return _ABSENT, None
    except (OSError, ValueError) as exc:
        logger.debug("transient read failure for %s: %s", path, exc)
        return _FAILED, None


def _meta_seq(meta: Any) -> int | None:
    seq = meta.get("seq") if isinstance(meta, dict) else None
    return seq if isinstance(seq, int) else None


def _meta_stamp(meta: Any) -> float | None:
    stamp = meta.get("received_unix") if isinstance(meta, dict) else None
    return float(stamp) if isinstance(stamp, (int, float)) else None


def _valid_latest(latest: Any) -> bool:
    return isinstance(latest, dict) and "state" in latest and isinstance(latest.get("seq"), int)


def _available(latest: dict[str, Any]) -> dict[str, Any]:
    received_unix = _meta_stamp(latest)
    age_ms = round((time.time() - received_unix) * 1000, 2) if received_unix is not None else None
    return {
        "available": True,
        "seq": latest["seq"],
        "received_wall": latest.get("received_wall"),
        "received_unix": received_unix,
        "age_ms": age_ms,
        "state": latest["state"],
    }


def read_state(out_dir: str) -> dict[str, Any]:
    """Return the current state shape, retrying transiently failed reads.

    ``latest.json`` is self-contained and swapped in atomically, so any successful parse
    is a consistent snapshot; ``latest.meta.json`` is only the cheap change signal used by
    :func:`wait_for_state`. A transient open/parse failure (the writer is swapping the
    file in, or a Windows sharing violation) is retried briefly; an absent file is final
    (``no_state_yet``). Never raises.
    """
    if not os.path.isdir(out_dir):
        return {"available": False, "reason": "no_state_dir"}
    latest_path = os.path.join(out_dir, LATEST_NAME)
    for _ in range(_TORN_READ_ATTEMPTS):
        status, latest = _read_json(latest_path)
        if status == _ABSENT:
            return {"available": False, "reason": "no_state_yet"}
        if status == _OK and _valid_latest(latest):
            return _available(latest)
        time.sleep(_TORN_READ_SLEEP_S)  # mid-swap or sharing violation; try again
    return {"available": False, "reason": "unreadable"}


def _is_newer(meta: Any, since_seq: int, baseline_stamp: float | None) -> bool:
    """A state counts as new when its seq passed ``since_seq`` or, if the dump was
    restarted and the seq did not advance, when its timestamp differs from the one
    seen at the start of the wait."""
    seq = _meta_seq(meta)
    if seq is None:
        return False
    if seq > since_seq:
        return True
    stamp = _meta_stamp(meta)
    return baseline_stamp is not None and stamp is not None and stamp != baseline_stamp


def wait_for_state(out_dir: str, since_seq: int, timeout_s: float, *, poll_s: float = _POLL_S) -> dict[str, Any]:
    """Poll until a state newer than ``since_seq`` exists, then return read_state + timed_out."""
    timeout = max(1.0, min(600.0, float(timeout_s)))
    deadline = time.monotonic() + timeout
    meta_path = os.path.join(out_dir, META_NAME)
    baseline_stamp = _meta_stamp(_read_json(meta_path)[1])
    while True:
        _, meta = _read_json(meta_path)
        if _is_newer(meta, since_seq, baseline_stamp):
            return {**read_state(out_dir), "timed_out": False}
        if time.monotonic() >= deadline:
            return {**read_state(out_dir), "timed_out": True}
        time.sleep(poll_s)


def needs_intent_refresh(state: Any) -> bool:
    """True when a combat state still carries the placeholder ``DEBUG`` intent.

    CommunicationMod pushes the first state of a combat before the monsters' intents
    are created; a ``STATE`` re-send afterwards returns the real intents.
    """
    game_state = state.get("game_state") if isinstance(state, dict) else None
    combat = game_state.get("combat_state") if isinstance(game_state, dict) else None
    monsters = combat.get("monsters") if isinstance(combat, dict) else None
    if not isinstance(monsters, list):
        return False
    return any(isinstance(m, dict) and not m.get("is_gone") and m.get("intent") == DEBUG_INTENT for m in monsters)


def refresh_state(out_dir: str, *, reason: str, timeout_s: float = _REFRESH_TIMEOUT_S, poll_s: float = _REFRESH_POLL_S) -> dict[str, Any]:
    """Ask the dump to send ``STATE`` and wait for the resulting push.

    Returns the ``read_state`` shape plus ``refresh: {requested, reason, resolved}``.
    ``resolved`` is False when no new push arrived in time (dump not running, mod not
    ready); the request marker is then removed so it cannot fire later by surprise.
    """
    baseline = _read_json(os.path.join(out_dir, META_NAME))[1]
    since_seq = _meta_seq(baseline) if _meta_seq(baseline) is not None else -1
    baseline_stamp = _meta_stamp(baseline)
    requested = request.place_request(out_dir)
    deadline = time.monotonic() + max(0.2, min(30.0, float(timeout_s)))
    resolved = False
    while requested and time.monotonic() < deadline:
        _, meta = _read_json(os.path.join(out_dir, META_NAME))
        if _is_newer(meta, since_seq, baseline_stamp):
            resolved = True
            break
        time.sleep(poll_s)
    if not resolved:
        request.clear_request(out_dir)
    return {**read_state(out_dir), "refresh": {"requested": requested, "reason": reason, "resolved": resolved}}


VIEWS = ("compact", "full")


def apply_view(result: dict[str, Any], view: str) -> dict[str, Any]:
    """Project ``result["state"]`` for the requested view (``compact`` by default)."""
    chosen = view if view in VIEWS else "compact"
    if chosen == "full" or not result.get("available"):
        return {**result, "view": chosen}
    return {**result, "view": chosen, "state": compact_state(result.get("state"))}


def get_state(out_dir: str, *, refresh: bool = False, view: str = "compact") -> dict[str, Any]:
    """``read_state`` with an automatic ``STATE`` re-send when intents are still DEBUG,
    or an explicit one when ``refresh`` is True, projected to ``view``."""
    if refresh:
        return apply_view(refresh_state(out_dir, reason="explicit"), view)
    current = read_state(out_dir)
    if current.get("available") and needs_intent_refresh(current.get("state")):
        return apply_view(refresh_state(out_dir, reason="intent_debug"), view)
    return apply_view(current, view)


def build_server(out_dir: str) -> FastMCP:
    mcp = FastMCP(name="sts-state-mcp", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(
        name="get_game_state",
        description="Return the latest Slay the Spire state written by sts-state-dump. Read-only; no screenshot, no input. Default view='compact' (~1-2 KB): screen, floor, hp, gold, relics/potions by name, and in combat the hand (i = 1-based screen order, name, cost, type, needs_target), living monsters (i, hp, block, intent, damage x hits, powers), pile counts. view='full' returns the raw mod JSON (8-13 KB) — only use it when a compact field is missing. Returns {available, view, seq, received_wall, received_unix, age_ms, state}, or {available:false, reason}. If a combat state still shows intent DEBUG (the mod's first push of a fight precedes intent creation) the server asks the mod to re-send once automatically; refresh=true forces a re-send. 'refresh' reports whether that happened. Call once per decision; do not poll.",
        annotations=_ro("get_game_state"),
    )
    def get_game_state(refresh: bool = False, view: str = "compact") -> dict[str, Any]:
        return get_state(out_dir, refresh=refresh, view=view)

    @mcp.tool(
        name="wait_for_state",
        description="Block until the dump writes a state newer than since_seq (seq > since_seq, or a changed received_unix after a dump restart; polled ~250ms), up to timeout_s seconds (default 55, clamped 1..600). Returns the get_game_state shape (same view parameter, default compact) plus timed_out. On timed_out:true, simply call again with the same since_seq. Read-only; never touches the game.",
        annotations=_ro("wait_for_state"),
    )
    def wait_for_state_tool(since_seq: int, timeout_s: float = 55, view: str = "compact") -> dict[str, Any]:
        return apply_view(wait_for_state(out_dir, since_seq, timeout_s), view)

    return mcp


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sts-state-mcp", description="Read-only advisor-mode state MCP server (stdio).")
    parser.add_argument("--out", default=os.environ.get("GAME_MCP_STATE_DIR") or default_state_dir())
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"sts-state-mcp {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("sts-state-mcp reading state from %s", args.out)
    mcp = build_server(args.out)
    mcp.run(transport="stdio", show_banner=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
