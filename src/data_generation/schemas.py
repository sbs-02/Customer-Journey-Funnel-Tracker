"""
Single source of truth for the star schema's column names and types.

Imported by:
- load_iceberg.py   (the Spark loader — uses these instead of inferSchema)
- generate_data.py / generate_new_batch.py (optional: validate output columns
  match these definitions before writing CSVs, to prevent drift)
"""

from pyspark.sql.types import (
    StructType, StructField, IntegerType, LongType,
    StringType, DoubleType, DateType
)

DIM_CHANNEL_SCHEMA = StructType([
    StructField("channel_key",   IntegerType(), False),
    StructField("channel_name",  StringType(),  False),
    StructField("channel_group", StringType(),  False),
])

DIM_CUSTOMER_SCHEMA = StructType([
    StructField("customer_key", IntegerType(), False),
    StructField("customer_id",  StringType(),  False),  # UUID, natural key
    StructField("name",         StringType(),  False),
    StructField("segment",      StringType(),  False),  # Enterprise / Mid-Market / SMB
    StructField("region",       StringType(),  False),
    StructField("signup_date",  DateType(),    False),
])

DIM_DATE_SCHEMA = StructType([
    StructField("date_key",     IntegerType(), False),
    StructField("date",         DateType(),    False),
    StructField("year",         IntegerType(), False),
    StructField("month",        IntegerType(), False),
    StructField("iso_week",     IntegerType(), False),
    StructField("day_of_week",  StringType(),  False),
])

DIM_PRODUCT_SCHEMA = StructType([
    StructField("product_key",  IntegerType(), False),
    StructField("product_name", StringType(),  False),
    StructField("category",     StringType(),  False),
    StructField("unit_price",   DoubleType(),  False),
])

# shared by fact_funnel_event.csv and fact_funnel_event_new.csv
FACT_FUNNEL_EVENT_SCHEMA = StructType([
    StructField("event_key",    LongType(),    False),  # new batch already starts at 500000
    StructField("date_key",     IntegerType(), False),
    StructField("customer_key", IntegerType(), False),
    StructField("channel_key",  IntegerType(), False),
    StructField("stage",        StringType(),  False),  # visit / lead / opportunity
    StructField("event_ts",     StringType(),  False),  # cast after read: two batches use different formats
])

FACT_ORDERS_SCHEMA = StructType([
    StructField("order_line_key", LongType(),    False),
    StructField("date_key",       IntegerType(), False),
    StructField("customer_key",   IntegerType(), False),
    StructField("channel_key",    IntegerType(), False),
    StructField("product_key",    IntegerType(), False),
    StructField("revenue",        DoubleType(),  False),
    StructField("quantity",       IntegerType(), False),
    StructField("order_ts",       StringType(),  False),
])

DIM_SCHEMAS = {
    "dim_channel":  DIM_CHANNEL_SCHEMA,
    "dim_customer": DIM_CUSTOMER_SCHEMA,
    "dim_date":     DIM_DATE_SCHEMA,
    "dim_product":  DIM_PRODUCT_SCHEMA,
}

FACT_SCHEMAS = {
    "fact_funnel_event": FACT_FUNNEL_EVENT_SCHEMA,
    "fact_orders":       FACT_ORDERS_SCHEMA,
}