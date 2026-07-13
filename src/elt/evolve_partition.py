"""
Demonstrates Apache Iceberg partition evolution using Spark.
Connects to a local Iceberg Hadoop catalog and warehouse.
Updates the partition specification from daily to monthly partitions.
Loads a new batch of funnel event data from a CSV file using the shared
schema (schemas.py) instead of inferring it.
Casts event timestamps to the correct data type before ingestion.
Appends the new data using the evolved partition layout without rewriting existing files.
Illustrates Iceberg's metadata-only partition evolution capability.
Prints the status of the partition evolution and data append operations.
"""

import os
import sys
from pathlib import Path
from pyspark.sql import SparkSession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data_generation.schemas import FACT_SCHEMAS

WAREHOUSE = os.environ.get(
    "ICEBERG_WAREHOUSE",
    os.path.join(os.path.dirname(__file__), "..", "..", "warehouse")
)

spark = (SparkSession.builder
    .appName("iceberg-partition-evolution")
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.local.type", "hadoop")
    .config("spark.sql.catalog.local.warehouse", WAREHOUSE)
    .getOrCreate())

# Helper function to read the raw CSV files, using the shared schema
# (fact_funnel_event_new.csv shares FACT_FUNNEL_EVENT_SCHEMA with fact_funnel_event.csv)
def load_csv(name, schema):
    return spark.read.option("header", True).schema(schema).csv(f"data/raw/{name}.csv")

print("--- Executing Partition Evolution ---")

# Alter the table layout in metadata to evolve from days to months
# This operation is instant and doesn't touch old data files.
spark.sql("""
    ALTER TABLE local.db.fact_funnel_event
    REPLACE PARTITION FIELD days(event_ts) WITH months(event_ts)
""")
print("Successfully updated partition layout metadata from daily to monthly.")

# Load the new batch file, ensuring timestamps are cast properly
try:
    new_batch_raw = load_csv("fact_funnel_event_new", FACT_SCHEMAS["fact_funnel_event"])
    new_batch = new_batch_raw.withColumn("event_ts", new_batch_raw["event_ts"].cast("timestamp"))

    # Append data to the table without rewriting historical files
    new_batch.writeTo("local.db.fact_funnel_event").append()
    print("Successfully appended the new batch under the monthly partition scheme.")

except Exception as e:
    print(f"\nError running the script: {e}")
    print("Ensure 'data/raw/fact_funnel_event_new.csv' exists before appending.")