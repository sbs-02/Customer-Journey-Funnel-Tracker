"""
Generates the simulated incremental batch of new funnel events and orders.
Extends dim_date.csv with any missing dates before generating events, so
every new row gets a real date_key instead of a placeholder sentinel.
Writes data to data/raw/fact_funnel_event_new.csv and data/raw/fact_orders_new.csv
"""

import csv, os, sys, random, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_generation.dim_date import DIM_DATE_COLUMNS, dim_date_row

OUT = Path("data/raw")
OUT.mkdir(parents=True, exist_ok=True)

random.seed(43)   # fixed seed (distinct from generate_data.py's 42) for reproducible incremental batches

# --- Channel config (2026 changes) ---
CHANNELS = ["Paid Search", "Email", "Organic", "Social"]

# 2025 rates: Paid 18%, Email 30%, Organic 22%, Social 12%
# 2026 changes: Email up (better targeting), Paid Search down (competition), Social up slightly
LEAD_RATE = {"Paid Search": .12, "Email": .42, "Organic": .25, "Social": .15}

# 2025: OPP_RATE=0.30, ORDER_RATE=0.35
# 2026: better sales process, higher close rate
OPP_RATE = .40
ORDER_RATE = .42

# 2026 product mix: Enterprise gets a price bump to $349
PRODUCTS = [
    ("Starter Plan", "Software", 29.0),
    ("Pro Plan", "Software", 99.0),
    ("Enterprise Plan", "Software", 299.0),
]

# Channel traffic share shifts throughout 2026:
#   Q1: Paid Search still dominant but shrinking
#   Q2-Q3: Social surge (viral campaign), Email grows
#   Q4: Social stays high, Paid Search stabilises at lower level
def channel_weights(month: int) -> list[float]:
    """Return normalised [Paid Search, Email, Organic, Social] weights for the month."""
    if month <= 2:          # Jan-Feb: budget cut, Paid Search still biggest share
        return [0.35, 0.20, 0.30, 0.15]
    elif month <= 4:        # Mar-Apr: Social starts ramping
        return [0.25, 0.25, 0.25, 0.25]
    elif month <= 6:        # May-Jun: viral Social campaign peaks
        return [0.15, 0.25, 0.20, 0.40]
    elif month <= 9:        # Jul-Oct: Social dominant, Paid Search minimal
        return [0.12, 0.28, 0.20, 0.40]
    else:                   # Nov-Dec: budget flush, Paid Search climbs back a bit
        return [0.20, 0.25, 0.20, 0.35]


NEW_START = dt.date(2026, 1, 1)
NEW_END = dt.date(2026, 12, 31)

# --- 2026 daily visits: Q1 dip, Q2-Q3 surge, Q4 taper ---
# 2025 exited at DAILY_VISITS * 1.25 = 150.
# 2026: Jan-Feb 100 (-33%), Mar-Apr 140, May-Aug 180 (+20% surge), Sep-Oct 160, Nov-Dec 150
def daily_visits_for_date(d: dt.date) -> int:
    """Base daily visit count reflecting 2026's business trajectory."""
    m = d.month
    if m <= 2:
        base = 100       # Q1 dip: budget cut
    elif m <= 4:
        base = 140       # Q2 ramp
    elif m <= 8:
        base = 180       # Q2-Q3 surge: viral Social campaign
    elif m <= 10:
        base = 160       # early autumn taper
    else:
        base = 150       # Q4 stabilise
    return base


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


def weighted_choice(items: list, weights: list[float]):
    """Pick from items according to normalised weights."""
    return random.choices(items, weights=weights, k=1)[0]


print("--- Generating new event batch data (2026 with business changes) ---")

new_dates = list(daterange(NEW_START, NEW_END))

dim_rows, date_key, next_key = load_dim_date()
if extend_dim_date(dim_rows, date_key, next_key, new_dates):
    write_dim_date(dim_rows)
    print(f"Extended dim_date.csv with {len(new_dates)} new date(s)")

orders_new_f = open(OUT / "fact_orders_new.csv", "w", newline="")
ow = csv.writer(orders_new_f)
ow.writerow(["order_line_key", "date_key", "customer_key", "channel_key", "product_key",
             "revenue", "quantity", "order_ts"])
order_id = 100000

# Product weight shifts: more Pro/Enterprise in 2026 (upselling push)
# 2025 was uniform random across 3 products.
# 2026: Starter 30%, Pro 45%, Enterprise 25%
PRODUCT_WEIGHTS = [0.30, 0.45, 0.25]

with open(OUT / "fact_funnel_event_new.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["event_key", "date_key", "customer_key", "channel_key", "stage", "event_ts"])

    event_id = 500000

    for current_date in new_dates:
        dk = date_key[current_date.isoformat()]
        month = current_date.month

        # Seasonal wave: different from 2025 -- stronger Q2 (renewal season) and Q4 (budget flush)
        week = current_date.isocalendar().week
        if month in (5, 6):                    # Q2 peak (renewals)
            season = 1.15
        elif month in (11, 12):                # Q4 peak (budget flush)
            season = 1.10
        elif month in (1, 2):                  # Q1 trough
            season = 0.85
        else:
            season = 1.0

        base_visits = daily_visits_for_date(current_date)
        visits = int(base_visits * season * random.uniform(0.88, 1.12))

        # Channel weights shift throughout the year
        cw = channel_weights(month)

        for _ in range(visits):
            cust_key = random.randint(0, 2999)
            chan = weighted_choice(CHANNELS, cw)
            chan_key = CHANNELS.index(chan)

            ts = dt.datetime.combine(current_date, dt.time(random.randint(0, 23), random.randint(0, 59)))
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")

            # 1. Visit
            w.writerow([event_id, dk, cust_key, chan_key, "visit", ts_str])
            event_id += 1

            # 2. Lead (using 2026 rates -- higher for Email, lower for Paid Search)
            if random.random() < LEAD_RATE[chan]:
                w.writerow([event_id, dk, cust_key, chan_key, "lead", ts_str])
                event_id += 1

                # 3. Opportunity (2026: 40% -- better sales team)
                if random.random() < OPP_RATE:
                    w.writerow([event_id, dk, cust_key, chan_key, "opportunity", ts_str])
                    event_id += 1

                    # 4. Order (2026: 42% -- better close rate)
                    if random.random() < ORDER_RATE:
                        prod = random.choices(range(len(PRODUCTS)), weights=PRODUCT_WEIGHTS, k=1)[0]
                        qty = random.randint(1, 4)     # up to 4 units (was 3) -- bulk deals
                        ow.writerow([order_id, dk, cust_key, chan_key, prod,
                                     PRODUCTS[prod][2] * qty, qty, ts_str])
                        order_id += 1

orders_new_f.close()
print(f"Successfully generated new file: {OUT / 'fact_funnel_event_new.csv'} ({event_id - 500000} records)")
print(f"Successfully generated new file: {OUT / 'fact_orders_new.csv'} ({order_id - 100000} records)")
