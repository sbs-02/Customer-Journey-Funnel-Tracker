"""
Generates synthetic sales and marketing data.
Creates a data/raw directory if it does not exist.
Generates date, customer, channel, and product dimension tables.
Simulates customer funnel events (visit → lead → opportunity).
Simulates customer orders based on conversion probabilities.
Writes all dimension and fact tables as CSV files.
Uses a fixed random seed for reproducible datasets.
Prints the total number of generated events and orders.
"""

import os, csv, random, sys, datetime as dt
from pathlib import Path
from faker import Faker
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_generation.dim_date import DIM_DATE_COLUMNS, dim_date_row

load_dotenv()

fake = Faker()
random.seed(42)                       # same "random" data every run — reproducible
OUT = Path("data/raw"); OUT.mkdir(parents=True, exist_ok=True)

CHANNELS = ["Paid Search", "Email", "Organic", "Social"]
LEAD_RATE = {"Paid Search": .18, "Email": .30, "Organic": .22, "Social": .12}
OPP_RATE, ORDER_RATE = .30, .35
PRODUCTS = [("Starter Plan", "Software", 29.0), ("Pro Plan", "Software", 99.0),
            ("Enterprise Plan", "Software", 299.0)]
START, END = dt.date(2022, 1, 3), dt.date(2025, 12, 31)
DAILY_VISITS = int(os.environ.get("DAILY_VISITS", "120"))

def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)

dates = list(daterange(START, END))
with open(OUT / "dim_date.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=DIM_DATE_COLUMNS)
    w.writeheader()
    for i, d in enumerate(dates):
        w.writerow(dim_date_row(i, d))
date_key = {d.isoformat(): i for i, d in enumerate(dates)}

dates = list(daterange(START, END))

customers = [(i, fake.uuid4(), fake.name(), random.choice(["SMB", "Mid-Market", "Enterprise"]),
              fake.state(), fake.date_between(START, END).isoformat()) for i in range(3000)]
with open(OUT / "dim_customer.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["customer_key", "customer_id", "name", "segment", "region", "signup_date"])
    w.writerows(customers)

with open(OUT / "dim_channel.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["channel_key", "channel_name", "channel_group"])
    for i, c in enumerate(CHANNELS):
        w.writerow([i, c, "Paid" if "Paid" in c else "Organic"])
channel_key = {c: i for i, c in enumerate(CHANNELS)}

with open(OUT / "dim_product.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["product_key", "product_name", "category", "unit_price"])
    for i, (name, cat, price) in enumerate(PRODUCTS):
        w.writerow([i, name, cat, price])

events_f = open(OUT / "fact_funnel_event.csv", "w", newline="")
orders_f = open(OUT / "fact_orders.csv", "w", newline="")
ew = csv.writer(events_f); ow = csv.writer(orders_f)
ew.writerow(["event_key", "date_key", "customer_key", "channel_key", "stage", "event_ts"])
ow.writerow(["order_line_key", "date_key", "customer_key", "channel_key", "product_key",
             "revenue", "quantity", "order_ts"])

event_id = order_id = 0
for d in dates:
    growth = 1.0 if d.year == 2022 else 1.08 if d.year == 2023 else 1.15 if d.year == 2024 else 1.25 
    season = 1 + 0.15 * ((d.isocalendar().week % 8) / 8)   # a gentle wave, not a flat line
    visits = int(DAILY_VISITS * growth * season * random.uniform(.9, 1.1))
    for _ in range(visits):
        cust = random.choice(customers)
        chan = random.choice(CHANNELS)
        ts = dt.datetime.combine(d, dt.time(random.randint(0, 23), random.randint(0, 59)))
        ew.writerow([event_id, date_key[d.isoformat()], cust[0], channel_key[chan], "visit", ts]); event_id += 1
        if random.random() < LEAD_RATE[chan]:                       # not everyone becomes a lead
            ew.writerow([event_id, date_key[d.isoformat()], cust[0], channel_key[chan], "lead", ts]); event_id += 1
            if random.random() < OPP_RATE:                          # fewer still become opportunities
                ew.writerow([event_id, date_key[d.isoformat()], cust[0], channel_key[chan], "opportunity", ts]); event_id += 1
                if random.random() < ORDER_RATE:                     # fewest place an order
                    prod = random.randrange(len(PRODUCTS))
                    qty = random.randint(1, 3)
                    ow.writerow([order_id, date_key[d.isoformat()], cust[0], channel_key[chan], prod,
                                 PRODUCTS[prod][2] * qty, qty, ts]); order_id += 1

events_f.close(); orders_f.close()
print(f"events={event_id} orders={order_id}")