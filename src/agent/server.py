"""
FastAPI backend for the funnel chat agent.

Flow of one question:

  React  --POST /chat-->  FastAPI
                            |
                            | 1. ask Ollama, advertising the MCP tool list
                            | 2. Ollama replies with tool_calls
                            | 3. execute each call THROUGH THE MCP CLIENT
                            | 4. hand the results (with provenance) back to Ollama
                            | 5. Ollama writes prose citing the snapshot
                            v
                       {"answer": ..., "tool_calls": [...]}

The tool_calls are returned to the UI as well as the prose, so a user can see the
raw grounded numbers next to the model's summary. That is the difference between
trusting the model and being able to check it.

Run:  uv run uvicorn src.agent.server:app --reload --port 8000
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import ollama
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.mcp_client import MCPClient
from agent.prompts import SUGGESTED_PROMPTS, SYSTEM_PROMPT, TOOL_RESULT_REMINDER

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("agent.server")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
CORS_ORIGINS = [o.strip() for o in
                os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
                if o.strip()]

# Two rounds is enough: one to gather tool results, one to narrate them. An
# unbounded loop lets a confused model call tools forever on one request.
MAX_TOOL_ROUNDS = 2

mcp_client = MCPClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the MCP server once at boot, not per request -- spawning a
    subprocess and re-reading Iceberg metadata on every chat turn would add
    seconds of latency to each message."""
    await mcp_client.start()
    log.info("MCP tools available: %s", ", ".join(mcp_client.tool_names))
    yield
    await mcp_client.stop()


app = FastAPI(title="Funnel Analyst", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,      # not "*" -- an explicit allowlist
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list, max_length=20)


class ToolCall(BaseModel):
    name: str
    arguments: dict
    result: dict


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    model: str


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": OLLAMA_MODEL,
        "mcp_tools": mcp_client.tool_names,
    }


@app.get("/suggested-prompts")
async def suggested_prompts() -> dict:
    return {"prompts": SUGGESTED_PROMPTS}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [m.model_dump() for m in body.history]
    messages.append({"role": "user", "content": body.message})

    executed: list[ToolCall] = []
    client = ollama.AsyncClient(host=OLLAMA_HOST)

    for round_no in range(MAX_TOOL_ROUNDS):
        try:
            response = await client.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=mcp_client.ollama_tool_specs(),
            )
        except Exception as exc:
            log.exception("ollama call failed")
            return ChatResponse(
                answer=(
                    f"I could not reach the language model at {OLLAMA_HOST}. "
                    f"Check that Ollama is running (`ollama serve`) and that "
                    f"`{OLLAMA_MODEL}` is pulled.\n\nDetail: {exc}"
                ),
                model=OLLAMA_MODEL,
            )

        message = response.message
        messages.append(message.model_dump())

        if not message.tool_calls:
            break

        for call in message.tool_calls:
            name = call.function.name
            args = call.function.arguments or {}
            result = await mcp_client.call(name, args)
            executed.append(ToolCall(name=name, arguments=args, result=result))

            messages.append({
                "role": "tool",
                "content": json.dumps(result, default=str) + TOOL_RESULT_REMINDER,
            })
    else:
        log.warning("hit MAX_TOOL_ROUNDS without a final answer")

    answer = (messages[-1].get("content") or "").strip()

    # The model called tools but then said nothing useful. Rather than show an
    # empty bubble, fall back to the grounded numbers themselves -- the data is
    # the point, the prose is decoration.
    if not answer and executed:
        answer = _fallback_summary(executed)

    return ChatResponse(answer=answer, tool_calls=executed, model=OLLAMA_MODEL)


def _fallback_summary(executed: list[ToolCall]) -> str:
    """Render tool results directly when the model produces no prose."""
    lines = ["I retrieved the data but could not summarise it. Here are the raw "
             "figures, with their sources:\n"]
    for call in executed:
        result = call.result
        if "error" in result:
            lines.append(f"- **{call.name}**: {result['error']}")
            continue
        prov = result.get("provenance", {})
        lines.append(f"- **{call.name}**: `{json.dumps({k: v for k, v in result.items() if k != 'provenance'}, default=str)}`")
        if prov:
            lines.append(f"  - snapshot `{prov.get('snapshot_id')}`, "
                         f"committed {prov.get('snapshot_committed_at')}, "
                         f"range {prov.get('date_range')}")
    return "\n".join(lines)