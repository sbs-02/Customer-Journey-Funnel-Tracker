import os
from pathlib import Path
from pyspark.sql import SparkSession
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

WAREHOUSE = os.environ.get(
    "ICEBERG_WAREHOUSE",
    os.path.join(os.path.dirname(__file__), "..", "..", "warehouse")
)
WAREHOUSE_URI = Path(WAREHOUSE).resolve().as_uri()

POSTGRES_URL = (
    f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
)

spark = (SparkSession.builder
    .appName("run-metrics")
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.11.0")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.local.type", "hadoop")
    .config("spark.sql.catalog.local.warehouse", WAREHOUSE_URI)
    .getOrCreate())

engine = create_engine(POSTGRES_URL)

TABLES = ["dim_date", "dim_customer", "dim_channel", "dim_product", "fact_funnel_event", "fact_orders"]

for name in TABLES:
    print(f"Loading {name} into Postgres...")
    df = spark.table(f"local.db.{name}").toPandas()
    df.to_sql(name, engine, if_exists="replace", index=False)

with engine.connect() as conn:
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_orders_date_key ON fact_orders (date_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_orders_customer_key ON fact_orders (customer_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_orders_channel_key ON fact_orders (channel_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_orders_product_key ON fact_orders (product_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_funnel_date_key ON fact_funnel_event (date_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_funnel_customer_key ON fact_funnel_event (customer_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fact_funnel_channel_key ON fact_funnel_event (channel_key)"))
    conn.commit()

spark.stop()

sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "metrics.sql")
queries = [q.strip() for q in open(sql_path).read().split(';') if q.strip()]

with engine.connect() as conn:
    for i, q in enumerate(queries, 1):
        print(f"\n--- Query {i} ---")
        result = conn.execute(text(q))
        rows = result.fetchall()
        for row in rows[:10]:
            print(row)