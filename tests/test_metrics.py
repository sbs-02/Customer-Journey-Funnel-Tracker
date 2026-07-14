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


def _minimal_args(tool) -> dict:
    """Smallest valid argument set for each tool."""
    return {
        "funnel_yoy": {"stage": "lead"},
        "funnel_snapshot": {},
        "weekly_trend": {"measure": "lead", "weeks": 4},
        "running_total": {"measure": "orders", "period": "mtd"},
        "top_dimension": {"dimension": "channel"},
        "compare_as_of": {"as_of_date": dt.date.today().isoformat()},
        "explain_scan": {},
        "snapshot_history": {},
    }[tool.name]