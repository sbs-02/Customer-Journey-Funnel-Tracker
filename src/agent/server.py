"""
FastAPI backend for the funnel chat agent.

Flow of one question:

  React  --POST /chat-->  FastAPI
                            |
                            | 1. ask Groq, advertising the MCP tool list
                            | 2. Groq replies with tool_calls
                            | 3. execute each call THROUGH THE MCP CLIENT
                            | 4. hand the results (with provenance) back to Groq
                            | 5. Groq writes prose citing the snapshot
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

from openai import AsyncOpenAI
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.mcp_client import MCPClient
from agent.prompts import (TOOL_RESULT_REMINDER, build_suggested_prompts,
                           build_system_prompt)

load_dotenv()

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_LOG_DATEFMT = "%H:%M:%S"
_LOG_FILE = Path(__file__).resolve().parents[2] / "server.log"

logging.basicConfig(level=logging.INFO,
                    format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
_file_handler = logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
logging.getLogger().addHandler(_file_handler)
log = logging.getLogger("agent.server")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
CORS_ORIGINS = [o.strip() for o in
                os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
                if o.strip()]

# Available models for the model picker. Each entry is (id, display_name, notes).
# Only models that support tool calling on Groq are listed here.
MODELS = [
    {"id": "llama-3.1-8b-instant",    "name": "Llama 3.1 8B",    "note": "Fast, lightweight"},
    {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B",   "note": "Strong reasoning"},
    {"id": "llama-3.1-70b-versatile", "name": "Llama 3.1 70B",   "note": "Reliable, proven"},
    {"id": "meta-llama/llama-4-scout-17b-16e-instruct", "name": "Llama 4 Scout", "note": "Latest Llama 4"},
    {"id": "qwen/qwen3-32b",         "name": "Qwen 3 32B",      "note": "Balanced performance"},
    {"id": "qwen/qwen3.6-27b",       "name": "Qwen 3.6 27B",    "note": "Updated Qwen"},
    {"id": "openai/gpt-oss-120b",     "name": "GPT-OSS 120B",    "note": "Largest, most capable"},
    {"id": "openai/gpt-oss-20b",      "name": "GPT-OSS 20B",     "note": "Compact GPT"},
]
MODEL_IDS = {m["id"] for m in MODELS}

# Rounds in which the model is ALLOWED to call tools. Three, not two: a first
# call, one retry when that call comes back with an "invalid arguments" error,
# and a little headroom. An unbounded loop lets a confused model call tools
# forever on one request.
#
# Narration is no longer one of these rounds -- see the forced final turn in
# chat(). Previously, a model that used its last round on a tool call never got
# to describe the result, and the user saw the raw-JSON fallback instead.
MAX_TOOL_ROUNDS = 3

mcp_client = MCPClient()


def _assistant_message(message) -> dict:
    """Re-encode a completion as an assistant message Groq will accept back.

    THIS IS THE GROQ FIX. The obvious `message.model_dump()` round-trips every
    field the OpenAI SDK models -- including ones Groq's request validator does
    not accept. `annotations` is the one that bites: the SDK always emits the key
    (as null, or as an empty list), and Groq rejects the whole request with

        400 - 'messages.N' : for 'role:assistant' the following must be
              satisfied[('messages.N' : property 'annotations' is unsupported)]

    Note where that lands. The FIRST call has no assistant message in the history
    and succeeds; the failure only appears on the SECOND call, once a tool-calling
    turn has been appended -- so every tool-backed question died and every
    question the model could answer without data worked. That is exactly the
    reported symptom.

    Rather than blocklist `annotations` and wait for the next SDK field to break
    us, allowlist the three keys the protocol actually needs: role, content and
    tool_calls. Empty content is sent as "" because Groq requires the key to be
    present, and tool_calls are rebuilt as plain dicts so no SDK-only field can
    ride along inside them either.
    """
    payload: dict = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name,
                             "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ]
    return payload


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
    model: str | None = Field(default=None, max_length=100)


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
    log.info("health check requested")
    result = {
        "status": "ok",
        "model": GROQ_MODEL,
        "mcp_tools": mcp_client.tool_names,
    }
    log.info("health check: model=%s, tools=%s", GROQ_MODEL, ", ".join(mcp_client.tool_names))
    return result


@app.get("/models")
async def models() -> dict:
    log.info("models list requested")
    return {"models": MODELS, "default": GROQ_MODEL}


@app.get("/suggested-prompts")
async def suggested_prompts() -> dict:
    """Starters derived from the warehouse's real snapshot history.

    Read over MCP like everything else -- the backend does not import the tool
    handlers. If history is unavailable the fallback starters need no snapshot,
    so a cold warehouse costs us a nice-to-have rather than the whole page.
    """
    log.info("suggested_prompts: fetching snapshot history")
    result = await mcp_client.call("snapshot_history", {})
    if "error" in result:
        log.warning("snapshot_history unavailable, using fallback prompts: %s",
                    result["error"])
        return {"prompts": build_suggested_prompts([])}
    prompts = build_suggested_prompts(result.get("snapshots", []))
    log.info("suggested_prompts: generated %d prompts", len(prompts))
    return {"prompts": prompts}


def _describe_anomaly(a: dict) -> str:
    """One anomaly as a sentence fragment, using only figures the tool returned."""
    drop = abs(a["change_pct"])
    if a["kind"] == "conversion_rate":
        return (f"{a['label']} has declined by {drop:.0f}%, "
                f"from {a['previous']:.1f}% to {a['current']:.1f}%")
    return (f"{a['label']} has declined by {drop:.0f}%, "
            f"from {a['previous']:,} to {a['current']:,}")


async def _session_opener() -> tuple[str, list[ToolCall]]:
    """The proactive anomaly note shown before the first answer of a session.

    Composed in PYTHON, not by the model. The whole point of this feature is that
    it interrupts the user unprompted, so it has to be right: handing the figures
    to an 8B model to phrase would reintroduce exactly the fabrication risk the
    rest of the system exists to remove. Deterministic text cannot round 27% to
    "about a third".

    Returns ("", []) when there is nothing to say -- no anomalies, or the check
    could not run. A warehouse that cannot answer this is not a reason to fail the
    user's actual question, so every failure here is swallowed and logged.
    """
    result = await mcp_client.call("funnel_anomalies", {})

    if "error" in result:
        log.warning("session anomaly check unavailable: %s", result["error"])
        return "", []

    anomalies = result.get("anomalies") or []
    if not anomalies:
        log.info("session anomaly check: nothing above threshold")
        return "", []

    log.info("session anomaly check: %d flagged", len(anomalies))
    call = ToolCall(name="funnel_anomalies", arguments={}, result=result)
    window = f"{result['current_week']} vs {result['previous_week']}"

    if len(anomalies) == 1:
        text = (f"Before we begin, I noticed that {_describe_anomaly(anomalies[0])} "
                f"compared to the previous week ({window}). You may want to "
                f"investigate this.")
    else:
        bullets = "\n".join(f"- {_describe_anomaly(a).capitalize()}"
                            for a in anomalies)
        text = (f"Before we begin, a few things dropped sharply compared to the "
                f"previous week ({window}):\n\n{bullets}\n\nYou may want to "
                f"investigate.")
    return text, [call]


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    # Resolve the model: use the per-request override if valid, otherwise default.
    selected_model = GROQ_MODEL
    if body.model:
        if body.model in MODEL_IDS:
            selected_model = body.model
        else:
            log.warning("unknown model %r, falling back to %s", body.model, GROQ_MODEL)

    log.info("chat request: message=%r (len=%d), history=%d messages, model=%s",
             body.message[:100], len(body.message), len(body.history), selected_model)

    # Built per request, not per process: a long-running server would otherwise
    # still think it is the day it booted.
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages += [m.model_dump() for m in body.history]
    messages.append({"role": "user", "content": body.message})

    executed: list[ToolCall] = []
    client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)

    # An empty history IS a new session -- the frontend sends every prior turn, so
    # there is no session id to track and no server-side state to keep.
    opener, opener_calls = ("", [])
    if not body.history:
        log.info("new session: running anomaly check")
        opener, opener_calls = await _session_opener()
        if opener:
            log.info("anomaly opener: %s", opener[:100])
        else:
            log.info("anomaly check: no anomalies to report")

    async def ask(with_tools: bool):
        """One turn against Groq. Returns the completion, or raises if unreachable."""
        kwargs = {"model": selected_model, "messages": messages}
        if with_tools:
            kwargs["tools"] = mcp_client.openai_tool_specs()
        log.info("asking Groq: model=%s, with_tools=%s, message_count=%d",
                 selected_model, with_tools, len(messages))
        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        log.info("Groq replied: content_len=%d, tool_calls=%d",
                 len(msg.content or ""), len(msg.tool_calls or []))
        return msg

    try:
        used_all_rounds = True
        for round_num in range(1, MAX_TOOL_ROUNDS + 1):
            log.info("tool loop round %d/%d", round_num, MAX_TOOL_ROUNDS)
            message = await ask(with_tools=True)
            messages.append(_assistant_message(message))

            if not message.tool_calls:
                log.info("no tool calls in round %d, breaking", round_num)
                used_all_rounds = False
                break

            for call in message.tool_calls:
                name = call.function.name
                # Groq/OpenAI send arguments as a JSON STRING, not a dict --
                # parse them ourselves.
                args = json.loads(call.function.arguments or "{}")
                log.info("executing tool call: %s(%s)", name, args)
                result = await mcp_client.call(name, args)
                executed.append(ToolCall(name=name, arguments=args, result=result))
                log.info("tool %s result: %s", name,
                         "error" if "error" in result else "success")

                # tool_call_id (not tool_name) is how OpenAI-compatible APIs
                # line a tool result up with the call that requested it.
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str) + TOOL_RESULT_REMINDER,
                })

        # The model spent its last allowed round calling tools, so it has never
        # seen those results. Give it one final turn WITHOUT tools: it cannot
        # call anything else, so it must write the answer.
        if used_all_rounds:
            log.info("tool rounds exhausted; forcing a narration turn")
            message = await ask(with_tools=False)
            messages.append(_assistant_message(message))

    except Exception as exc:
        log.exception("groq call failed")
        return ChatResponse(
            answer=_join(opener, _groq_error_message(exc, selected_model)),
            tool_calls=opener_calls,
            model=selected_model,
        )

    answer = (messages[-1].get("content") or "").strip()

    # The model called tools but then said nothing useful. Rather than show an
    # empty bubble, fall back to the grounded numbers themselves -- the data is
    # the point, the prose is decoration.
    if not answer and executed:
        log.info("model produced no prose after %d tool calls; using fallback", len(executed))
        answer = _fallback_summary(executed)

    log.info("chat response: answer_len=%d, tool_calls=%d",
             len(answer), len(opener_calls) + len(executed))
    return ChatResponse(answer=_join(opener, answer),
                        tool_calls=opener_calls + executed,
                        model=selected_model)


def _join(opener: str, answer: str) -> str:
    return f"{opener}\n\n{answer}".strip() if opener else answer


def _groq_error_message(exc: Exception, model: str | None = None) -> str:
    """Explain a failed Groq call in terms of what to actually check.

    The old text blamed connectivity and the API key for every failure, which is
    actively misleading when Groq answered and REJECTED us -- a 400 means the key
    is fine and the request was malformed. Reporting a bad request as "I could not
    reach Groq" is what made the annotations bug look like a config problem.
    """
    status = getattr(exc, "status_code", None)
    model = model or GROQ_MODEL

    if status == 401:
        detail = "GROQ_API_KEY is missing or not valid."
    elif status == 404:
        detail = f"Groq does not recognise the model `{model}`."
    elif status == 429:
        detail = "Groq is rate-limiting this key. Try again shortly."
    elif status == 400:
        detail = ("Groq rejected the request as malformed. This is a bug on our "
                  "side, not a problem with your question.")
    elif status is not None:
        detail = f"Groq returned HTTP {status}."
    else:
        detail = (f"I could not reach Groq at {GROQ_BASE_URL}. Check network "
                  f"access and that GROQ_API_KEY is set.")

    return f"{detail}\n\nDetail: {exc}"


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