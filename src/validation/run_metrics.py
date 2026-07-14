import glob
import os
import re
from pathlib import Path
from pyiceberg.table import StaticTable
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

WAREHOUSE = os.environ.get(
    "ICEBERG_WAREHOUSE",
    os.path.join(os.path.dirname(__file__), "..", "..", "warehouse")
)

POSTGRES_URL = (
    f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
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

engine = create_engine(POSTGRES_URL)

for name in ["fact_orders", "dim_date", "fact_funnel_event"]:
    print(f"Loading {name} into Postgres...")
    arrow_tbl = load_table_arrow(name)
    arrow_tbl.to_pandas().to_sql(name, engine, if_exists="replace", index=False)

sql_path = os.path.join(os.path.dirname(__file__), "..", "..", "sql", "metrics.sql")
queries = [q.strip() for q in open(sql_path).read().split(';') if q.strip()]

with engine.connect() as conn:
    for i, q in enumerate(queries, 1):
        print(f"\n--- Query {i} ---")
        result = conn.execute(text(q))
        rows = result.fetchall()
        for row in rows[:10]:
            print(row)