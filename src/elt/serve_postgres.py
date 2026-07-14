"""
Serve Iceberg data in Postgres for Power BI compatibility.
Why two stores. Iceberg is the source of truth: versioned, snapshotted, and the
only thing that can answer "as of which snapshot?".
Postgres is a disposable serving copy that exists because Power BI's Postgres connector is reliable and
its Iceberg story is not. If Postgres drifts, drop it and re-run this script.

  Iceberg (truth, snapshots) --> Postgres (serving copy) --> Power BI
        
          --> the MCP agent reads Iceberg DIRECTLY, so it can cite a snapshot
              id on every number. See src/agent/lakehouse.py.
"""

import os
import sys
import logging
from pathlib import Path

from pyspark.sql import SparkSession
from sqlalchemy import create_engine, text, types
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
WAREHOUSE_URI = Path(
    os.environ.get("ICEBERG_WAREHOUSE", ROOT / "warehouse")
).resolve().as_uri()

POSTGRES_URL = (
    f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("serve_postgres")

TABLES = ["dim_date", "dim_customer", "dim_channel", "dim_product",
          "fact_funnel_event", "fact_orders"]

# Views must be dropped before their base tables can be replaced, then recreated.
# Ordered dependents-first. Without this, Postgres refuses the replace outright:
#   "cannot drop table dim_date because other objects depend on it".
VIEWS = ["vw_orders_trend", "vw_funnel_stage_trend", "vw_funnel_weekly",
         "vw_weekly_orders", "vw_weekly_funnel", "vw_week"]

# pandas.to_sql infers TEXT for anything it is unsure about, and a date column
# landing as TEXT is not cosmetic: Power BI will not build a date hierarchy on
# it, and MAX(date) - MIN(date) fails with "operator does not exist: text - text".
COLUMN_TYPES = {
    "dim_date":          {"date": types.Date(), "week_start_date": types.Date()},
    "dim_customer":      {"signup_date": types.Date()},
    "fact_funnel_event": {"event_ts": types.TIMESTAMP()},
    "fact_orders":       {"order_ts": types.TIMESTAMP()},
}

INDEXES = {
    "fact_orders":       ["date_key", "customer_key", "channel_key", "product_key"],
    "fact_funnel_event": ["date_key", "customer_key", "channel_key"],
}


def main() -> None:
    spark = (SparkSession.builder
        .appName("serve-postgres")
        .config("spark.jars.packages",
                "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0")
        .config("spark.sql.extensions",
                "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.local.type", "hadoop")
        .config("spark.sql.catalog.local.warehouse", WAREHOUSE_URI)
        .getOrCreate())

    engine = create_engine(POSTGRES_URL)

    try:
        with engine.begin() as conn:
            for view in VIEWS:
                conn.execute(text(f"DROP VIEW IF EXISTS {view} CASCADE"))
        log.info("dropped %d dependent views", len(VIEWS))

        for table in TABLES:
            # toPandas() collects onto the driver. Fine here -- the largest fact
            # is a few hundred thousand rows -- and it avoids shipping a Postgres
            # JDBC jar into Spark just to write. If the facts ever reach millions
            # of rows, switch to a JDBC write or COPY.
            df = spark.table(f"local.db.{table}").toPandas()
            df.to_sql(table, engine, if_exists="replace", index=False,
                      dtype=COLUMN_TYPES.get(table), chunksize=10_000, method="multi")
            log.info("served %-18s %8d rows", table, len(df))

        with engine.begin() as conn:
            for table, columns in INDEXES.items():
                for column in columns:
                    conn.execute(text(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_{column} "
                        f"ON {table} ({column})"))
            log.info("created foreign-key indexes")

            conn.execute(text((ROOT / "sql" / "views.sql").read_text()))
            log.info("applied sql/views.sql")

        with engine.connect() as conn:
            weeks = conn.execute(text(
                "SELECT COUNT(*) FROM vw_week WHERE is_complete_week")).scalar_one()
        log.info("serving layer ready -- %d complete ISO weeks", weeks)

    finally:
        spark.stop()


if __name__ == "__main__":
    main()