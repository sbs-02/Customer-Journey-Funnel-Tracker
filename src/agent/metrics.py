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
import os
import sys
from dataclasses import dataclass
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.lakehouse import ScanStats, Snapshot, lakehouse

FUNNEL_STAGES = ("visit", "lead", "opportunity")
FACT_FUNNEL, FACT_ORDERS, DIM_DATE = "fact_funnel_event", "fact_orders", "dim_date"
MAX_TOOL_ROWS = int(os.environ.get("MAX_TOOL_ROWS", "500"))

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
    return Week(int(r.iso_year), int(r.iso_week), _as_date(r.week_start_date))


def resolve_week(iso_year: int | None = None, iso_week: int | None = None) -> Week:
    """Resolve an explicit week, or fall back to the latest complete one."""
    if iso_year is None or iso_week is None:
        return latest_complete_week()
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
    return Week(int(iso_year), int(iso_week), _as_date(ws))


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
    stage = _validate_stage(stage)
    week = resolve_week(iso_year, iso_week)
    snapshot = lakehouse.snapshot(FACT_FUNNEL)

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
    week = resolve_week(iso_year, iso_week)
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

    points = []
    for r in df.itertuples():
        value = None if r.value is None else float(r.value)
        points.append({
            "week": f"{int(r.iso_year)}-W{int(r.iso_week):02d}",
            "week_start_date": _as_date(r.week_start_date).isoformat(),
            "value": value,
            "wow_pct": _change(value, None if r.prior_week is None else float(r.prior_week)),
            "yoy_pct": _change(value, None if r.prior_year is None else float(r.prior_year)),
            "prior_year_value": None if r.prior_year is None else float(r.prior_year),
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


def running_total(measure: str = "orders", period: str = "mtd") -> dict:
    """Running total of a measure within a period: wtd, mtd or ytd.

    The window resets at each period boundary -- that is what "within a period"
    means, and it is the same reset Power BI's DATESMTD/DATESYTD perform.
    """
    period = (period or "mtd").strip().lower()
    partitions = {
        "wtd": "d.iso_year, d.iso_week",  # ISO calendar for the weekly grain
        "mtd": "d.year, d.month",         # civil calendar for month/year grains
        "ytd": "d.year",
    }
    if period not in partitions:
        raise MetricError(f"period must be wtd, mtd or ytd -- got {period!r}.")

    if measure == "orders":
        source, agg, fact = FACT_ORDERS, "COUNT(*)", "fact_orders f"
    elif measure == "revenue":
        source, agg, fact = FACT_ORDERS, "SUM(f.revenue)", "fact_orders f"
    elif measure in FUNNEL_STAGES:
        source, agg, fact = FACT_FUNNEL, "COUNT(*)", "fact_funnel_event f"
    else:
        raise MetricError(
            f"Unknown measure {measure!r}. Use orders, revenue, or a funnel stage.")

    stage_filter = "AND f.stage = ?" if measure in FUNNEL_STAGES else ""
    params: list = [measure] if measure in FUNNEL_STAGES else []

    arrow = {"dim_date": lakehouse.arrow(DIM_DATE),
             ("fact_orders" if source == FACT_ORDERS else "fact_funnel_event"):
                 lakehouse.arrow(source)}

    df = lakehouse.query(
        f"""
        WITH daily AS (
            SELECT d.date, {partitions[period]}, {agg} AS value
            FROM {fact}
            JOIN dim_date d ON f.date_key = d.date_key
            WHERE 1=1 {stage_filter}
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
        raise MetricError(f"No {measure} data found.")

    points = [{"date": _as_date(r.date).isoformat(),
               "value": float(r.value),
               "running_total": float(r.running_total)} for r in df.itertuples()]

    return {
        "metric": "running_total",
        "measure": measure,
        "period": period,
        "points": points,
        "latest": points[-1],
        "provenance": _provenance(
            lakehouse.snapshot(source),
            {"start": points[0]["date"], "end": points[-1]["date"]},
            f"daily {measure}, cumulatively summed within each {period.upper()} "
            f"window; the partition resets at each period boundary",
            [source, DIM_DATE]),
    }


def top_dimension(dimension: str = "channel", measure: str = "orders",
                  iso_year: int | None = None, iso_week: int | None = None,
                  limit: int = 5) -> dict:
    """Rank channels or products by orders or revenue for a week."""
    limit = max(1, min(int(limit), 50))
    dims = {"channel": ("dim_channel", "channel_key", "channel_name"),
            "product": ("dim_product", "product_key", "product_name")}
    if dimension not in dims:
        raise MetricError(f"dimension must be 'channel' or 'product' -- got {dimension!r}.")
    if measure not in ("orders", "revenue"):
        raise MetricError(f"measure must be 'orders' or 'revenue' -- got {measure!r}.")

    dim_table, dim_key, dim_label = dims[dimension]
    agg = "COUNT(*)" if measure == "orders" else "SUM(o.revenue)"
    week = resolve_week(iso_year, iso_week)

    df = lakehouse.query(
        f"""
        SELECT dm.{dim_label} AS name, {agg} AS value,
               COUNT(*) AS orders, SUM(o.revenue) AS revenue
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
                  "orders": int(r.orders), "revenue": round(float(r.revenue), 2)}
                 for r in df.itertuples()],
        "provenance": _provenance(
            lakehouse.snapshot(FACT_ORDERS), week.date_range,
            f"{measure} grouped by {dim_label}, ordered descending, top {limit}, "
            f"restricted to ISO week ({week.iso_year}, {week.iso_week})",
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
    stage = _validate_stage(stage)
    try:
        target = dt.date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise MetricError(f"as_of_date must be YYYY-MM-DD -- got {as_of_date!r}.") from exc

    historical = lakehouse.snapshot_as_of(FACT_FUNNEL, target)
    current = lakehouse.snapshot(FACT_FUNNEL)

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
    history = lakehouse.history(FACT_FUNNEL)
    return {
        "metric": "snapshot_history",
        "table": FACT_FUNNEL,
        "snapshots": [s.as_dict() for s in history],
        "current_snapshot_id": history[-1].snapshot_id if history else None,
    }


# --- helpers ----------------------------------------------------------------

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