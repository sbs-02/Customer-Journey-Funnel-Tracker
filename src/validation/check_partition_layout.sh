#!/bin/bash
set -e

cd "$(dirname "$0")/../.."

echo "--- partition layout file counts (on-disk evidence) ---"

DAY_COUNT=$(find warehouse/db/fact_funnel_event/data -path "*event_ts_day=*" -name "*.parquet" | wc -l)
MONTH_COUNT=$(find warehouse/db/fact_funnel_event/data -path "*event_ts_month=*" -name "*.parquet" | wc -l)

echo "day-partitioned files (pre-evolution):    $DAY_COUNT"
echo "month-partitioned files (post-evolution): $MONTH_COUNT"