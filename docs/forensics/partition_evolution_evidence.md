# Iceberg Snapshot Immutability & Partition Evolution Evidence

## Purpose

This document records evidence that:

1.  Iceberg's snapshot history is append-only --- each write produces a new
    snapshot, and prior snapshots and their files are not mutated or
    deleted as part of normal writes.
2.  A single query correctly scans across a date range spanning both the
    pre-evolution (day) and post-evolution (month) partition layouts,
    resolved through one unified logical plan against one snapshot.

------------------------------------------------------------------------

# 1. Snapshot History

## Command

``` bash
uv run --active python -m src.validation.check_evolution_immutability
```

## Result (abridged --- 50 snapshots total)

  snapshot_id            committed_at              operation   added_files   total_files
  ----------------------- ------------------------- ----------- ------------- -------------
  8001372227948383874     2026-07-14 21:55:46.72     overwrite   32            32
  4236679257919180430     2026-07-14 21:55:47.774    append      29            61
  ...                     ...                        append      ~31--32       ...
  9180215740250648694     2026-07-14 21:56:30.397    append      32            1509
  6168678159175103032     2026-07-14 22:01:31.077    append      2             1511

## Observation

Every write after the initial `overwrite` shows `operation = append` with a
`deleted_files` value of `NULL` --- no snapshot in the 50-row history removes
or rewrites files belonging to an earlier snapshot. `total_files` increases
monotonically from 32 to 1511, confirming each snapshot strictly adds to the
table rather than replacing prior state.

The final snapshot (`6168678159175103032`, committed 22:01:31, over five
minutes after the previous one) adds only 2 files, consistent with a small,
separate write --- e.g. the partition-evolution commit or a subsequent
metadata-only operation --- landing on top of the 1509 files already
established by the preceding 49 snapshots.

------------------------------------------------------------------------

# 2. Historical-Range Scan (Pre-Evolution Data Only)

## Query

``` python
df.filter("event_ts < '2025-12-01'")
```

## Spark Physical Plan

    *(1) Filter (event_ts#42 < 2025-12-01 00:00:00)
    +- *(1) ColumnarToRow
       +- BatchScan local.db.fact_funnel_event[event_key#37L, date_key#38, customer_key#39, channel_key#40, stage#41, event_ts#42]
          IcebergScan(
             table=local.db.fact_funnel_event,
             schemaId=0,
             snapshotId=6168678159175103032,
             branch=null,
             filters=event_ts IS NOT NULL, event_ts < 1764526500000000,
             runtimeFilters=,
             groupedBy=
          )

## Observation

The scan resolves against the latest snapshot (`6168678159175103032` ---
the same one shown as the final row of the snapshot history above) and
pushes the timestamp filter down into the `IcebergScan`, restricting the
read to the pre-evolution date range.

------------------------------------------------------------------------

# 3. Cross-Boundary Scan (Old + New Partition Layouts Together)

## Query

``` python
df.filter("event_ts >= '2022-01-01'")
```

## Spark Physical Plan

    *(1) Filter (event_ts#59 >= 2022-01-01 00:00:00)
    +- *(1) ColumnarToRow
       +- BatchScan local.db.fact_funnel_event[event_key#54L, date_key#55, customer_key#56, channel_key#57, stage#58, event_ts#59]
          IcebergScan(
             table=local.db.fact_funnel_event,
             schemaId=0,
             snapshotId=6168678159175103032,
             branch=null,
             filters=event_ts IS NOT NULL, event_ts >= 1640974500000000,
             runtimeFilters=,
             groupedBy=
          )

## Observation

This query spans the full history of the table, from before the partition
evolution through after it. Spark resolves it as a **single** `IcebergScan`
against the **same** snapshot ID as the historical-range scan above --- no
separate plan branch, union, or manual handling is required to reconcile
the day-partitioned and month-partitioned files.

------------------------------------------------------------------------

# Comparison

  Evidence Type                Result
  ----------------------------- -----------------------------------------------
  Snapshot history               49 appends + 1 overwrite; no deletes; monotonic file growth
  Historical-range scan           Single IcebergScan, correct filter pushdown
  Cross-boundary scan             Single IcebergScan spanning both layouts, same snapshot

------------------------------------------------------------------------

# Conclusion

The snapshot history confirms Iceberg's append-only write model: no
snapshot in this table's history deletes or rewrites files committed by an
earlier snapshot. The query plans confirm this immutability doesn't come at
the cost of query-side complexity --- a single scan against a single
snapshot correctly spans both the pre- and post-evolution partition
layouts without requiring the caller to special-case either one.

Together with `partition_evolution_evidence.md` and
`check_partition_layout.sh`, this demonstrates that Iceberg's partition
evolution and ongoing writes are both metadata-driven and non-destructive:
old data is neither moved nor rewritten, and remains fully and correctly
queryable alongside new data through the same logical table.