"""
MCP (Model Context Protocol) server exposing the lakehouse metric tools.

The brief requires MCP explicitly -- "Tool Protocol: MCP" in the baseline stack,
and "Build an MCP tool that computes YoY / WoW / running-total metrics directly
from the lakehouse" in Project 3's core requirements. Handing a plain Python
function to the LLM's native function-calling API is NOT MCP, and
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
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_LOG_DATEFMT = "%H:%M:%S"
_LOG_FILE = Path(__file__).resolve().parents[2] / "server.log"

logging.basicConfig(
    level=logging.INFO, stream=sys.stderr,
    format=_LOG_FORMAT, datefmt=_LOG_DATEFMT,
)
_file_handler = logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
logging.getLogger().addHandler(_file_handler)
log = logging.getLogger("mcp_server")

server = Server("funnel-lakehouse")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Advertise the metric tools. Built from the same registry the HTTP backend
    uses, so the two can never disagree about what exists."""
    log.info("list_tools: advertising %d tools: %s",
             len(TOOLS), ", ".join(t.name for t in TOOLS))
    return [
        types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
        for t in TOOLS
    ]


# validate_input=False is deliberate and load-bearing.
#
# The SDK's built-in validator runs against the arguments EXACTLY as the model
# sent them, before we get a chance to normalise. Small models routinely send
# {"iso_year": null} for an omitted optional, which fails `type: integer`, and the
# SDK reports that failure as the bare string "Input validation error: None is
# not of type 'integer'". That string is not JSON, so the client cannot parse it,
# the model is handed an unreadable tool result, and it invents nulls to fill the
# gap -- the exact bug this project was returning to users.
#
# We still validate every call: dispatch() coerces first, then applies the very
# same JSON Schema, and reports violations as structured JSON.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """Execute a tool and return its JSON result, provenance included.

    dispatch() runs synchronously against Arrow tables held in memory -- fast, but
    still CPU-bound, so it goes to a worker thread rather than blocking the event
    loop while DuckDB runs.

    NOTHING may escape this function as an exception. An uncaught error is turned
    by the SDK into a plain-text error block, which breaks the JSON contract the
    client depends on. A failure is still a JSON object here.
    """
    log.info("call_tool: %s(%s)", name, arguments or {})
    try:
        result = await asyncio.to_thread(dispatch, name, arguments or {})
    except Exception as exc:
        log.exception("dispatch raised for %s", name)
        result = {"error": f"{name} failed: {exc}", "tool": name}

    has_error = "error" in result
    log.info("call_tool: %s completed%s", name, f" (ERROR: {result.get('error', '')})" if has_error else "")
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