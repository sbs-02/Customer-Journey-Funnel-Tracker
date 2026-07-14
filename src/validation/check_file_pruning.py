"""
Hard proof that Iceberg's metadata-driven pruning skips data files.

df.explain() shows a filter was PUSHED DOWN into the scan. That is not the same
as proving a file was SKIPPED -- a pushed-down predicate that matches every file
skips nothing, and the plan looks identical either way.

plan_files() asks Iceberg which data files it would actually open. Comparing that
count with and without a predicate is the before/after the brief asks for: the
difference is the set of files Iceberg eliminated using per-file partition and
column statistics held in the manifests, WITHOUT opening them.
"""

import os
import sys
from pathlib import Path

from pyiceberg.table import StaticTable
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE = Path(os.environ.get("ICEBERG_WAREHOUSE", ROOT / "warehouse")).resolve()
NAMESPACE = "db"


def load(table_name: str) -> StaticTable:
    """Open a Hadoop-catalog table read-only.

    PyIceberg has no 'hadoop' catalog type -- load_catalog(type="hadoop") raises
    KeyError: 'HADOOP'. A Hadoop catalog instead records its live metadata
    pointer in version-hint.text. Read that, resolve vN.metadata.json, and open
    it with StaticTable, which needs no catalog at all.
    """
    metadata_dir = WAREHOUSE / NAMESPACE / table_name / "metadata"
    version = (metadata_dir / "version-hint.text").read_text().strip()
    return StaticTable.from_metadata((metadata_dir / f"v{version}.metadata.json").as_uri(), properties={"py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO"},)

def main() -> None:
    table = load("fact_funnel_event")

    unfiltered = len(list(table.scan().plan_files()))

    week = "event_ts >= '2025-06-02T00:00:00+00:00' AND event_ts < '2025-06-09T00:00:00+00:00'"
    filtered = len(list(table.scan(row_filter=week).plan_files()))

    month = "event_ts >= '2025-06-01T00:00:00+00:00' AND event_ts < '2025-07-01T00:00:00+00:00'"
    filtered_month = len(list(table.scan(row_filter=month).plan_files()))

    skipped = unfiltered - filtered
    pct = 100.0 * skipped / unfiltered if unfiltered else 0.0

    print("--- Iceberg file-level pruning ---")
    print(f"  no filter              : {unfiltered:5d} data files planned")
    print(f"  one month  ({month[:24]}...): {filtered_month:5d} data files planned")
    print(f"  one week   ({week[:24]}...): {filtered:5d} data files planned")
    print()
    print(f"  files skipped by the one-week filter: {skipped} of {unfiltered} ({pct:.1f}%)")
    print()
    print("  Those files were never opened. Iceberg eliminated them from the scan")
    print("  using per-file partition and column statistics stored in the manifests.")

    print("\n--- partition specs on this table ---")
    for spec in table.metadata.partition_specs:
        fields = [(f.name, str(f.transform)) for f in spec.fields]
        print(f"  spec {spec.spec_id}: {fields}")
    print("  Two specs coexist. Old files are still read under spec 0 (day);")
    print("  new files are written under spec 1 (month). No rewrite occurred.")


if __name__ == "__main__":
    main()