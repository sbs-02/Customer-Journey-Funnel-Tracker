"""
Demonstrates Apache Iceberg partition evolution using Spark.
Connects to a local Iceberg Hadoop catalog and warehouse.
Updates the partition specification from daily to monthly partitions.
Loads a new batch of funnel event data from a CSV file.
Casts event timestamps to the correct data type before ingestion.
Appends the new data using the evolved partition layout without rewriting existing files.
Illustrates Iceberg's metadata-only partition evolution capability.
Prints the status of the partition evolution and data append operations.
"""

import os
from pyspark.sql import SparkSession

WAREHOUSE = "/customer-journey-funnel-tracker/warehouse" 

spark = (SparkSession.builder
    .appName("iceberg-partition-evolution")
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.local.type", "hadoop")
    .config("spark.sql.catalog.local.warehouse", WAREHOUSE)
    .getOrCreate())

# Helper function to read the raw CSV files
def load_csv(name):
    return spark.read.option("header", True).option("inferSchema", True).csv(f"data/raw/{name}.csv")

print("--- Step 10: Executing Partition Evolution ---")

# Alter the table layout in metadata to evolve from days to months
# This operation is instant and doesn't touch old data files.
spark.sql("""
    ALTER TABLE local.db.fact_funnel_event
    REPLACE PARTITION FIELD days(event_ts) WITH months(event_ts)
""")
print("Successfully updated partition layout metadata from daily to monthly.")

# Load the new batch file, ensuring timestamps are cast properly (just like Step 8)
try:
    new_batch_raw = load_csv("fact_funnel_event_new")
    new_batch = new_batch_raw.withColumn("event_ts", new_batch_raw["event_ts"].cast("timestamp"))

    # Append data to the table without rewriting historical files
    new_batch.writeTo("local.db.fact_funnel_event").append()
    print("Successfully appended the new batch under the monthly partition scheme.")

except Exception as e:
    print(f"\nError running the script: {e}")
    print("Ensure 'data/raw/fact_funnel_event_new.csv' exists before appending.")