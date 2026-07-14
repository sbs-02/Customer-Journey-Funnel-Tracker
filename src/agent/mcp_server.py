"""
MCP (Model Context Protocol) server exposing the lakehouse metric tools.

The brief requires MCP explicitly -- "Tool Protocol: MCP" in the baseline stack,
and "Build an MCP tool that computes YoY / WoW / running-total metrics directly
from the lakehouse" in Project 3's core requirements. Handing a plain Python
function to ollama.chat(tools=[...]) is native function-calling, NOT MCP, and
does not satisfy that requirement.

This server speaks MCP over stdio. It is read-only: it exposes no tool that can
write, and the underlying lakehouse handle is a StaticTable, which physically
cannot commit.

Run standalone:  uv run python src/agent/mcp_server.py
Inspect:         npx @modelcontextprotocol/inspector uv run python src/agent/mcp_server.py
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import mcp
import mcp.server
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools import TOOLS, dispatch

# MCP speaks JSON-RPC on stdout. Anything else printed there corrupts the
# protocol stream, so logs MUST go to stderr.`
logging.basicConfig(
    level=logging.INFO, stream=sys.stderr,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_server")

server = Server("funnel-lakehouse")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Advertise the metric tools. Built from the same registry the HTTP backend
    uses, so the two can never disagree about what exists."""
    return [
        types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
        for t in TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """Execute a tool and return its JSON result, provenance included.

    dispatch() runs synchronously against Arrow tables held in memory -- fast, but
    still CPU-bound, so it goes to a worker thread rather than blocking the event
    loop while DuckDB runs.
    """
    result = await asyncio.to_thread(dispatch, name, arguments or {})
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main() -> None:
    log.info("MCP server ready, %d tools: %s",
             len(TOOLS), ", ".join(t.name for t in TOOLS))
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="funnel-lakehouse",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())