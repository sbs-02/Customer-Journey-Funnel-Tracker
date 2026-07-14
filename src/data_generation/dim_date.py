"""
Single builder for dim_date rows.

Both generate_data.py (initial load) and generate_new_batch.py (incremental
batch) emit dim_date rows. They each used to hard-code their own column list,
which is how the calendar-year/ISO-year bug got in and stayed in. Both now go
through dim_date_row() so the two cannot drift apart.

The important column is iso_year.

    date        calendar year   ISO year   ISO week
    2022-01-01  2022            2021       52
    2024-12-30  2024            2025       1

A weekly metric keyed on (year, iso_week) puts 2022-01-01 and 2022-12-31 in the
SAME bucket -- 364 days apart. Weekly metrics must key on (iso_year, iso_week).

Column order here is the contract with DIM_DATE_SCHEMA in schemas.py.
"""

import datetime as dt

DIM_DATE_COLUMNS = [
    "date_key", "date", "year", "quarter", "month", "month_name",
    "iso_year", "iso_week", "week_start_date", "day_of_week",
]


def dim_date_row(date_key: int, d: dt.date) -> dict:
    """Build one dim_date row. iso_year/iso_week come from the ISO calendar,
    year/quarter/month from the civil calendar; they disagree at year edges."""
    iso_year, iso_week, iso_weekday = d.isocalendar()
    return {
        "date_key": date_key,
        "date": d.isoformat(),
        "year": d.year,
        "quarter": (d.month - 1) // 3 + 1,
        "month": d.month,
        "month_name": d.strftime("%B"),
        "iso_year": iso_year,
        "iso_week": iso_week,
        # Monday of this ISO week. Lets Power BI sort weeks chronologically and
        # gives the agent a human-readable week label.
        "week_start_date": (d - dt.timedelta(days=iso_weekday - 1)).isoformat(),
        "day_of_week": d.strftime("%A"),
    }