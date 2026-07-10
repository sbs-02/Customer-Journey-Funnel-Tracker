"""
Generates the simulated incremental batch of new funnel events for Step 10.
Writes data to data/raw/fact_funnel_event_new.csv
"""
import csv
import random
import datetime as dt
from pathlib import Path

OUT = Path("data/raw")
OUT.mkdir(parents=True, exist_ok=True)

# Re-use project parameters from your main data generator
CHANNELS = ["Paid Search", "Email", "Organic", "Social"]
LEAD_RATE = {"Paid Search": .18, "Email": .30, "Organic": .22, "Social": .12}
OPP_RATE = .30

# Simulate fresh events arriving right after the old tracking end date (e.g., January 2026)
NEW_START = dt.date(2026, 1, 1)
NEW_END = dt.date(2026, 1, 5)  # Let's generate a few days of new data

print(f"--- Generating new event batch data ---")

with open(OUT / "fact_funnel_event_new.csv", "w", newline="") as f:
    w = csv.writer(f)
    # Match the exact schema layout your Spark pipeline expects
    w.writerow(["event_key", "date_key", "customer_key", "channel_key", "stage", "event_ts"])
    
    event_id = 500000  # Pick a high starting index to prevent any potential primary key collision
    
    current_date = NEW_START
    while current_date <= NEW_END:
        # Mock daily volume numbers
        visits = int(25 * random.uniform(.9, 1.1))
        
        for _ in range(visits):
            cust_key = random.randint(0, 2999)      # Pointing to keys from dim_customer
            chan = random.choice(CHANNELS)
            chan_key = CHANNELS.index(chan)
            
            # Generate a timestamp for the event
            ts = dt.datetime.combine(current_date, dt.time(random.randint(0, 23), random.randint(0, 59)))
            
            # 1. Write visit stage
            w.writerow([event_id, 9999, cust_key, chan_key, "visit", ts.isoformat()])
            event_id += 1
            
            # 2. Write lead stage conditionally
            if random.random() < LEAD_RATE[chan]:
                w.writerow([event_id, 9999, cust_key, chan_key, "lead", ts.isoformat()])
                event_id += 1
                
                # 3. Write opportunity stage conditionally
                if random.random() < OPP_RATE:
                    w.writerow([event_id, 9999, cust_key, chan_key, "opportunity", ts.isoformat()])
                    event_id += 1
                    
        current_date += dt.timedelta(days=1)

print(f"Successfully generated new file: {OUT / 'fact_funnel_event_new.csv'} ({event_id - 500000} records)")