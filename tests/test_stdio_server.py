"""End-to-end: launch the real stdio server as a subprocess and talk MCP to it."""

import asyncio
import sys

import pytest
from fastmcp import Client

EXPECTED_TOOLS = {
    "list_game_windows",
    "bind_game",
    "get_game_screen",
    "click",
    "move_mouse",
    "press_key",
    "stop_input",
    "resume_input",
    "get_status",
}


@pytest.mark.integration
def test_stdio_server_exposes_exactly_the_game_tools():
    config = {
        "mcpServers": {
            "sts": {
                "command": sys.executable,
                "args": ["-m", "game_mcp", "--log-level", "WARNING"],
                "env": {"GAME_MCP_CORNER_FAILSAFE": "0"},
            }
        }
    }

    async def run():
        async with Client(config) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert names == EXPECTED_TOOLS
            status = await client.call_tool("get_status")
            assert status.data["ok"] is True
            assert status.data["binding"] is None
            assert status.data["operator_stop"]["alive"] is True
            result = await client.call_tool("click", {"x": 1, "y": 1, "observation_id": "nope"})
            assert result.data["error"]["code"] == "NOT_BOUND"
            resumed = await client.call_tool("resume_input")
            assert resumed.data["ok"] is True and resumed.data["stop"]["stopped"] is False

    asyncio.run(run())
