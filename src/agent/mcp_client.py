"""
MCP client. Speaks the protocol to mcp_server.py over stdio and adapts the tool
schemas into the shape the LLM's function-calling API expects.

This is the piece that makes the system genuinely MCP rather than "a Python
function passed directly to the LLM". The FastAPI backend never imports the tool
handlers directly -- every call goes over the protocol.
"""

import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("agent.mcp_client")

SERVER_SCRIPT = Path(__file__).resolve().parent / "mcp_server.py"
ROOT = Path(__file__).resolve().parents[2]


class MCPClient:
    """Owns the MCP server subprocess and one session against it."""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list = []

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        # env/cwd are both explicit on purpose.
        #
        # With env=None the MCP SDK does NOT inherit our environment -- it passes
        # a sanitised whitelist (HOME, PATH, SHELL, TERM, USER, LOGNAME) and drops
        # everything else. ICEBERG_WAREHOUSE, GROQ_* and MAX_TOOL_ROWS would
        # never reach the tool subprocess, so the server would quietly fall back
        # to defaults and read the wrong warehouse. That is invisible wherever
        # config comes from real env vars rather than a .env file (Docker,
        # systemd, CI).
        #
        # cwd is pinned to the repo root because the server's load_dotenv() and
        # the default warehouse path are both resolved relative to it.
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_SCRIPT)],
            env=os.environ.copy(),
            cwd=str(ROOT),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._tools = (await self._session.list_tools()).tools
        log.info("connected to MCP server, %d tools", len(self._tools))

    async def stop(self) -> None:
        if self._stack:
            await self._stack.aclose()

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self._tools]

    def openai_tool_specs(self) -> list[dict]:
        """Translate MCP tool definitions into the OpenAI-compatible function-
        calling schema that Groq expects."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema,
                },
            }
            for t in self._tools
        ]

    async def call(self, name: str, arguments: dict) -> dict:
        """Invoke a tool over MCP and unwrap its JSON payload."""
        if self._session is None:
            return {"error": "MCP session is not started."}
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:
            log.exception("MCP call failed: %s", name)
            return {"error": f"MCP call to {name} failed: {exc}"}

        for block in result.content:
            if block.type == "text":
                try:
                    return json.loads(block.text)
                except json.JSONDecodeError:
                    # The server should always send JSON, but the MCP SDK itself
                    # emits bare text for protocol-level failures (input
                    # validation, an uncaught handler error). Surfacing that text
                    # as the error message beats the old "MCP returned non-JSON",
                    # which told the model -- and the user -- nothing actionable.
                    log.error("non-JSON from tool %s: %s", name, block.text)
                    return {"error": block.text.strip() or "MCP returned non-JSON",
                            "tool": name}
        return {"error": "MCP returned no content", "tool": name}