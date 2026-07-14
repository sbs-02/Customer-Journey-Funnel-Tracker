"""
Generates the simulated incremental batch of new funnel events.
Extends dim_date.csv with any missing dates before generating events, so
every new row gets a real date_key instead of a placeholder sentinel.
Writes data to data/raw/fact_funnel_event_new.csv
"""

import csv, os, sys, random, datetime as dt
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_generation.dim_date import DIM_DATE_COLUMNS, dim_date_row

OUT = Path("data/raw")
OUT.mkdir(parents=True, exist_ok=True)

random.seed(43)   # fixed seed (distinct from generate_data.py's 42) for reproducible incremental batches

# Re-use project parameters from your main data generator
CHANNELS = ["Paid Search", "Email", "Organic", "Social"]
LEAD_RATE = {"Paid Search": .18, "Email": .30, "Organic": .22, "Social": .12}
OPP_RATE = .30

# Simulate fresh events arriving right after the old tracking end date
NEW_START = dt.date(2026, 1, 1)
NEW_END = dt.date(2026, 1, 5)  # Let's generate a few days of new data

# 2025 exits at DAILY_VISITS * 1.25 growth; keep the new batch on that run-rate.
NEW_DAILY_VISITS = int(int(os.environ.get("DAILY_VISITS", "120")) * 1.25)

def load_dim_date():
    """Read the existing dim_date.csv into (rows, date_key lookup, next_key)."""
    path = OUT / "dim_date.csv"
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    date_key = {row["date"]: int(row["date_key"]) for row in rows}
    next_key = max((int(row["date_key"]) for row in rows), default=-1) + 1
    return rows, date_key, next_key


def extend_dim_date(rows, date_key, next_key, new_dates):
    """Append any dates not already in dim_date, in-place on rows/date_key.
    Returns True if the file needs to be rewritten."""
    added = False
    for d in new_dates:
        iso = d.isoformat()
        if iso not in date_key:
            # Built by the shared dim_date_row() so the incremental batch cannot
            # emit a different column set than generate_data.py's initial load.
            rows.append(dim_date_row(next_key, d))
            date_key[iso] = next_key
            next_key += 1
            added = True
    return added


def write_dim_date(rows):
    with open(OUT / "dim_date.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DIM_DATE_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


print("--- Generating new event batch data ---")

new_dates = list(daterange(NEW_START, NEW_END))

dim_rows, date_key, next_key = load_dim_date()
if extend_dim_date(dim_rows, date_key, next_key, new_dates):
    write_dim_date(dim_rows)
    print(f"Extended dim_date.csv with {len(new_dates)} new date(s)")

with open(OUT / "fact_funnel_event_new.csv", "w", newline="") as f:
    w = csv.writer(f)
    # Matches FACT_FUNNEL_EVENT_SCHEMA in schemas.py
    w.writerow(["event_key", "date_key", "customer_key", "channel_key", "stage", "event_ts"])

    event_id = 500000  # Pick a high starting index to prevent any potential primary key collision

    for current_date in new_dates:
        dk = date_key[current_date.isoformat()]

        # Mock daily volume numbers
        visits = int(NEW_DAILY_VISITS * random.uniform(.9, 1.1))

        for _ in range(visits):
            cust_key = random.randint(0, 2999)      # Pointing to keys from dim_customer
            chan = random.choice(CHANNELS)
            chan_key = CHANNELS.index(chan)

            # Generate a timestamp for the event.
            # Use "%Y-%m-%d %H:%M:%S" (space-separated) to match the format
            # generate_data.py produces via its raw datetime -> csv writer,
            # so event_ts is consistent across both fact_funnel_event.csv
            # and fact_funnel_event_new.csv.
            ts = dt.datetime.combine(current_date, dt.time(random.randint(0, 23), random.randint(0, 59)))
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")

            # 1. Write visit stage
            w.writerow([event_id, dk, cust_key, chan_key, "visit", ts_str])
            event_id += 1

            # 2. Write lead stage conditionally
            if random.random() < LEAD_RATE[chan]:
                w.writerow([event_id, dk, cust_key, chan_key, "lead", ts_str])
                event_id += 1

                # 3. Write opportunity stage conditionally
                if random.random() < OPP_RATE:
                    w.writerow([event_id, dk, cust_key, chan_key, "opportunity", ts_str])
                    event_id += 1

print(f"Successfully generated new file: {OUT / 'fact_funnel_event_new.csv'} ({event_id - 500000} records)")