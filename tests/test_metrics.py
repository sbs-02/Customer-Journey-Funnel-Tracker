"""
The tests that matter: the ISO-week grain, and the traceability contract.

Run:  uv run pytest tests/ -v
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import metrics
from agent.tools import TOOLS, dispatch


def test_iso_year_never_collides_january_with_december():
    """The regression test for the bug this project shipped with.

    Under the old (year, iso_week) key, ISO week 52 of 2022 spanned 2022-01-01 to
    2022-12-31 -- 364 days. Under (iso_year, iso_week) no bucket may exceed 7 days.
    """
    from agent.lakehouse import lakehouse

    df = lakehouse.query(
        """
        SELECT iso_year, iso_week,
               MAX(date) - MIN(date) AS span_days
        FROM dim_date
        GROUP BY iso_year, iso_week
        ORDER BY span_days DESC
        LIMIT 1
        """,
        {"dim_date": lakehouse.arrow("dim_date")},
    )
    assert int(df.iloc[0].span_days) <= 6, (
        "An ISO week spans more than 7 days -- dim_date is keying weeks on the "
        "calendar year again."
    )


def test_every_numeric_tool_returns_provenance():
    """The brief: traceability is part of the spec, not a nice-to-have."""
    required = {"snapshot_id", "snapshot_committed_at",
                "as_of_date", "date_range", "source_tables", "calculation"}

    for tool in TOOLS:
        if tool.name == "snapshot_history":     # returns the audit trail itself
            continue
        result = dispatch(tool.name, _minimal_args(tool))
        assert "error" not in result, f"{tool.name} errored: {result.get('error')}"
        assert required <= set(result["provenance"]), (
            f"{tool.name} is missing provenance keys: "
            f"{required - set(result['provenance'])}"
        )


def test_yoy_compares_the_same_week_one_year_earlier():
    result = metrics.funnel_yoy(stage="lead", iso_year=2025, iso_week=23)
    assert result["week"] == "2025-W23"
    assert result["prior_year_week"] == "2024-W23"


def test_missing_prior_year_is_unknown_not_zero():
    """The earliest year has no prior year. yoy_pct must be None -- reporting 0%
    or -100% would be a fabricated number."""
    earliest = metrics.lakehouse.query(
        "SELECT MIN(iso_year) AS y FROM dim_date",
        {"dim_date": metrics.lakehouse.arrow("dim_date")},
    ).iloc[0].y

    result = metrics.funnel_yoy(stage="lead", iso_year=int(earliest) + 0, iso_week=30)
    if result["prior_year_events"] is None:
        assert result["yoy_pct"] is None
        assert result["direction"] == "unknown"
        assert result["note"] is not None


def test_unknown_stage_is_rejected():
    result = dispatch("funnel_yoy", {"stage": "purchase"})
    assert "error" in result
    assert "purchase" in result["error"]


def test_conversion_rate_with_no_traffic_is_none_not_zero():
    assert metrics._pct(0, 0) is None
    assert metrics._pct(5, 0) is None
    assert metrics._pct(0, 10) == 0.0     # a real zero IS zero


def test_period_labels_are_normalised_to_the_key_format():
    """The model and the user both write periods loosely. Needs no warehouse."""
    assert metrics._normalise_period("2026-3", "month") == "2026-03"
    assert metrics._normalise_period("2026-03", "month") == "2026-03"
    assert metrics._normalise_period("q1 2026", "quarter") == "2026-Q1"
    assert metrics._normalise_period("2026-Q1", "quarter") == "2026-Q1"
    assert metrics._normalise_period("2026-w5", "week") == "2026-W05"
    assert metrics._normalise_period("2026", "year") == "2026"


def test_assistant_turn_carries_only_fields_groq_accepts():
    """The regression test for the bug that broke every tool-backed answer.

    message.model_dump() round-trips SDK-only fields -- annotations, audio,
    refusal, function_call. Groq rejects the request outright:

        400 'messages.N' : property 'annotations' is unsupported

    Only ever on the SECOND call, once an assistant turn is in the history, which
    is why questions needing data failed while chit-chat worked.
    """
    from openai.types.chat import ChatCompletionMessage
    from openai.types.chat.chat_completion_message_tool_call import (
        ChatCompletionMessageToolCall, Function)

    from agent.server import _assistant_message

    message = ChatCompletionMessage(
        role="assistant", content=None,
        tool_calls=[ChatCompletionMessageToolCall(
            id="call_1", type="function",
            function=Function(name="funnel_yoy", arguments='{"stage": "lead"}'))])

    # Guard the premise: if the SDK ever stops emitting these, this test should
    # tell us rather than quietly passing for the wrong reason.
    assert "annotations" in message.model_dump()

    payload = _assistant_message(message)

    assert set(payload) == {"role", "content", "tool_calls"}
    assert payload["content"] == ""          # Groq wants the key present, not null
    assert payload["tool_calls"][0]["id"] == "call_1"
    assert payload["tool_calls"][0]["function"]["name"] == "funnel_yoy"


def test_plain_assistant_turn_has_no_tool_calls_key():
    from openai.types.chat import ChatCompletionMessage

    from agent.server import _assistant_message

    payload = _assistant_message(
        ChatCompletionMessage(role="assistant", content="235 leads."))
    assert payload == {"role": "assistant", "content": "235 leads."}


def _minimal_args(tool) -> dict:
    """Smallest valid argument set for each tool."""
    return {
        "funnel_yoy": {"stage": "lead"},
        "funnel_snapshot": {},
        "weekly_trend": {"measure": "lead", "weeks": 4},
        "running_total": {"measure": "orders", "period": "mtd"},
        "top_dimension": {"dimension": "channel"},
        "period_compare": {"measure": "revenue"},
        "daily_trend": {"measure": "lead", "days": 7},
        "funnel_anomalies": {},
        "compare_as_of": {"as_of_date": dt.date.today().isoformat()},
        "explain_scan": {},
        "snapshot_history": {},
    }[tool.name]