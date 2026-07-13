from pyspark.sql import SparkSession
import os

WAREHOUSE = os.environ.get("ICEBERG_WAREHOUSE", "warehouse")

spark = (SparkSession.builder
    .appName("evolution-immutability-check")
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.local.type", "hadoop")
    .config("spark.sql.catalog.local.warehouse", WAREHOUSE)
    .getOrCreate())

# 1. Inspect Iceberg Metadata Table to verify no files were deleted during evolution
print("--- snapshot history (added/deleted file counts per snapshot) ---")
spark.sql("""
    SELECT snapshot_id, committed_at, operation,
           summary['added-data-files']   AS added_files,
           summary['deleted-data-files'] AS deleted_files,
           summary['total-data-files']   AS total_files
    FROM local.db.fact_funnel_event.snapshots
    ORDER BY committed_at
""").show(200, truncate=False)

# 2. Query Plan for pre-evolution daily partition range
print("--- explain(): scan restricted to the pre-evolution (daily-partitioned) date range ---")
spark.table("local.db.fact_funnel_event") \
    .where("event_ts < '2025-12-01'") \
    .explain(True)

# 3. Query Plan across both historical daily and new monthly partition specs
print("--- explain(): scan spanning old (day) + new (month) partition layouts together ---")
spark.table("local.db.fact_funnel_event") \
    .where("event_ts >= '2022-01-01'") \
    .explain(True)

spark.stop()