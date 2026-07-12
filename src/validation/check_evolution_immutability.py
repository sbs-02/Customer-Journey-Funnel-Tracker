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

print("--- snapshot history (added/deleted file counts per snapshot) ---")
spark.sql("""
    SELECT snapshot_id, committed_at, operation,
           summary['added-data-files']   AS added_files,
           summary['deleted-data-files'] AS deleted_files,
           summary['total-data-files']   AS total_files
    FROM local.db.fact_funnel_event.snapshots
    ORDER BY committed_at
""").show(200, truncate=False)