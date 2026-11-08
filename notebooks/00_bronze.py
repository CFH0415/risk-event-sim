# Databricks notebook source
# MAGIC %md
# MAGIC # 00 Bronze — raw landing
# MAGIC
# MAGIC Loads the 8 raw CSVs from the `raw` volume into `bronze_*` tables, **exactly as received**, plus two lineage columns:
# MAGIC `_ingested_at` (when it was loaded) and `_source_file` (from which file).
# MAGIC
# MAGIC **Prerequisite:** the CSVs are uploaded to `/Volumes/workspace/risk_sim/raw/`. Safe to re-run (idempotent).

# COMMAND ----------

spark.conf.set("spark.sql.session.timeZone", "UTC")   # timestamps in the files are UTC (SPEC R8)

SCHEMA = "workspace.risk_sim"
RAW = "/Volumes/workspace/risk_sim/raw"
TABLES = ["customers", "accounts", "statements", "payments",
          "fees", "fee_adjustments", "transactions", "system_events"]

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {SCHEMA}.raw")

# COMMAND ----------

def load_bronze(name: str) -> int:
    """Load one CSV from the raw volume into a bronze table. Return the row count."""
    spark.sql(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.bronze_{name} AS
        SELECT *, current_timestamp() AS _ingested_at, _metadata.file_path AS _source_file
        FROM read_files('{RAW}/{name}.csv', format => 'csv', header => true, inferSchema => true)
    """)
    return spark.table(f"{SCHEMA}.bronze_{name}").count()

for t in TABLES:
    print(f"{t:<16} {load_bronze(t):>8,}")
