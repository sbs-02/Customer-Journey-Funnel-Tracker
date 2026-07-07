from pyspark.sql import SparkSession
from pyspark.sql.functions import days
from pyspark.sql.functions import col, month, year
from pyspark.sql.functions.partitioning import days

WAREHOUSE = "/customer-journey-funnel-tracker/warehouse"   # where Iceberg's files live on disk

spark = (SparkSession.builder
    .appName("iceberg-load")
    .config("spark.jars.packages",
            "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.local.type", "hadoop")   # simplest catalog type, no extra server needed
    .config("spark.sql.catalog.local.warehouse", WAREHOUSE)
    .getOrCreate())

spark.sql("CREATE NAMESPACE IF NOT EXISTS local.db")

def load_csv(name):
    return spark.read.option("header", True).option("inferSchema", True).csv(f"data/raw/{name}.csv")

for dim in ["dim_date", "dim_customer", "dim_channel", "dim_product"]:
    load_csv(dim).writeTo(f"local.db.{dim}").createOrReplace()

def load_fact_by_month(name, ts_col):
    df = load_csv(name)
    df = df.withColumn(ts_col, col(ts_col).cast("timestamp"))
    
    years_months = df.select(year(ts_col).alias("y"), month(ts_col).alias("m")) \
                     .distinct().orderBy("y", "m").collect()
    
    first = True
    for row in years_months:
        chunk = df.filter(
            (year(col(ts_col)) == row["y"]) & (month(col(ts_col)) == row["m"])
        )
        if first:
            chunk.writeTo(f"local.db.{name}").partitionedBy(days(ts_col)).createOrReplace()
            first = False
        else:
            chunk.writeTo(f"local.db.{name}").append()

load_fact_by_month("fact_funnel_event", "event_ts")
load_fact_by_month("fact_orders", "order_ts")

print(spark.table("local.db.fact_funnel_event").count(), "funnel events loaded")
print(spark.table("local.db.fact_orders").count(), "orders loaded")