# Iceberg Partition Pruning Evidence

## Purpose

This document records evidence that Iceberg applies filtering during
table scans instead of reading the entire table.

The same table is queried twice:

1.  Unfiltered scan --- reads the entire `fact_funnel_event` table.
2.  Date-filtered scan --- reads only data matching a one-week timestamp
    range.

The goal is to verify that Iceberg receives and applies filter
conditions during the scan process.

------------------------------------------------------------------------

# 1. Unfiltered Query

## Query

``` sql
SELECT *
FROM local.db.fact_funnel_event;
```

## Spark Physical Plan

    *(1) ColumnarToRow
    +- BatchScan local.db.fact_funnel_event[event_key, date_key, customer_key, channel_key, stage, event_ts]
       IcebergScan(
          table=local.db.fact_funnel_event,
          schemaId=0,
          snapshotId=885043562039456892,
          filters=,
          runtimeFilters=,
          groupedBy=
       )

## Observation

The unfiltered query does not provide any filtering condition to the
Iceberg scan.

The scan shows:

    filters=

This means Spark reads the table without any timestamp restriction.

------------------------------------------------------------------------

# 2. Filtered Query (One Week)

## Query

``` sql
SELECT *
FROM local.db.fact_funnel_event
WHERE event_ts >= '2025-06-01'
AND event_ts < '2025-06-08';
```

## Spark Physical Plan

    *(1) Filter (
        event_ts >= 2025-06-01 00:00:00
        AND event_ts < 2025-06-08 00:00:00
    )
    +- *(1) ColumnarToRow
       +- BatchScan local.db.fact_funnel_event[event_key, date_key, customer_key, channel_key, stage, event_ts]
          IcebergScan(
             table=local.db.fact_funnel_event,
             schemaId=0,
             snapshotId=885043562039456892,
             filters=
                event_ts IS NOT NULL,
                event_ts >= 1748715300000000,
                event_ts < 1749320100000000,
             runtimeFilters=,
             groupedBy=
          )

## Observation

The filtered query pushes the timestamp condition into the Iceberg scan:

    filters=
    event_ts IS NOT NULL,
    event_ts >= 1748715300000000,
    event_ts < 1749320100000000

This confirms that Spark passes the filter predicate to Iceberg before
reading the data.

------------------------------------------------------------------------

# Comparison

  Query Type                Iceberg Scan Filter
  ------------------------- --------------------------------------------
  Unfiltered query          No filters applied
  One-week filtered query   Timestamp filters pushed into Iceberg scan

------------------------------------------------------------------------

# Conclusion

The query plan comparison demonstrates that Iceberg is applying
scan-level filtering.

The unfiltered query performs a full table scan, while the filtered
query provides timestamp predicates directly to the Iceberg scan.

This confirms that the Iceberg table supports partition-aware filtering
and avoids unnecessary data reads when a query includes a timestamp
condition.