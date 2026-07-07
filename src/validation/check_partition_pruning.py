"""
Validates Iceberg partition pruning.
Compares Spark query plans for:
1. Full table scan
2. Date-filtered scan

The output is saved as evidence that Iceberg skips unnecessary files.
"""

from pyspark.sql import SparkSession

WAREHOUSE = "/customer-journey-funnel-tracker/warehouse"

spark = (
    SparkSession.builder
    .appName("partition-pruning-check")
    .config(
        "spark.jars.packages",
        "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0"
    )
    .config(
        "spark.sql.extensions",
        "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"
    )
    .config(
        "spark.sql.catalog.local",
        "org.apache.iceberg.spark.SparkCatalog"
    )
    .config(
        "spark.sql.catalog.local.type",
        "hadoop"
    )
    .config(
        "spark.sql.catalog.local.warehouse",
        WAREHOUSE
    )
    .getOrCreate()
)


df = spark.table("local.db.fact_funnel_event")

print("--- unfiltered ---")
df.explain(True)

print("--- filtered (one week) ---")
df.where(
    "event_ts >= '2025-06-01' AND event_ts < '2025-06-08'"
).explain(True)

spark.stop()