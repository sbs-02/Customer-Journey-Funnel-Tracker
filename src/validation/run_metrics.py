"""
Execute every query in sql/metrics.sql against the Postgres serving layer and
report pass/fail per statement.

Run:  uv run python src/elt/serve_postgres.py      # publish first
      uv run python src/validation/run_metrics.py  # then validate
"""

import os
import re
import sys
import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_URL = (
    f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
log = logging.getLogger("run_metrics")


def split_statements(sql: str) -> list[str]:
    """Split a .sql file into executable statements.

    A naive sql.split(';') breaks on any semicolon inside a comment -- and a
    comment explaining ISO-week semantics is exactly the sort of prose that
    contains one. Strip line comments first, so the file stays freely commentable.
    """
    return [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]


def main() -> int:
    engine = create_engine(POSTGRES_URL)
    statements = split_statements((ROOT / "sql" / "metrics.sql").read_text())
    log.info("sql/metrics.sql -> %d statements", len(statements))

    failures = 0
    for i, stmt in enumerate(statements, 1):
        # Each statement gets its own transaction, so one failure does not
        # cascade into "current transaction is aborted" for all the others.
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(stmt)).fetchall()
            log.info("[%2d] OK   %6d rows", i, len(rows))
        except Exception as exc:
            failures += 1
            log.error("[%2d] FAIL  %s", i, str(exc).splitlines()[0])

    log.info("%d/%d queries passed", len(statements) - failures, len(statements))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())