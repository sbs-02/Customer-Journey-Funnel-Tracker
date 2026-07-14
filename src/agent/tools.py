"""
Tool registry. One definition of every tool, its JSON schema, and its handler.

Both the MCP server (mcp_server.py) and the FastAPI backend (server.py) build
their tool lists from TOOLS below, so the two surfaces cannot advertise different
tools or different schemas.

Every handler returns a dict containing a "provenance" key. dispatch() enforces
that -- a tool that forgets its receipts raises rather than returning a naked
number to the model.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable

from agent import metrics
from agent.metrics import MetricError

log = logging.getLogger("agent.tools")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict           # JSON Schema
    handler: Callable[..., dict]


# JSON-Schema fragments reused across tools.
_WEEK_ARGS = {
    "iso_year": {
        "type": "integer",
        "description": "ISO year, e.g. 2025. Omit for the latest complete week.",
    },
    "iso_week": {
        "type": "integer",
        "minimum": 1, "maximum": 53,
        "description": "ISO week number 1-53. Omit for the latest complete week.",
    },
}

TOOLS: list[Tool] = [
    Tool(
        name="funnel_yoy",
        description=(
            "Compare one funnel stage for a week against the SAME ISO week one "
            "year earlier. Use this for any 'vs last year' or 'year over year' "
            "question. Returns the current count, the prior-year count, the "
            "absolute delta and the percentage change."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["visit", "lead", "opportunity"],
                    "description": "Funnel stage to compare.",
                },
                **_WEEK_ARGS,
            },
            "required": ["stage"],
        },
        handler=metrics.funnel_yoy,
    ),
    Tool(
        name="funnel_snapshot",
        description=(
            "The whole funnel for one week: visits, leads, opportunities and "
            "orders, plus stage-to-stage conversion and drop-off rates. Use this "
            "for 'how is the funnel doing' or 'what is our conversion rate'."
        ),
        parameters={"type": "object", "properties": {**_WEEK_ARGS}},
        handler=metrics.funnel_snapshot,
    ),
    Tool(
        name="weekly_trend",
        description=(
            "Recent weeks of a measure, each with week-over-week and "
            "year-over-year percentage changes. Use for 'trend', 'over time', "
            "'last N weeks', or any week-over-week question."
        ),
        parameters={
            "type": "object",
            "properties": {
                "measure": {
                    "type": "string",
                    "enum": ["visit", "lead", "opportunity", "orders", "revenue"],
                    "description": "What to trend.",
                },
                "weeks": {
                    "type": "integer", "minimum": 1, "maximum": 104,
                    "description": "How many recent weeks to return. Default 12.",
                },
            },
            "required": ["measure"],
        },
        handler=metrics.weekly_trend,
    ),
    Tool(
        name="running_total",
        description=(
            "Running (cumulative) total of a measure within a period. The total "
            "resets at each period boundary. Use for 'running total', "
            "'cumulative', 'month to date', 'year to date'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "measure": {
                    "type": "string",
                    "enum": ["visit", "lead", "opportunity", "orders", "revenue"],
                },
                "period": {
                    "type": "string", "enum": ["wtd", "mtd", "ytd"],
                    "description": "Reset boundary: week, month or year to date.",
                },
            },
            "required": ["measure"],
        },
        handler=metrics.running_total,
    ),
    Tool(
        name="top_dimension",
        description=(
            "Rank channels or products by orders or revenue for a week. Use for "
            "'top channels', 'best products', 'which channel drove the most'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": ["channel", "product"]},
                "measure": {"type": "string", "enum": ["orders", "revenue"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                **_WEEK_ARGS,
            },
            "required": ["dimension"],
        },
        handler=metrics.top_dimension,
    ),
    Tool(
        name="compare_as_of",
        description=(
            "Iceberg TIME TRAVEL. Read the data as it existed on a past date, "
            "using the snapshot that was live then, and compare it with the data "
            "now. Use for 'what did X look like as of <date>', 'has the data "
            "changed since', 'what did we think last week'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "as_of_date": {
                    "type": "string",
                    "description": "Date to travel to, YYYY-MM-DD.",
                },
                "stage": {"type": "string", "enum": ["visit", "lead", "opportunity"]},
            },
            "required": ["as_of_date"],
        },
        handler=metrics.compare_as_of,
    ),
    Tool(
        name="explain_scan",
        description=(
            "Explain the query plan: how many Iceberg data files a filtered read "
            "opens versus skips, and why. Use when asked how the query ran, how "
            "much data was read, or whether partitioning is helping."
        ),
        parameters={"type": "object", "properties": {**_WEEK_ARGS}},
        handler=metrics.explain_scan,
    ),
    Tool(
        name="snapshot_history",
        description=(
            "List every Iceberg snapshot of the funnel table with commit times "
            "and row counts. Use for 'what snapshots exist', 'when was the data "
            "last updated', 'can I trust this number'."
        ),
        parameters={"type": "object", "properties": {}},
        handler=metrics.snapshot_history,
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def dispatch(name: str, arguments: dict[str, Any]) -> dict:
    """Run a tool by name. This is the ONLY path from the model to the data.

    Returns a dict that always carries provenance. On a bad question it returns a
    structured error rather than raising, so the model can apologise usefully
    instead of the request 500-ing.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name!r}. "
                         f"Available: {', '.join(TOOLS_BY_NAME)}"}

    # Drop nulls: Ollama frequently sends {"iso_year": null} for omitted optional
    # args, which would override the "latest complete week" default with None and
    # look like an explicit request for week None.
    cleaned = {k: v for k, v in (arguments or {}).items() if v is not None}

    log.info("tool call: %s(%s)", name, cleaned)
    try:
        result = tool.handler(**cleaned)
    except MetricError as exc:
        # An understood question we cannot answer. Not a bug -- tell the truth.
        log.warning("tool %s refused: %s", name, exc)
        return {"error": str(exc), "tool": name}
    except TypeError as exc:
        log.warning("tool %s bad arguments: %s", name, exc)
        return {"error": f"Invalid arguments for {name}: {exc}", "tool": name}
    except Exception as exc:
        log.exception("tool %s failed", name)
        return {"error": f"{name} failed: {exc}", "tool": name}

    # Guardrail: a numeric answer without receipts is exactly what the brief
    # forbids. Fail loudly rather than let an unsourced number reach the model.
    if "provenance" not in result:
        raise RuntimeError(
            f"Tool {name} returned no provenance. Every numeric result must carry "
            "snapshot id, as-of date, date range and source tables.")
    return result