"""Advisor-mode state tooling for sts-game-mcp.

Two independent console programs live here:

* ``sts-state-dump`` (``game_mcp.bridge.__main__``) is launched by CommunicationMod
  inside the game. It is one-way: it prints ``ready`` once, reads the state JSON the
  mod pushes, and writes it to disk (``latest.json`` + ``latest.meta.json``). It never
  sends anything else to the game, so it cannot act on it.
* ``sts-state-mcp`` (``game_mcp.bridge.mcp_server``) is a small read-only MCP server
  that serves that dumped state to an AI advisor. No screenshot, no input.

Advisor mode is separate from the desktop-input server (``game_mcp.server``), which
remains the v1 path and is untouched.
"""
