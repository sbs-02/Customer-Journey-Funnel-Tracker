# Iceberg File-Level Pruning Evidence

## Purpose

This document records evidence that Iceberg skips opening data files
entirely during a scan, rather than opening every file and filtering rows
after the fact.

The same table is queried with three predicates of increasing selectivity:

1.  Unfiltered scan --- plans every data file in the table.
2.  One-month filter --- plans only files matching a one-month timestamp
    range.
3.  One-week filter --- plans only files matching a one-week timestamp
    range.

The goal is to verify that Iceberg eliminates files at the manifest level
using stored statistics, without reading the files themselves.

------------------------------------------------------------------------

# 1. File-Pruning Counts

## Command

``` bash
uv run python -m src.validation.check_file_pruning
```

## Result

  Filter                              Files Planned   Files Skipped   % Skipped
  ------------------------------------ --------------- --------------- -----------
  None                                  1511            ---             ---
  One month (`2025-06-01`--`07-01`)     31              1480            98.0%
  One week (`2025-06-02`--`06-09`)      7               1504            99.5%

## Observation

The narrowing is monotonic: a smaller predicate range excludes more files,
not fewer. None of the 1504 files skipped under the one-week filter were
opened --- Iceberg eliminated them using per-file partition and column
statistics already stored in the manifests, without reading a single row.

------------------------------------------------------------------------

# 2. Partition Spec Confirmation

## Result

  Spec     Field              Transform
  -------- ------------------ -----------
  spec 0   `event_ts_day`      day
  spec 1   `event_ts_month`    month

## Observation

Both specs coexist on the table. Files written before the evolution remain
under spec 0 and are still read correctly under it; new files are written
under spec 1. No rewrite occurred, consistent with the on-disk file counts
in `partition_evolution_evidence.md`.

------------------------------------------------------------------------

# Conclusion

File-planning counts confirm Iceberg skips data files at the manifest level
rather than opening and filtering them row-by-row, and the pruning benefit
holds across both coexisting partition specs. Combined with
`partition_evolution_evidence.md` and `check_partition_layout.sh`, this
completes the evidence that partition evolution here was metadata-only and
did not degrade query-time pruning.