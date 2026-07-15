"""
Read-only access to the Iceberg lakehouse for the agent.

WHY NOT load_catalog(). The obvious code is:

    catalog = load_catalog("local", type="hadoop", warehouse=...)

It does not work. PyIceberg 0.8 registers exactly five catalog types -- rest,
hive, glue, dynamodb, sql -- and 'hadoop' is not among them; the call dies with
KeyError: 'HADOOP'. Our Spark writer uses a Hadoop catalog, which is just a
directory tree with no metastore behind it, so there is no catalog server for
PyIceberg to talk to.

WHAT WORKS INSTEAD. A Hadoop catalog records its current metadata pointer in a
file called version-hint.text next to the table. Read that, resolve the matching
vN.metadata.json, and hand it to StaticTable -- a read-only Table that needs no
catalog at all. That is exactly the access mode the agent wants: it must never
write.

The alternative -- booting a Spark session per chat turn -- would add ~10s of JVM
startup to every question. PyIceberg + DuckDB answers in milliseconds.
"""

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.table import StaticTable
from dotenv import load_dotenv

load_dotenv()

if os.name == "nt":
    import re
    from urllib.parse import urlparse
    import pyiceberg.io as _pyi_io
    import pyiceberg.io.pyarrow as _pyi_pyarrow

    _DRIVE_LETTER = re.compile(r"^/[A-Za-z]:")

    def _windows_safe_parse_location(location: str, properties=None):
        uri = urlparse(location)
        if not uri.scheme:
            return "file", uri.netloc, os.path.abspath(location)
        if uri.scheme in ("hdfs", "viewfs"):
            return uri.scheme, uri.netloc, uri.path
        path = f"{uri.netloc}{uri.path}"
        if uri.scheme == "file" and _DRIVE_LETTER.match(path):
            path = path[1:]
        return uri.scheme, uri.netloc, path

    _pyi_io._parse_location = _windows_safe_parse_location
    _pyi_pyarrow._parse_location = _windows_safe_parse_location
    _pyi_pyarrow.PyArrowFileIO.parse_location = staticmethod(_windows_safe_parse_location)

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE = Path(os.environ.get("ICEBERG_WAREHOUSE", ROOT / "warehouse")).resolve()
NAMESPACE = os.environ.get("ICEBERG_NAMESPACE", "db")


class LakehouseError(RuntimeError):
    """Warehouse missing or unreadable, with the fix attached."""


@dataclass(frozen=True)
class Snapshot:
    """The Iceberg snapshot an answer was computed from.

    This is what makes a number defensible. "412 leads" is a claim. "412 leads as
    of snapshot 8568601916772387175, committed 2026-07-13T10:24:03Z" is a fact
    someone else can independently re-derive.
    """
    snapshot_id: str
    committed_at: dt.datetime
    operation: str
    total_records: int | None

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_committed_at": self.committed_at.isoformat(),
            "operation": self.operation,
            "total_records": self.total_records,
        }


@dataclass
class ScanStats:
    """File-level accounting -- the evidence that pruning skipped work."""
    files_total: int = 0
    files_scanned: int = 0

    @property
    def files_skipped(self) -> int:
        return self.files_total - self.files_scanned

    def as_dict(self) -> dict:
        return {"files_total": self.files_total,
                "files_scanned": self.files_scanned,
                "files_skipped": self.files_skipped}


def _metadata_file(table_name: str) -> Path:
    """Resolve a Hadoop-catalog table's current metadata JSON."""
    metadata_dir = WAREHOUSE / NAMESPACE / table_name / "metadata"
    if not metadata_dir.is_dir():
        raise LakehouseError(
            f"No Iceberg metadata at {metadata_dir}. "
            "Build the warehouse first: uv run python src/elt/load_iceberg.py")

    hint = metadata_dir / "version-hint.text"
    if hint.exists():
        candidate = metadata_dir / f"v{hint.read_text().strip()}.metadata.json"
        if candidate.exists():
            return candidate

    # Fallback for an interrupted write that left no hint file.
    versions = sorted(metadata_dir.glob("v*.metadata.json"),
                      key=lambda p: int(p.stem.lstrip("v").split(".")[0]))
    if not versions:
        raise LakehouseError(f"No vN.metadata.json under {metadata_dir}")
    return versions[-1]


class Lakehouse:
    """Read-only handle on the Iceberg tables.

    Arrow tables are cached per (table, snapshot). The star schema is small
    enough to sit in memory, and a chat session asks many questions against the
    same snapshot -- re-reading Parquet every turn would be wasted work.
    """

    def __init__(self) -> None:
        self._tables: dict[str, StaticTable] = {}
        self._arrow: dict[tuple[str, int | None], pa.Table] = {}

    def table(self, name: str) -> StaticTable:
        if name not in self._tables:
            self._tables[name] = StaticTable.from_metadata(_metadata_file(name).resolve().as_uri())
        return self._tables[name]

    def _wrap(self, snap) -> Snapshot:
        total = snap.summary.additional_properties.get("total-records") if snap.summary else None
        return Snapshot(
            snapshot_id=str(snap.snapshot_id),
            committed_at=dt.datetime.fromtimestamp(snap.timestamp_ms / 1000, dt.timezone.utc),
            operation=str(snap.summary.operation.value) if snap.summary else "unknown",
            total_records=int(total) if total is not None else None,
        )

    def snapshot(self, name: str, snapshot_id: int | None = None) -> Snapshot:
        table = self.table(name)
        if snapshot_id is None:
            snap = table.current_snapshot()
            if snap is None:
                raise LakehouseError(f"Table {name} has no snapshots -- it is empty.")
        else:
            snap = next((s for s in table.metadata.snapshots
                         if s.snapshot_id == snapshot_id), None)
            if snap is None:
                available = [s.snapshot_id for s in table.metadata.snapshots]
                raise LakehouseError(
                    f"Snapshot {snapshot_id} not found on {name}. Available: {available}")
        return self._wrap(snap)

    def history(self, name: str) -> list[Snapshot]:
        """Every snapshot, oldest first -- the menu for 'as of date X' questions."""
        return sorted((self._wrap(s) for s in self.table(name).metadata.snapshots),
                      key=lambda s: s.committed_at)

    def snapshot_as_of(self, name: str, as_of: dt.date) -> Snapshot:
        """Latest snapshot committed on or before `as_of` -- Iceberg time travel.

        Answers "what did the data look like on the 3rd?" with the state the table
        was ACTUALLY in then, rather than filtering today's data by date -- which
        would silently include rows backfilled since.
        """
        cutoff = dt.datetime.combine(as_of, dt.time.max, tzinfo=dt.timezone.utc)
        eligible = [s for s in self.history(name) if s.committed_at <= cutoff]
        if not eligible:
            earliest = self.history(name)[0].committed_at.date()
            raise LakehouseError(
                f"No snapshot of {name} existed on {as_of}. "
                f"The table's first snapshot is {earliest}.")
        return eligible[-1]

    def arrow(self, name: str, snapshot_id: int | None = None) -> pa.Table:
        key = (name, snapshot_id)
        if key not in self._arrow:
            table = self.table(name)
            scan = table.scan(snapshot_id=snapshot_id) if snapshot_id else table.scan()
            self._arrow[key] = scan.to_arrow()
        return self._arrow[key]

    def scan_stats(self, name: str, row_filter: str = "") -> ScanStats:
        """Count data files with and without a predicate -- the real pruning proof."""
        table = self.table(name)
        total = len(list(table.scan().plan_files()))
        scanned = (len(list(table.scan(row_filter=row_filter).plan_files()))
                   if row_filter else total)
        return ScanStats(files_total=total, files_scanned=scanned)

    def query(self, sql: str, tables: dict[str, pa.Table], params: list | None = None):
        """Run DuckDB SQL over Arrow tables read from Iceberg (zero-copy)."""
        con = duckdb.connect()
        try:
            for alias, arrow_table in tables.items():
                con.register(alias, arrow_table)
            return con.execute(sql, params or []).fetchdf()
        finally:
            con.close()


# One process-wide handle. The Arrow cache is the point -- rebuilding it per
# request would re-read Parquet on every chat turn.
lakehouse = Lakehouse()