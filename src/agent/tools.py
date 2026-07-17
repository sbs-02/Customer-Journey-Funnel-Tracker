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

import jsonschema

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
_MEASURES = ["visit", "lead", "opportunity", "orders", "revenue"]

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
            "Running (cumulative) total of one or more measures within a period. "
            "The total resets at each period boundary. Use for 'running total', "
            "'cumulative', 'month to date', 'year to date'. To cover several "
            "measures at once, pass a list to 'measure' -- one call is enough."
        ),
        parameters={
            "type": "object",
            "properties": {
                "measure": {
                    "description": ("A single measure, or a list of measures to "
                                    "report side by side."),
                    "anyOf": [
                        {"type": "string", "enum": _MEASURES},
                        {"type": "array",
                         "items": {"type": "string", "enum": _MEASURES},
                         "minItems": 1, "maxItems": 5},
                    ],
                },
                "period": {
                    "type": "string", "enum": ["wtd", "mtd", "ytd"],
                    "description": "Reset boundary: week, month or year to date.",
                },
                "year": {
                    "type": "integer",
                    "description": ("Restrict to a single calendar year, e.g. 2025. "
                                    "Omit for the most recent data."),
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
                    "description": (
                        "Moment to travel to: YYYY-MM-DD, or a full ISO "
                        "timestamp (2026-07-16T15:23:00) when several snapshots "
                        "share a day and you need to name one exactly."
                    ),
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


# Names the model reaches for that mean a parameter we really have under another
# name. Every other tool keys weeks on the ISO calendar, so the model says
# "iso_year" out of habit; running_total's year filter is the civil calendar.
# Renaming here beats carrying two near-identical parameters in the schema.
ARG_ALIASES: dict[str, dict[str, str]] = {
    "running_total": {"iso_year": "year"},
}


def coerce_arguments(tool: Tool, arguments: dict[str, Any] | None) -> dict:
    """Normalise what the model actually sends into what the schema demands.

    Small models do not emit clean JSON-Schema-conformant arguments. Three habits
    break an unforgiving validator, and all three are the model being reasonable
    rather than broken, so we absorb them here instead of failing the call:

      - null for an omitted optional: {"iso_year": null} means "no preference",
        NOT "the year None". Dropping the key restores the handler default.
      - plural enums: "visits" for the "visit" stage. The word is right, the
        grammar is ours.
      - stringified integers: "2025" for iso_year, because JSON typing is a
        detail the model is not thinking about.

    This MUST run before schema validation, not after. That ordering is the whole
    bug this function exists to fix.
    """
    props: dict = (tool.parameters or {}).get("properties", {})
    aliases = ARG_ALIASES.get(tool.name, {})
    cleaned: dict = {}

    for key, value in (arguments or {}).items():
        if value is None:                       # omitted optional -- use the default
            continue

        key = aliases.get(key, key)

        # An argument this tool does not have. Models hand back fields they saw in
        # an earlier tool RESULT -- snapshot_id, snapshot_committed_at -- as though
        # provenance were an input. Passing it through would raise TypeError in the
        # handler and turn a nearly-correct call into a hard failure, so drop it
        # and let the rest of the call stand or fall on its own merits.
        if key not in props:
            log.warning("tool %s: ignoring unknown argument %r", tool.name, key)
            continue

        spec = props[key]
        expected = spec.get("type")
        enum = _enum_of(spec)
        allows_list = _allows_list(spec)

        # A one-element list where only a scalar is allowed: the model was
        # thinking "a list of measures" and found one. Unwrap rather than reject.
        if not allows_list and isinstance(value, list) and len(value) == 1:
            value = value[0]

        if isinstance(value, list):
            value = [_coerce_scalar(key, v, expected, enum) for v in value]
        else:
            value = _coerce_scalar(key, value, expected, enum)

        cleaned[key] = value

    return cleaned


def _coerce_scalar(key: str, value: Any, expected: Any, enum: list | None) -> Any:
    """Nudge one value towards what the schema wants. Never forces it."""
    if enum and isinstance(value, str) and value not in enum:
        match = _match_enum(value, enum)
        if match is not None:
            log.info("coerced %s=%r -> %r", key, value, match)
            return match
    elif expected == "integer" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass                                # let the validator report it properly
    return value


def _enum_of(spec: dict) -> list | None:
    """The allowed values for a parameter, looking inside anyOf/items.

    running_total's `measure` is an anyOf(string, array-of-string), so the enum
    is not at the top level of the spec. Without this, "visits" would stop being
    corrected to "visit" the moment a parameter learned to accept a list.
    """
    if "enum" in spec:
        return spec["enum"]
    for branch in spec.get("anyOf", []):
        if "enum" in branch:
            return branch["enum"]
        if "enum" in branch.get("items", {}):
            return branch["items"]["enum"]
    if "enum" in spec.get("items", {}):
        return spec["items"]["enum"]
    return None


def _allows_list(spec: dict) -> bool:
    if spec.get("type") == "array":
        return True
    return any(b.get("type") == "array" for b in spec.get("anyOf", []))


def _match_enum(value: str, enum: list) -> Any | None:
    """Best-effort map of a near-miss onto an allowed enum value.

    Case-insensitive first, then a naive de-pluralisation. Deliberately
    conservative: it only ever returns a value the schema already allows, so the
    worst case is that we fail validation exactly as we would have anyway.
    """
    folded = value.strip().lower()
    options = {str(o).lower(): o for o in enum}

    if folded in options:
        return options[folded]
    if folded.endswith("s") and folded[:-1] in options:     # "visits" -> "visit"
        return options[folded[:-1]]
    if f"{folded}s" in options:                             # "order"  -> "orders"
        return options[f"{folded}s"]
    return None


def validate_arguments(tool: Tool, arguments: dict) -> str | None:
    """Check arguments against the tool's JSON Schema.

    Returns a human-readable reason on failure, or None when valid. We validate
    here -- after coercion -- rather than letting the MCP SDK do it at the
    protocol edge, because the SDK validates the RAW arguments and reports the
    failure as an opaque plain-text string the client cannot parse.
    """
    try:
        jsonschema.validate(instance=arguments, schema=tool.parameters)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(p) for p in exc.path) or "arguments"
        return f"{where}: {exc.message}"
    return None


def _fix_hint(tool: Tool, cleaned: dict) -> str:
    """Turn a validation failure into the call the model should make instead.

    The common failure by far is asking for several measures at once, because
    that is how the USER asked the question. Spelling out the fan-out is what
    actually gets the retry right.
    """
    props = tool.parameters.get("properties", {})

    for key, value in cleaned.items():
        if isinstance(value, list) and key in props and "enum" in props[key]:
            others = {k: v for k, v in cleaned.items() if k != key}
            calls = ", ".join(
                f"{tool.name}({key}={v!r}"
                + (", " + ", ".join(f"{k}={o!r}" for k, o in others.items()) if others else "")
                + ")"
                for v in value)
            return (
                f"{tool.name} takes ONE {key} per call and you passed {len(value)}. "
                f"Do not retry with a list. Instead make {len(value)} separate "
                f"calls now, in one go: {calls}. Then combine the results into a "
                f"single table for the user.")

    missing = [k for k in tool.parameters.get("required", []) if k not in cleaned]
    if missing:
        return (f"Required argument(s) {', '.join(missing)} were missing. Retry "
                f"{tool.name} with them set.")

    return f"Check the argument types and allowed values for {tool.name} and retry once."


def dispatch(name: str, arguments: dict[str, Any]) -> dict:
    """Run a tool by name. This is the ONLY path from the model to the data.

    Returns a dict that always carries provenance. On a bad question it returns a
    structured error rather than raising, so the model can apologise usefully
    instead of the request 500-ing.
    """
    log.info("dispatch: tool=%s, args=%s", name, arguments)
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        log.warning("dispatch: unknown tool %r", name)
        return {"error": f"Unknown tool {name!r}. "
                         f"Available: {', '.join(TOOLS_BY_NAME)}"}

    cleaned = coerce_arguments(tool, arguments)
    log.debug("dispatch: %s cleaned args=%s", name, cleaned)

    invalid = validate_arguments(tool, cleaned)
    if invalid is not None:
        log.warning("tool %s rejected arguments %s: %s", name, cleaned, invalid)
        return {"error": f"Invalid arguments for {name} -- {invalid}",
                "tool": name,
                "arguments_received": arguments,
                # The model reads the tool result, not the system prompt, when it
                # decides what to do next. An error that says only "invalid" gets
                # narrated as a failure; an error that says exactly which call to
                # make next gets retried correctly.
                "how_to_fix": _fix_hint(tool, cleaned),
                "valid_arguments": tool.parameters.get("properties", {})}

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
        log.error("tool %s returned no provenance!", name)
        raise RuntimeError(
            f"Tool {name} returned no provenance. Every numeric result must carry "
            "snapshot id, as-of date, date range and source tables.")
    log.info("dispatch: %s completed successfully (snapshot=%s)",
             name, result["provenance"].get("snapshot_id", "unknown"))
    return result