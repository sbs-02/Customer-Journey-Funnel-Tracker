"""
MCP client. Speaks the protocol to mcp_server.py over stdio and adapts the tool
schemas into the shape Ollama's function-calling API expects.

This is the piece that makes the system genuinely MCP rather than "a Python
function passed to ollama.chat". The FastAPI backend never imports the tool
handlers directly -- every call goes over the protocol.
"""

import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("agent.mcp_client")

SERVER_SCRIPT = Path(__file__).resolve().parent / "mcp_server.py"


class MCPClient:
    """Owns the MCP server subprocess and one session against it."""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list = []

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_SCRIPT)],
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

    def ollama_tool_specs(self) -> list[dict]:
        """Translate MCP tool definitions into Ollama's function-calling schema."""
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
        import json

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
                    return {"error": "MCP returned non-JSON", "raw": block.text}
        return {"error": "MCP returned no content"}