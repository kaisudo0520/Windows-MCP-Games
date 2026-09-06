"""Entry point: ``sts-game-mcp`` (stdio only).

No HTTP transport, no autostart registration, no telemetry. Logs go to stderr
because stdout carries the MCP protocol.
"""

from __future__ import annotations

import argparse
import logging
import sys

from game_mcp import __version__, win32


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sts-game-mcp", description="Minimal MCP server bound to one Slay the Spire window (stdio).")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"sts-game-mcp {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Must happen before any window/screen query in this process.
    win32.ensure_dpi_awareness()

    from game_mcp.server import build_server

    mcp, session = build_server()
    try:
        session.start_watcher()
    except ValueError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2
    try:
        mcp.run(transport="stdio", show_banner=False)
    finally:
        session.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
