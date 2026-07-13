import duckdb
import glob
import os
import re
from pathlib import Path
from pyiceberg.table import StaticTable

WAREHOUSE = os.environ.get(
    "ICEBERG_WAREHOUSE",
    os.path.join(os.path.dirname(__file__), "..", "..", "warehouse")
)

def to_file_uri(path: str) -> str:
    uri = Path(path).resolve().as_uri()
    
    if re.match(r"^file:///[A-Za-z]:/", uri):
        uri = uri.replace("file:///", "file://", 1)
    return uri

def latest_metadata(table_name: str) -> str:
    files = glob.glob(os.path.join(WAREHOUSE, "db", table_name, "metadata", "*.metadata.json"))
    if not files:
        raise FileNotFoundError(f"No metadata files found for {table_name}")
    latest = max(files, key=os.path.getmtime)
    return to_file_uri(latest)

def load_table_arrow(table_name: str):
    return StaticTable.from_metadata(latest_metadata(table_name)).scan().to_arrow()

fact_orders = load_table_arrow("fact_orders")
dim_date = load_table_arrow("dim_date")
fact_funnel_event = load_table_arrow("fact_funnel_event")

con = duckdb.connect()
con.register('fact_orders', fact_orders)
con.register('dim_date', dim_date)
con.register('fact_funnel_event', fact_funnel_event)

sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "metrics.sql")
queries = [q.strip() for q in open(sql_path).read().split(';') if q.strip()]

for i, q in enumerate(queries, 1):
    print(f"\n--- Query {i} ---")
    print(con.execute(q).fetchdf().head(10))