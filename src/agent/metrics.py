"""
The metric engine. Every number the agent can say comes from here.

TRACEABILITY IS THE CONTRACT. The brief: "Require the agent to state which
snapshot or as-of date it used for any number it reports -- traceability is part
of the spec, not a nice-to-have." So no function here returns a bare number.
Every result is wrapped in a provenance envelope carrying the snapshot id, when
that snapshot was committed, the date range covered, the source tables, and the
calculation in words. The LLM cannot report a figure without its receipts.

METRIC DEFINITIONS MIRROR sql/views.sql. The dashboard reads Postgres views; the
agent reads Iceberg directly (it must, to cite a snapshot). Two code paths, one
definition -- WEEKLY_FUNNEL_CTE below produces exactly what vw_weekly_funnel does.

THE WEEK KEY IS (iso_year, iso_week). Never (year, iso_week) -- see sql/views.sql.
"""

import datetime as dt
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.lakehouse import LakehouseError, ScanStats, Snapshot, lakehouse

log = logging.getLogger("agent.metrics")

FUNNEL_STAGES = ("visit", "lead", "opportunity")
FACT_FUNNEL, FACT_ORDERS, DIM_DATE = "fact_funnel_event", "fact_orders", "dim_date"
MAX_TOOL_ROWS = int(os.environ.get("MAX_TOOL_ROWS", "500"))

# A funnel stage must fall by more than this (percent, week over week) before the
# agent volunteers it at the start of a session. Configurable because the right
# number is a business judgement, not a technical one: too low and every session
# opens with noise nobody acts on, which trains people to ignore the real ones.
ANOMALY_THRESHOLD_PCT = float(os.environ.get("ANOMALY_THRESHOLD_PCT", "20"))

# How a period is labelled at each grain. These strings are the period NAMES the
# user sees and passes back ("2026-Q1"), so the SQL key and the label are the
# same expression -- there is no second mapping to drift out of step.
#
# week keys on the ISO calendar, month/quarter/year on the civil one. That split
# is deliberate and matches sql/views.sql: ISO weeks straddle year ends, so
# pairing d.year with d.iso_week silently mis-buckets late-December weeks.
_GRAINS = {
    "week":    "printf('%d-W%02d', d.iso_year, d.iso_week)",
    "month":   "printf('%d-%02d', d.year, d.month)",
    "quarter": "printf('%d-Q%d', d.year, d.quarter)",
    "year":    "printf('%d', d.year)",
}

# Mirrors vw_weekly_funnel in sql/views.sql.
WEEKLY_FUNNEL_CTE = """
    SELECT d.iso_year, d.iso_week, MIN(d.week_start_date) AS week_start_date,
           f.stage, COUNT(*) AS events
    FROM fact_funnel_event f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week, f.stage
"""

# Mirrors vw_weekly_orders in sql/views.sql.
WEEKLY_ORDERS_CTE = """
    SELECT d.iso_year, d.iso_week, MIN(d.week_start_date) AS week_start_date,
           COUNT(*) AS orders, SUM(o.revenue) AS revenue
    FROM fact_orders o
    JOIN dim_date d ON o.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week
"""


class MetricError(ValueError):
    """A question we understood but cannot answer from the data."""


@dataclass(frozen=True)
class Week:
    iso_year: int
    iso_week: int
    week_start_date: dt.date

    @property
    def label(self) -> str:
        return f"{self.iso_year}-W{self.iso_week:02d}"

    @property
    def date_range(self) -> dict:
        end = self.week_start_date + dt.timedelta(days=6)
        return {"start": self.week_start_date.isoformat(), "end": end.isoformat()}


def _provenance(snapshot: Snapshot, date_range: dict, calculation: str,
                source_tables: list[str], scan: ScanStats | None = None) -> dict:
    """The receipts attached to every number.

    as_of_date is when the answer was computed; snapshot_committed_at is when the
    data it used was written. Different facts -- stakeholders conflate them
    constantly -- so both are always present.
    """
    envelope = {
        **snapshot.as_dict(),
        "as_of_date": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date_range": date_range,
        "source_tables": source_tables,
        "calculation": calculation,
    }
    if scan is not None:
        envelope["scan"] = scan.as_dict()
    return envelope


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _parse_as_of(value: str) -> dt.date | dt.datetime:
    """Accept 'YYYY-MM-DD' or a full ISO timestamp for a time-travel target.

    A bare date keeps its original meaning (end of that day). A timestamp is
    needed whenever several snapshots share a day -- see snapshot_as_of.
    """
    text = str(value).strip()
    if text.endswith("Z"):                      # fromisoformat rejects Z before 3.11
        text = text[:-1] + "+00:00"
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise MetricError(
            f"as_of_date must be YYYY-MM-DD or an ISO timestamp such as "
            f"2026-07-16T15:23:00 -- got {value!r}.") from exc


def _validate_stage(stage: str) -> str:
    stage = (stage or "").strip().lower()
    if stage not in FUNNEL_STAGES:
        raise MetricError(
            f"Unknown funnel stage {stage!r}. Valid: {', '.join(FUNNEL_STAGES)}.")
    return stage


def latest_complete_week() -> Week:
    """The most recent ISO week with all 7 days present.

    "This week" cannot mean today's real-world week: the loaded data ends well
    before the wall clock, so that returns an empty result and the agent reports
    a truthful-looking zero. It also cannot mean the last week with ANY data --
    that is usually a partial week, and comparing a 2-day stub against a full week
    year-over-year produces a fake collapse.
    """
    log.info("resolving latest complete week from dim_date")
    df = lakehouse.query(
        """
        SELECT iso_year, iso_week, MIN(week_start_date) AS week_start_date
        FROM dim_date
        GROUP BY iso_year, iso_week
        HAVING COUNT(*) = 7
        ORDER BY iso_year DESC, iso_week DESC
        LIMIT 1
        """,
        {"dim_date": lakehouse.arrow(DIM_DATE)},
    )
    if df.empty:
        raise MetricError("dim_date contains no complete ISO week.")
    r = df.iloc[0]
    week = Week(int(r.iso_year), int(r.iso_week), _as_date(r.week_start_date))
    log.info("latest complete week: %s (start=%s)", week.label, week.week_start_date)
    return week


def resolve_week(iso_year: int | None = None, iso_week: int | None = None) -> Week:
    """Resolve an explicit week, or fall back to the latest complete one."""
    if iso_year is None or iso_week is None:
        log.info("resolve_week: no explicit week, falling back to latest complete")
        return latest_complete_week()
    log.info("resolve_week: looking up %d-W%02d", iso_year, iso_week)
    if not 1 <= int(iso_week) <= 53:
        raise MetricError(f"iso_week must be 1-53, got {iso_week}.")
    df = lakehouse.query(
        "SELECT MIN(week_start_date) AS ws FROM dim_date "
        "WHERE iso_year = ? AND iso_week = ?",
        {"dim_date": lakehouse.arrow(DIM_DATE)},
        [int(iso_year), int(iso_week)],
    )
    ws = df.iloc[0].ws
    if ws is None or str(ws) == "NaT":
        raise MetricError(
            f"ISO week {iso_year}-W{int(iso_week):02d} is not in the loaded date range.")
    week = Week(int(iso_year), int(iso_week), _as_date(ws))
    log.info("resolve_week: resolved to %s (start=%s)", week.label, week.week_start_date)
    return week


# --- Tools ------------------------------------------------------------------

def funnel_yoy(stage: str, iso_year: int | None = None,
               iso_week: int | None = None) -> dict:
    """One funnel stage for a week, against the same ISO week last year.

    This is the assignment's headline question:
    "How does this week's lead funnel compare to the same week last year?"

    The prior year is found by an EXPLICIT lookup of (iso_year - 1, iso_week) --
    not LAG() over the year ordering, which returns the previous year *present*
    and will happily compare 2026 against 2024 when a week is missing.
    """
    log.info("funnel_yoy: stage=%s, iso_year=%s, iso_week=%s", stage, iso_year, iso_week)
    stage = _validate_stage(stage)
    week = resolve_week(iso_year, iso_week)
    snapshot = lakehouse.snapshot(FACT_FUNNEL)
    log.info("funnel_yoy: querying %s for %s vs %s", stage, week.label, f"{week.iso_year - 1}-W{week.iso_week:02d}")

    df = lakehouse.query(
        f"""
        WITH weekly AS ({WEEKLY_FUNNEL_CTE})
        SELECT
            (SELECT events FROM weekly
              WHERE stage = ? AND iso_year = ? AND iso_week = ?) AS current_events,
            (SELECT events FROM weekly
              WHERE stage = ? AND iso_year = ? AND iso_week = ?) AS prior_year_events
        """,
        {"fact_funnel_event": lakehouse.arrow(FACT_FUNNEL),
         "dim_date": lakehouse.arrow(DIM_DATE)},
        [stage, week.iso_year, week.iso_week,
         stage, week.iso_year - 1, week.iso_week],
    )
    row = df.iloc[0]
    current = None if pd.isna(row.current_events) else int(row.current_events)
    prior = None if pd.isna(row.prior_year_events) else int(row.prior_year_events)

    if current is None:
        raise MetricError(
            f"No {stage} events recorded for {week.label}. "
            "The week may be outside the loaded date range.")

    # A missing prior year is not a 0% change and not a 100% rise. It is unknown,
    # and saying so is the entire point of this system.
    yoy_pct = None if not prior else round(100.0 * (current - prior) / prior, 1)
    log.info("funnel_yoy: %s %s current=%d, prior=%d, yoy_pct=%s",
             stage, week.label, current, prior, yoy_pct)

    return {
        "metric": "funnel_yoy",
        "stage": stage,
        "week": week.label,
        "week_start_date": week.week_start_date.isoformat(),
        "current_events": current,
        "prior_year_week": f"{week.iso_year - 1}-W{week.iso_week:02d}",
        "prior_year_events": prior,
        "delta": None if prior is None else current - prior,
        "yoy_pct": yoy_pct,
        "direction": _direction(yoy_pct),
        "note": None if prior is not None else
                f"ISO week {week.iso_week} of {week.iso_year - 1} has no data, so "
                f"year-over-year is undefined rather than zero.",
        "provenance": _provenance(
            snapshot,
            {"current_week": week.date_range,
             "prior_year_week": Week(week.iso_year - 1, week.iso_week,
                                     week.week_start_date - dt.timedelta(weeks=52)).date_range},
            f"count(stage='{stage}') for ISO week ({week.iso_year}, {week.iso_week}) "
            f"vs ISO week ({week.iso_year - 1}, {week.iso_week}); "
            f"yoy_pct = 100 * (current - prior) / prior",
            [FACT_FUNNEL, DIM_DATE],
        ),
    }


def funnel_snapshot(iso_year: int | None = None, iso_week: int | None = None) -> dict:
    """The whole funnel for one week: visit -> lead -> opportunity -> order,
    with stage-to-stage conversion and drop-off."""
    log.info("funnel_snapshot: iso_year=%s, iso_week=%s", iso_year, iso_week)
    week = resolve_week(iso_year, iso_week)
    log.info("funnel_snapshot: querying %s", week.label)
    df = lakehouse.query(
        f"""
        WITH weekly AS ({WEEKLY_FUNNEL_CTE}), o AS ({WEEKLY_ORDERS_CTE})
        SELECT
            SUM(CASE WHEN w.stage='visit'       THEN w.events ELSE 0 END) AS visits,
            SUM(CASE WHEN w.stage='lead'        THEN w.events ELSE 0 END) AS leads,
            SUM(CASE WHEN w.stage='opportunity' THEN w.events ELSE 0 END) AS opportunities,
            COALESCE(MAX(o.orders), 0)  AS orders,
            COALESCE(MAX(o.revenue), 0) AS revenue
        FROM weekly w
        LEFT JOIN o ON o.iso_year = w.iso_year AND o.iso_week = w.iso_week
        WHERE w.iso_year = ? AND w.iso_week = ?
        """,
        {"fact_funnel_event": lakehouse.arrow(FACT_FUNNEL),
         "fact_orders": lakehouse.arrow(FACT_ORDERS),
         "dim_date": lakehouse.arrow(DIM_DATE)},
        [week.iso_year, week.iso_week],
    )
    r = df.iloc[0]
    visits, leads = int(r.visits or 0), int(r.leads or 0)
    opps, orders = int(r.opportunities or 0), int(r.orders or 0)
    log.info("funnel_snapshot: %s visits=%d, leads=%d, opps=%d, orders=%d",
             week.label, visits, leads, opps, orders)

    if visits == 0:
        raise MetricError(f"No funnel events recorded for {week.label}.")

    return {
        "metric": "funnel_snapshot",
        "week": week.label,
        "week_start_date": week.week_start_date.isoformat(),
        "stages": {"visit": visits, "lead": leads,
                   "opportunity": opps, "order": orders},
        "revenue": round(float(r.revenue or 0.0), 2),
        "conversion_pct": {
            "visit_to_lead": _pct(leads, visits),
            "lead_to_opportunity": _pct(opps, leads),
            "opportunity_to_order": _pct(orders, opps),
            "visit_to_order": _pct(orders, visits),
        },
        "drop_off_pct": {
            "visit_to_lead": _pct(visits - leads, visits),
            "lead_to_opportunity": _pct(leads - opps, leads),
            "opportunity_to_order": _pct(opps - orders, opps),
        },
        "provenance": _provenance(
            lakehouse.snapshot(FACT_FUNNEL), week.date_range,
            "counts per stage for the ISO week; conversion = downstream / upstream "
            "* 100; drop-off = 100 - conversion",
            [FACT_FUNNEL, FACT_ORDERS, DIM_DATE]),
    }


def weekly_trend(measure: str = "lead", weeks: int = 12) -> dict:
    """Recent weeks of a measure with WoW and YoY deltas.

    measure: a funnel stage (visit/lead/opportunity), or 'orders' / 'revenue'.
    """
    weeks = max(1, min(int(weeks), MAX_TOOL_ROWS))
    log.info("weekly_trend: measure=%s, weeks=%d", measure, weeks)

    if measure in FUNNEL_STAGES:
        source, tables = FACT_FUNNEL, [FACT_FUNNEL, DIM_DATE]
        arrow = {"fact_funnel_event": lakehouse.arrow(FACT_FUNNEL),
                 "dim_date": lakehouse.arrow(DIM_DATE)}
        sql = f"""
            WITH weekly AS ({WEEKLY_FUNNEL_CTE})
            SELECT c.iso_year, c.iso_week, c.week_start_date, c.events AS value,
                   LAG(c.events) OVER (ORDER BY c.iso_year, c.iso_week) AS prior_week,
                   p.events AS prior_year
            FROM weekly c
            LEFT JOIN weekly p ON p.stage = c.stage
                              AND p.iso_year = c.iso_year - 1
                              AND p.iso_week = c.iso_week
            WHERE c.stage = ?
            ORDER BY c.iso_year DESC, c.iso_week DESC LIMIT ?
        """
        params = [measure, weeks]
    elif measure in ("orders", "revenue"):
        source, tables = FACT_ORDERS, [FACT_ORDERS, DIM_DATE]
        arrow = {"fact_orders": lakehouse.arrow(FACT_ORDERS),
                 "dim_date": lakehouse.arrow(DIM_DATE)}
        col = measure
        sql = f"""
            WITH weekly AS ({WEEKLY_ORDERS_CTE})
            SELECT c.iso_year, c.iso_week, c.week_start_date, c.{col} AS value,
                   LAG(c.{col}) OVER (ORDER BY c.iso_year, c.iso_week) AS prior_week,
                   p.{col} AS prior_year
            FROM weekly c
            LEFT JOIN weekly p ON p.iso_year = c.iso_year - 1
                              AND p.iso_week = c.iso_week
            ORDER BY c.iso_year DESC, c.iso_week DESC LIMIT ?
        """
        params = [weeks]
    else:
        raise MetricError(
            f"Unknown measure {measure!r}. Use one of: "
            f"{', '.join(FUNNEL_STAGES)}, orders, revenue.")

    df = lakehouse.query(sql, arrow, params).sort_values(["iso_year", "iso_week"])
    log.info("weekly_trend: query returned %d rows", len(df))

    points = []
    for r in df.itertuples():
        value = None if pd.isna(r.value) else float(r.value)
        points.append({
            "week": f"{int(r.iso_year)}-W{int(r.iso_week):02d}",
            "week_start_date": _as_date(r.week_start_date).isoformat(),
            "value": value,
            "wow_pct": _change(value, None if pd.isna(r.prior_week) else float(r.prior_week)),
            "yoy_pct": _change(value, None if pd.isna(r.prior_year) else float(r.prior_year)),
            "prior_year_value": None if pd.isna(r.prior_year) else float(r.prior_year),
        })

    covered = ({"start": points[0]["week_start_date"],
                "end": points[-1]["week_start_date"]} if points else {})

    return {
        "metric": "weekly_trend",
        "measure": measure,
        "weeks_returned": len(points),
        "points": points,
        "provenance": _provenance(
            lakehouse.snapshot(source), covered,
            f"weekly {measure} by (iso_year, iso_week); wow_pct vs the preceding "
            "week; yoy_pct vs the same ISO week one year earlier",
            tables),
    }


def running_total(measure: "str | list[str]" = "orders", period: str = "mtd",
                  year: int | None = None) -> dict:
    """Running total of one or several measures within a period: wtd, mtd or ytd.

    measure accepts a LIST as well as a single value, because "the running total
    of visits, leads, opportunities and orders" is one question to a user and
    they ask it constantly. Forcing that into four separate calls put the whole
    burden on the model to fan out correctly, and a 7B model does not: it sends
    the list anyway, gets a validation error, and -- worst case -- fabricates
    plausible-looking numbers rather than admit the call failed. Answering the
    question the user actually asked removes the failure mode at its source.
    """
    log.info("running_total: measure=%s, period=%s, year=%s", measure, period, year)
    if isinstance(measure, list):
        unique = list(dict.fromkeys(measure))       # dedupe, keep the model's order
        if not unique:
            raise MetricError("No measure requested.")
        if len(unique) > 1:
            return _running_total_many(unique, period, year)
        measure = unique[0]

    return _running_total_one(measure, period, year)


def _running_total_many(measures: list[str], period: str, year: int | None) -> dict:
    """Several measures over the same window, each with its own receipts.

    Funnel stages and orders live in different Iceberg tables and therefore
    different snapshots, so each series keeps its own provenance. The top-level
    envelope summarises honestly rather than pretending one snapshot covers all.
    """
    series = {m: _running_total_one(m, period, year) for m in measures}

    tables = sorted({t for s in series.values()
                     for t in s["provenance"]["source_tables"]})
    starts = [s["points"][0]["date"] for s in series.values()]
    ends = [s["points"][-1]["date"] for s in series.values()]

    # Group the measures by the snapshot they actually came from. Funnel stages
    # and orders live in different tables, so one answer legitimately spans two
    # snapshots.
    #
    # Each id MUST travel next to its OWN commit time. An earlier version listed
    # the ids together and gave a single (latest) timestamp beside them; the model
    # read that envelope and confidently paired each id with the wrong
    # timestamp -- a fabricated receipt, which is precisely what provenance
    # exists to prevent. Ambiguity in the envelope becomes a lie in the prose.
    groups: dict[tuple, dict] = {}
    for m, s in series.items():
        p = s["provenance"]
        key = (p["snapshot_id"], p["snapshot_committed_at"])
        groups.setdefault(key, {"snapshot_id": p["snapshot_id"],
                                "snapshot_committed_at": p["snapshot_committed_at"],
                                "source_tables": p["source_tables"],
                                "measures": []})["measures"].append(m)
    snapshots = list(groups.values())

    latest = max(dt.datetime.fromisoformat(g["snapshot_committed_at"])
                 for g in snapshots)

    envelope = _provenance(
        Snapshot(snapshot_id=", ".join(g["snapshot_id"] for g in snapshots),
                 committed_at=latest,
                 operation="append",
                 total_records=None),
        {"start": min(starts), "end": max(ends)},
        f"daily {', '.join(measures)}, each cumulatively summed within "
        f"each {period.upper()} window"
        + (f", restricted to {year}" if year is not None else ""),
        tables)

    # The top-level snapshot_id/snapshot_committed_at are a SUMMARY for the UI
    # strip. "snapshots" is the authoritative, unambiguous pairing: each id sits
    # in the same entry as its own commit time and the measures it covers, so
    # there is no way to read one id's timestamp off another's row.
    #
    # Guidance for the model belongs in the system prompt, not in here -- a note
    # embedded in the payload gets recited back to the user as though it were
    # part of the answer.
    envelope["snapshots"] = snapshots

    return {
        "metric": "running_total",
        "measures": measures,
        "period": period,
        "year": year,
        "series": {m: {"latest": s["latest"], "points": s["points"],
                       "provenance": s["provenance"]}
                   for m, s in series.items()},
        "totals": {m: s["latest"]["running_total"] for m, s in series.items()},
        "provenance": envelope,
    }


def _running_total_one(measure: str = "orders", period: str = "mtd",
                       year: int | None = None) -> dict:
    """Running total of a single measure within a period: wtd, mtd or ytd.

    The window resets at each period boundary -- that is what "within a period"
    means, and it is the same reset Power BI's DATESMTD/DATESYTD perform.

    year restricts the result to one calendar year. Without it the LIMIT below
    keeps only the most RECENT rows, so a question about a specific past year
    would silently be answered with data from the latest year instead -- a wrong
    number reported confidently, which is worse than an error.
    """
    log.info("_running_total_one: measure=%s, period=%s, year=%s", measure, period, year)
    period = (period or "mtd").strip().lower()
    partitions = {
        "wtd": "d.iso_year, d.iso_week",  # ISO calendar for the weekly grain
        "mtd": "d.year, d.month",         # civil calendar for month/year grains
        "ytd": "d.year",
    }
    if period not in partitions:
        raise MetricError(f"period must be wtd, mtd or ytd -- got {period!r}.")

    # A cumulative average is not an average of anything meaningful, so
    # avg_deal_size is excluded here even though _measure_source can build it.
    if measure == "avg_deal_size":
        raise MetricError(
            "avg_deal_size has no running total. Ask for average deal size over a "
            "week or compare it between two periods instead.")

    source, fact, agg, stage = _measure_source(measure)

    stage_filter = "AND f.stage = ?" if stage else ""
    params: list = [stage] if stage else []

    year_filter = ""
    if year is not None:
        year_filter = "AND d.year = ?"
        params.append(int(year))

    arrow = _arrow_for(source)

    df = lakehouse.query(
        f"""
        WITH daily AS (
            SELECT d.date, {partitions[period]}, {agg} AS value
            FROM {fact}
            JOIN dim_date d ON f.date_key = d.date_key
            WHERE 1=1 {stage_filter} {year_filter}
            GROUP BY d.date, {partitions[period]}
        )
        SELECT date, value,
               SUM(value) OVER (
                   PARTITION BY {partitions[period].replace('d.', '')}
                   ORDER BY date
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS running_total
        FROM daily
        ORDER BY date DESC LIMIT ?
        """,
        arrow, params + [MAX_TOOL_ROWS],
    ).sort_values("date")

    if df.empty:
        raise MetricError(
            f"No {measure} data found"
            + (f" for {year}." if year is not None else ".")
            + (" The loaded data may not cover that year." if year is not None else ""))

    points = [{"date": _as_date(r.date).isoformat(),
               "value": float(r.value),
               "running_total": float(r.running_total)} for r in df.itertuples()]

    return {
        "metric": "running_total",
        "measure": measure,
        "period": period,
        "year": year,
        "points": points,
        "latest": points[-1],
        "provenance": _provenance(
            lakehouse.snapshot(source),
            {"start": points[0]["date"], "end": points[-1]["date"]},
            f"daily {measure}, cumulatively summed within each {period.upper()} "
            f"window; the partition resets at each period boundary",
            [source, DIM_DATE]),
    }


# The dimensions an order can be ranked by, and where each one lives.
# region/segment hang off the CUSTOMER, not the order, so they join through
# customer_key -- which is why one table serves two dimensions here.
_DIMENSIONS = {
    "channel": ("dim_channel", "channel_key", "channel_name"),
    "product": ("dim_product", "product_key", "product_name"),
    "region": ("dim_customer", "customer_key", "region"),
    "segment": ("dim_customer", "customer_key", "segment"),
}

# COUNT(*) counts ORDER LINES: fact_orders has no order id to group by, so one
# multi-line order counts once per line. That is the only "deal" grain the data
# supports, and avg_deal_size therefore means average revenue per order line.
_ORDER_MEASURES = {
    "orders": "COUNT(*)",
    "revenue": "SUM(o.revenue)",
    "avg_deal_size": "SUM(o.revenue) / COUNT(*)",
}


def top_dimension(dimension: str = "channel", measure: str = "orders",
                  iso_year: int | None = None, iso_week: int | None = None,
                  limit: int = 5) -> dict:
    """Rank channels, products, regions or segments by orders, revenue or
    average deal size for a week."""
    log.info("top_dimension: dimension=%s, measure=%s, iso_year=%s, iso_week=%s, limit=%d",
             dimension, measure, iso_year, iso_week, limit)
    limit = max(1, min(int(limit), 50))
    if dimension not in _DIMENSIONS:
        raise MetricError(f"dimension must be one of "
                          f"{', '.join(_DIMENSIONS)} -- got {dimension!r}.")
    if measure not in _ORDER_MEASURES:
        raise MetricError(f"measure must be one of "
                          f"{', '.join(_ORDER_MEASURES)} -- got {measure!r}.")

    dim_table, dim_key, dim_label = _DIMENSIONS[dimension]
    agg = _ORDER_MEASURES[measure]
    week = resolve_week(iso_year, iso_week)

    df = lakehouse.query(
        f"""
        SELECT dm.{dim_label} AS name, {agg} AS value,
               COUNT(*) AS orders, SUM(o.revenue) AS revenue,
               SUM(o.revenue) / COUNT(*) AS avg_deal_size
        FROM fact_orders o
        JOIN dim_date d ON o.date_key = d.date_key
        JOIN {dim_table} dm ON o.{dim_key} = dm.{dim_key}
        WHERE d.iso_year = ? AND d.iso_week = ?
        GROUP BY dm.{dim_label}
        ORDER BY value DESC LIMIT ?
        """,
        {"fact_orders": lakehouse.arrow(FACT_ORDERS),
         "dim_date": lakehouse.arrow(DIM_DATE),
         dim_table: lakehouse.arrow(dim_table)},
        [week.iso_year, week.iso_week, limit],
    )
    if df.empty:
        raise MetricError(f"No orders recorded in {week.label}, so nothing to rank.")

    return {
        "metric": "top_dimension",
        "dimension": dimension,
        "measure": measure,
        "week": week.label,
        "rows": [{"name": r.name, "value": round(float(r.value), 2),
                  "orders": int(r.orders), "revenue": round(float(r.revenue), 2),
                  "avg_deal_size": round(float(r.avg_deal_size), 2)}
                 for r in df.itertuples()],
        "provenance": _provenance(
            lakehouse.snapshot(FACT_ORDERS), week.date_range,
            f"{measure} grouped by {dim_label}, ordered descending, top {limit}, "
            f"restricted to ISO week ({week.iso_year}, {week.iso_week})"
            + ("; avg_deal_size = SUM(revenue) / COUNT(order lines)"
               if measure == "avg_deal_size" else ""),
            [FACT_ORDERS, DIM_DATE, dim_table]),
    }


def compare_as_of(as_of_date: str, stage: str = "lead") -> dict:
    """Iceberg time travel: the same metric at a historical snapshot and now.

    Filtering TODAY's table by an old date is NOT the same thing -- it would
    include rows backfilled since, so it tells you what we now believe about
    date X, not what we believed ON date X. Reading the snapshot that was live on
    X gives the latter, which is what someone auditing a number they saw that day
    actually means.
    """
    log.info("compare_as_of: as_of_date=%s, stage=%s", as_of_date, stage)
    stage = _validate_stage(stage)
    target = _parse_as_of(as_of_date)

    try:
        historical = lakehouse.snapshot_as_of(FACT_FUNNEL, target)
    except LakehouseError as exc:
        # The table simply has no state at that moment. That is an answerable
        # "no" for the user, not a crash -- and the message names the range that
        # would work, so the model can retry instead of apologising.
        raise MetricError(str(exc)) from exc

    current = lakehouse.snapshot(FACT_FUNNEL)
    log.info("compare_as_of: historical snapshot=%s, current snapshot=%s",
             historical.snapshot_id, current.snapshot_id)

    def count_at(snapshot_id: int | None) -> int:
        df = lakehouse.query(
            "SELECT COUNT(*) AS n FROM fact_funnel_event WHERE stage = ?",
            {"fact_funnel_event": lakehouse.arrow(FACT_FUNNEL, snapshot_id)},
            [stage])
        return int(df.iloc[0].n)

    then = count_at(int(historical.snapshot_id))
    now = count_at(None)

    return {
        "metric": "compare_as_of",
        "stage": stage,
        "as_of_requested": target.isoformat(),
        "historical": {"events": then,
                       "snapshot_id": historical.snapshot_id,
                       "snapshot_committed_at": historical.committed_at.isoformat()},
        "current": {"events": now,
                    "snapshot_id": current.snapshot_id,
                    "snapshot_committed_at": current.committed_at.isoformat()},
        "events_added_since": now - then,
        "provenance": _provenance(
            current, {"as_of": target.isoformat()},
            f"count(stage='{stage}') read from the snapshot live on "
            f"{target.isoformat()} (snapshot {historical.snapshot_id}), compared "
            f"against the current snapshot ({current.snapshot_id}). Iceberg time "
            f"travel -- not a date filter over current data.",
            [FACT_FUNNEL]),
    }


def explain_scan(iso_year: int | None = None, iso_week: int | None = None) -> dict:
    """How many data files a filtered read actually skips.

    The stretch goal ("let the agent explain its own query plan -- which files it
    scanned and why") and the hard proof the brief asks for. A Spark plan showing
    a pushed-down filter proves only that Iceberg was TOLD about the predicate.
    Counting planned files with and without it proves Iceberg used manifest
    statistics to eliminate files WITHOUT opening them.
    """
    log.info("explain_scan: iso_year=%s, iso_week=%s", iso_year, iso_week)
    week = resolve_week(iso_year, iso_week)
    start = week.week_start_date
    end = start + dt.timedelta(days=7)
    start_ts = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
    end_ts = dt.datetime.combine(end, dt.time.min, tzinfo=dt.timezone.utc)
    row_filter = f"event_ts >= '{start_ts.isoformat()}' AND event_ts < '{end_ts.isoformat()}'"

    stats = lakehouse.scan_stats(FACT_FUNNEL, row_filter)
    pct = round(100.0 * stats.files_skipped / stats.files_total, 1) if stats.files_total else 0.0

    return {
        "metric": "explain_scan",
        "week": week.label,
        "row_filter": row_filter,
        "files_total": stats.files_total,
        "files_scanned": stats.files_scanned,
        "files_skipped": stats.files_skipped,
        "files_skipped_pct": pct,
        "explanation": (
            f"Reading one week of {FACT_FUNNEL} planned {stats.files_scanned} of "
            f"{stats.files_total} data files. Iceberg eliminated {stats.files_skipped} "
            f"({pct}%) using per-file partition and column statistics held in the "
            f"manifests -- those files were never opened."),
        "provenance": _provenance(
            lakehouse.snapshot(FACT_FUNNEL), week.date_range,
            "len(scan(row_filter).plan_files()) vs len(scan().plan_files())",
            [FACT_FUNNEL], scan=stats),
    }


def snapshot_history() -> dict:
    """Every snapshot of the funnel fact -- the audit trail behind any number."""
    log.info("snapshot_history: retrieving snapshot history for %s", FACT_FUNNEL)
    history = lakehouse.history(FACT_FUNNEL)
    if not history:
        raise MetricError(f"{FACT_FUNNEL} has no Iceberg snapshots.")
    log.info("snapshot_history: found %d snapshots", len(history))

    # This tool IS the audit trail, so a provenance envelope is arguably circular
    # -- but dispatch() enforces provenance on every tool without exception, and
    # omitting it here made dispatch raise RuntimeError on every call, which the
    # MCP layer turned into a non-JSON error block. The contract is cheap to
    # honour and the guardrail is worth more than the exemption.
    return {
        "metric": "snapshot_history",
        "table": FACT_FUNNEL,
        "snapshots": [s.as_dict() for s in history],
        "current_snapshot_id": history[-1].snapshot_id,
        "provenance": _provenance(
            history[-1],
            {"start": history[0].committed_at.date().isoformat(),
             "end": history[-1].committed_at.date().isoformat()},
            f"every snapshot recorded in {FACT_FUNNEL}'s Iceberg metadata, "
            f"ordered oldest to newest",
            [FACT_FUNNEL]),
    }


def period_compare(measure: str = "revenue", grain: str = "month",
                   current: str | None = None, previous: str | None = None) -> dict:
    """Compare one measure between two calendar periods of the same grain.

    This is the general "X vs Y" engine behind month-over-month, quarter-over-
    quarter, year-over-year and "the percentage difference between two periods".
    funnel_yoy still owns the same-ISO-week-last-year case, which is a different
    question: it holds the week number fixed rather than stepping back one period.

    Periods are named the way they read: 2026-03, 2026-Q1, 2026-W12, 2026. Omit
    both and you get the two most recent COMPLETE periods, which is what "this
    month vs last month" means against a warehouse whose data stops well short of
    today. Name only `current` and `previous` defaults to the period immediately
    before it on the calendar.

    Completeness is judged against the span the FACT table actually covers, not
    against today: a period is complete only if it starts and ends inside the
    loaded data. A half-loaded month compared against a full one manufactures a
    collapse, and a period outside the data entirely reports as unknown rather
    than as zero.
    """
    log.info("period_compare: measure=%s, grain=%s, current=%s, previous=%s",
             measure, grain, current, previous)
    grain = (grain or "month").strip().lower()
    if grain not in _GRAINS:
        raise MetricError(f"grain must be one of {', '.join(_GRAINS)} -- got {grain!r}.")

    source, fact, agg, stage = _measure_source(measure)
    key = _GRAINS[grain]
    stage_filter = "AND f.stage = ?" if stage else ""
    params = ([stage] if stage else []) * 2      # once for agg, once for bounds

    df = lakehouse.query(
        f"""
        WITH cal AS (
            SELECT {key} AS period, MIN(d.date) AS period_start,
                   MAX(d.date) AS period_end
            FROM dim_date d GROUP BY {key}
        ),
        agg AS (
            SELECT {key} AS period, {agg} AS value
            FROM {fact} JOIN dim_date d ON f.date_key = d.date_key
            WHERE 1=1 {stage_filter}
            GROUP BY {key}
        ),
        bounds AS (
            SELECT MIN(d.date) AS lo, MAX(d.date) AS hi
            FROM {fact} JOIN dim_date d ON f.date_key = d.date_key
            WHERE 1=1 {stage_filter}
        )
        SELECT cal.period, cal.period_start, cal.period_end, agg.value,
               (cal.period_start >= (SELECT lo FROM bounds)
                AND cal.period_end <= (SELECT hi FROM bounds)) AS complete
        FROM cal LEFT JOIN agg ON agg.period = cal.period
        ORDER BY cal.period_start
        """,
        _arrow_for(source), params,
    )
    if df.empty:
        raise MetricError(f"No {measure} data is loaded, so periods cannot be compared.")

    rows: dict[str, dict] = {}
    for r in df.itertuples():
        complete = bool(r.complete)
        value = None if pd.isna(r.value) else float(r.value)
        # Inside the loaded range with no rows is a REAL zero. Outside it, the
        # value is unknown -- never claim a period had none when we simply hold
        # no data for it.
        if complete and value is None:
            value = 0.0
        rows[r.period] = {
            "period": r.period,
            "start": _as_date(r.period_start).isoformat(),
            "end": _as_date(r.period_end).isoformat(),
            "value": value,
            "complete": complete,
        }

    ordered = list(rows)
    complete_periods = [p for p in ordered if rows[p]["complete"]]

    if current is None:
        if len(complete_periods) < 2:
            raise MetricError(
                f"The loaded data covers fewer than two complete {grain}s of "
                f"{measure}, so there is nothing to compare.")
        current, previous = complete_periods[-1], complete_periods[-2]
    else:
        current = _normalise_period(current, grain)
        if current not in rows:
            raise MetricError(_unknown_period(current, grain, complete_periods))
        if previous is None:
            index = ordered.index(current)
            if index == 0:
                raise MetricError(
                    f"{current} is the earliest {grain} in the calendar, so it has "
                    "no preceding period to compare against.")
            previous = ordered[index - 1]

    previous = _normalise_period(previous, grain)
    if previous not in rows:
        raise MetricError(_unknown_period(previous, grain, complete_periods))

    now, before = rows[current], rows[previous]
    pct = _change(now["value"], before["value"])
    delta = (None if now["value"] is None or before["value"] is None
             else round(now["value"] - before["value"], 2))

    incomplete = [p["period"] for p in (now, before) if not p["complete"]]

    return {
        "metric": "period_compare",
        "measure": measure,
        "grain": grain,
        "current": now,
        "previous": before,
        "delta": delta,
        "pct_change": pct,
        "direction": _direction(pct),
        "note": _period_note(incomplete, now, before),
        "provenance": _provenance(
            lakehouse.snapshot(source),
            {"current_period": {"start": now["start"], "end": now["end"]},
             "previous_period": {"start": before["start"], "end": before["end"]}},
            f"{measure} aggregated per {grain} ({current} vs {previous}); "
            f"pct_change = 100 * (current - previous) / previous",
            [source, DIM_DATE]),
    }


def _period_note(incomplete: list[str], now: dict, before: dict) -> str | None:
    """Say out loud why a comparison cannot be trusted, or is undefined."""
    if incomplete:
        return (f"{', '.join(incomplete)} is not fully covered by the loaded data, "
                f"so its total is unknown rather than zero and the comparison is "
                f"not like-for-like.")
    if not before["value"]:
        return (f"{before['period']} recorded none of this measure, so a "
                f"percentage change against it is undefined rather than zero.")
    return None


def _unknown_period(period: str, grain: str, available: list[str]) -> str:
    """A period we hold no data for -- and the ones we do, so a retry can work."""
    if not available:
        return f"No complete {grain} of data is loaded, so {period} cannot be compared."
    shown = available if len(available) <= 12 else available[:6] + ["..."] + available[-6:]
    return (f"{period} is not a complete {grain} in the loaded data. "
            f"Complete {grain}s available: {', '.join(shown)}.")


def daily_trend(measure: str = "lead", days: int = 30) -> dict:
    """The last N days of a measure, one point per day.

    Anchored to the newest day the data HOLDS, not to today. Anchoring to the
    wall clock would return an empty tail of real zeros for days the warehouse
    has simply not loaded yet, and "leads collapsed to nothing" is a far more
    alarming answer than "the data stops on the 3rd".
    """
    days = max(1, min(int(days), MAX_TOOL_ROWS))
    log.info("daily_trend: measure=%s, days=%d", measure, days)
    source, fact, agg, stage = _measure_source(measure)
    stage_filter = "AND f.stage = ?" if stage else ""
    params: list = [stage] if stage else []

    df = lakehouse.query(
        f"""
        SELECT d.date AS date, {agg} AS value
        FROM {fact} JOIN dim_date d ON f.date_key = d.date_key
        WHERE 1=1 {stage_filter}
        GROUP BY d.date
        ORDER BY d.date DESC LIMIT ?
        """,
        _arrow_for(source), params + [days],
    ).sort_values("date")

    if df.empty:
        raise MetricError(f"No {measure} data is loaded, so there is no trend to show.")

    points = [{"date": _as_date(r.date).isoformat(), "value": float(r.value)}
              for r in df.itertuples()]
    values = [p["value"] for p in points]
    first, last = values[0], values[-1]

    return {
        "metric": "daily_trend",
        "measure": measure,
        "days_requested": days,
        "days_returned": len(points),
        "points": points,
        "total": round(sum(values), 2),
        "average_per_day": round(sum(values) / len(values), 2),
        "first_value": first,
        "last_value": last,
        "change_pct": _change(last, first),
        "note": (f"The data ends on {points[-1]['date']}, so this covers the "
                 f"{len(points)} most recent days held rather than the {days} days "
                 f"up to today." if len(points) < days else None),
        "provenance": _provenance(
            lakehouse.snapshot(source),
            {"start": points[0]["date"], "end": points[-1]["date"]},
            f"daily {measure} for the {len(points)} most recent days present in the "
            f"data; change_pct = 100 * (last - first) / first",
            [source, DIM_DATE]),
    }


def funnel_anomalies(threshold_pct: float | None = None) -> dict:
    """Stages and conversion steps that fell more than `threshold_pct` week over week.

    Runs at the start of a chat session, so it must be CHEAP and it must not
    raise: a warehouse that cannot answer this yet is not a reason to refuse the
    user's actual question. Every failure path returns an empty anomaly list with
    a reason attached.

    Both levels are checked because they fail independently and mean different
    things. A stage COUNT falling tracks volume -- fewer visits drags every later
    stage down with it, and nothing is broken. A CONVERSION RATE falling is the
    stage itself getting worse at its job, which is the one worth interrupting
    someone about.

    Only drops are reported. A 40% jump in leads is not something to open a
    conversation by warning about.
    """
    threshold = abs(float(ANOMALY_THRESHOLD_PCT if threshold_pct is None
                          else threshold_pct))
    log.info("funnel_anomalies: threshold=%s%%", threshold)

    current_week = latest_complete_week()
    prior_week = _week_containing(current_week.week_start_date - dt.timedelta(days=7))
    log.info("funnel_anomalies: comparing %s vs %s", current_week.label, prior_week.label)
    current = funnel_snapshot(current_week.iso_year, current_week.iso_week)
    prior = funnel_snapshot(prior_week.iso_year, prior_week.iso_week)

    anomalies = []

    for stage, now in current["stages"].items():
        change = _change(now, prior["stages"].get(stage))
        if change is not None and change <= -threshold:
            anomalies.append({
                "kind": "stage_volume",
                "name": stage,
                "label": f"{stage} volume",
                "current": now,
                "previous": prior["stages"][stage],
                "change_pct": change,
            })

    for step, now in current["conversion_pct"].items():
        before = prior["conversion_pct"].get(step)
        change = _change(now, before)
        if change is not None and change <= -threshold:
            anomalies.append({
                "kind": "conversion_rate",
                "name": step,
                "label": f"{step.replace('_to_', ' → ')} conversion",
                "current": now,
                "previous": before,
                "change_pct": change,
            })

    anomalies.sort(key=lambda a: a["change_pct"])       # steepest drop first
    log.info("funnel_anomalies: found %d anomalies", len(anomalies))
    for a in anomalies:
        log.info("funnel_anomalies: %s %.1f%% (%s -> %s)",
                 a["label"], a["change_pct"], a["previous"], a["current"])

    return {
        "metric": "funnel_anomalies",
        "threshold_pct": threshold,
        "current_week": current["week"],
        "previous_week": prior["week"],
        "anomalies": anomalies,
        "provenance": _provenance(
            lakehouse.snapshot(FACT_FUNNEL),
            {"current_week": current_week.date_range,
             "previous_week": prior_week.date_range},
            f"stage counts and stage-to-stage conversion rates for "
            f"{current['week']} vs {prior['week']}; flagged when "
            f"100 * (current - previous) / previous <= -{threshold}",
            [FACT_FUNNEL, FACT_ORDERS, DIM_DATE]),
    }


# --- helpers ----------------------------------------------------------------

def _week_containing(day: dt.date) -> Week:
    """The ISO week a given date falls in, per dim_date.

    Read from dim_date rather than computed with isocalendar() so that a week the
    calendar table does not cover raises here, rather than producing a week key
    that silently matches no rows.
    """
    log.debug("_week_containing: looking up week for %s", day)
    df = lakehouse.query(
        "SELECT iso_year, iso_week, MIN(week_start_date) AS ws FROM dim_date "
        "WHERE date = ? GROUP BY iso_year, iso_week",
        {"dim_date": lakehouse.arrow(DIM_DATE)}, [day],
    )
    if df.empty:
        raise MetricError(f"{day.isoformat()} is outside the loaded date range.")
    r = df.iloc[0]
    return Week(int(r.iso_year), int(r.iso_week), _as_date(r.ws))


def _measure_source(measure: str) -> tuple[str, str, str, str | None]:
    """Where a measure comes from: (table, FROM clause, aggregate, stage filter).

    One definition shared by running_total, period_compare and daily_trend. These
    each used to spell the same mapping out themselves, which is how `revenue`
    ends up as SUM in one tool and COUNT in another.
    """
    if measure in FUNNEL_STAGES:
        return FACT_FUNNEL, "fact_funnel_event f", "COUNT(*)", measure
    if measure == "orders":
        return FACT_ORDERS, "fact_orders f", "COUNT(*)", None
    if measure == "revenue":
        return FACT_ORDERS, "fact_orders f", "SUM(f.revenue)", None
    if measure == "avg_deal_size":
        return FACT_ORDERS, "fact_orders f", "SUM(f.revenue) / COUNT(*)", None
    raise MetricError(
        f"Unknown measure {measure!r}. Use one of: {', '.join(FUNNEL_STAGES)}, "
        f"orders, revenue, avg_deal_size.")


def _arrow_for(source: str) -> dict:
    """The Arrow tables a fact-plus-calendar query needs to be handed."""
    alias = "fact_orders" if source == FACT_ORDERS else "fact_funnel_event"
    return {"dim_date": lakehouse.arrow(DIM_DATE), alias: lakehouse.arrow(source)}


def _normalise_period(text: str, grain: str) -> str:
    """Accept the many ways a period gets written; emit the one the SQL key uses.

    "2026-3", "2026-Q1", "q1 2026" all name periods a user would reasonably type
    and a model would reasonably pass through. Normalising here beats rejecting
    them and hoping the retry guesses our exact format.
    """
    t = str(text).strip().upper().replace(" ", "-")
    patterns = {
        "month":   [(r"^(\d{4})-(\d{1,2})$", "{0}-{1:02d}")],
        "quarter": [(r"^(\d{4})-?Q(\d)$", "{0}-Q{1}"),
                    (r"^Q(\d)-(\d{4})$", "{1}-Q{0}")],
        "week":    [(r"^(\d{4})-?W(\d{1,2})$", "{0}-W{1:02d}")],
        "year":    [(r"^(\d{4})$", "{0}")],
    }
    for pattern, template in patterns.get(grain, []):
        m = re.match(pattern, t)
        if m:
            groups = [int(g) for g in m.groups()]
            return template.format(*groups)
    return t


def _pct(numerator: float, denominator: float) -> float | None:
    """Percentage, or None when the denominator is zero.

    Deliberately NOT 0.0: a conversion rate with no upstream traffic is unknown,
    and reporting it as 0% reads as "we converted nobody" rather than "we had
    nobody to convert".
    """
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 2)


def _change(current: float | None, baseline: float | None) -> float | None:
    if current is None or not baseline:
        return None
    return round(100.0 * (current - baseline) / baseline, 1)


def _direction(pct: float | None) -> str:
    """Drives the green/red indicator in the UI."""
    if pct is None:
        return "unknown"
    return "up" if pct > 0 else "down" if pct < 0 else "flat"