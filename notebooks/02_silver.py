# Databricks notebook source
# MAGIC %md
# MAGIC # 02 Silver — cleaned, typed, conformed
# MAGIC
# MAGIC **Input:** `bronze_*` (as received) → **Output:** `silver_*` (safe to analyze)
# MAGIC
# MAGIC | Issue found in profiling | Fix applied here |
# MAGIC |---|---|
# MAGIC | Money stored as `double` (floating-point error, e.g. `90.94000000000001`) | Cast to `DECIMAL(12,2)` |
# MAGIC | 83 duplicate rows in `payments` (OQ1) | Confirm they are **exact** copies, then drop them |
# MAGIC | Timestamps are UTC, but business rule R1 uses Eastern Time (OQ2) | Add `*_et` columns next to the UTC ones (UTC kept as-is) |
# MAGIC | `_rescued_data` (unparseable values) | Confirm all null, then drop |
# MAGIC
# MAGIC **Not done here (on purpose):** any business logic — who is late, who was harmed, what to refund. That belongs in Gold.
# MAGIC Silver only makes the data *trustworthy*; it does not *interpret* it.
# MAGIC
# MAGIC Safe to re-run (idempotent): every table is overwritten.

# COMMAND ----------

from pyspark.sql import functions as F

spark.conf.set("spark.sql.session.timeZone", "UTC")

SCHEMA = "workspace.risk_sim"
MONEY = "decimal(12,2)"
ET = "America/New_York"   # handles daylight saving time automatically

def bronze(name):
    return spark.table(f"{SCHEMA}.bronze_{name}")

def check_rescued(df, name):
    """Fail loudly if any value could not be parsed when loading bronze."""
    n = df.filter(F.col("_rescued_data").isNotNull()).count()
    assert n == 0, f"{name}: {n} rows have _rescued_data — inspect bronze before continuing"
    return df.drop("_rescued_data")

def write_silver(df, name, comment):
    full = f"{SCHEMA}.silver_{name}"
    (df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(full))
    spark.sql(f"COMMENT ON TABLE {full} IS '{comment}'")
    print(f"silver_{name:<16} {spark.table(full).count():>8,} rows")

def money(*cols):
    return {c: F.col(c).cast(MONEY) for c in cols}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Evidence for OQ1 — are the duplicate payments exact copies?
# MAGIC If every column matches, the copies carry no new information and can be safely dropped.
# MAGIC If the values differed, we could **not** just drop them; we would need a rule for which version is correct.

# COMMAND ----------

p = bronze("payments")
biz_cols = [c for c in p.columns if not c.startswith("_")]

display(spark.createDataFrame([(
    p.count(),
    p.select("payment_id").distinct().count(),
    p.select(*biz_cols).distinct().count(),
)], "total_rows long, distinct_payment_ids long, distinct_full_rows long"))
# If distinct_payment_ids == distinct_full_rows, every duplicate is an exact copy.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build the silver tables

# COMMAND ----------

# customers
df = check_rescued(bronze("customers"), "customers")
df = df.withColumn("customer_since", F.col("customer_since").cast("date"))
write_silver(df, "customers", "One row per customer.")

# accounts
df = check_rescued(bronze("accounts"), "accounts")
df = (df.withColumns(money("credit_limit"))
        .withColumns({
            "purchase_apr": F.col("purchase_apr").cast("decimal(6,4)"),
            "statement_cycle_day": F.col("statement_cycle_day").cast("int"),
            "autopay_enrolled": F.col("autopay_enrolled").cast("boolean"),
            "open_date": F.col("open_date").cast("date"),
            "closed_date": F.col("closed_date").cast("date"),
        }))
write_silver(df, "accounts", "One row per card account. purchase_apr is a fraction (0.2449 = 24.49%).")

# statements
df = check_rescued(bronze("statements"), "statements")
df = (df.withColumns(money("previous_balance", "payments", "credits_adjustments", "purchases",
                           "fees_charged", "interest_charged", "avg_daily_balance",
                           "new_balance", "minimum_due"))
        .withColumns({c: F.col(c).cast("date") for c in ["cycle_start_date", "statement_date", "due_date"]})
        .withColumn("days_in_cycle", F.col("days_in_cycle").cast("int")))
write_silver(df, "statements", "One row per account per billing cycle. Summary of activity posted in the cycle.")

# payments — drop exact duplicate rows, add Eastern Time columns
df = check_rescued(bronze("payments"), "payments")
df = (df.dropDuplicates(biz_cols)
        .withColumns(money("amount"))
        .withColumn("received_ts_utc", F.col("received_ts_utc").cast("timestamp"))
        .withColumn("received_ts_et", F.from_utc_timestamp("received_ts_utc", ET))
        .withColumn("received_date_et", F.to_date("received_ts_et"))
        .withColumn("posted_date", F.col("posted_date").cast("date")))
write_silver(df, "payments",
             "One row per payment received (exact duplicates removed). received_date_et is the date that counts for on-time rule R1.")

# fees
df = check_rescued(bronze("fees"), "fees")
df = (df.withColumns(money("fee_amount"))
        .withColumn("assessed_date", F.col("assessed_date").cast("date")))
write_silver(df, "fees", "One row per late fee assessed.")

# fee_adjustments
df = check_rescued(bronze("fee_adjustments"), "fee_adjustments")
df = (df.withColumns(money("amount"))
        .withColumn("adjustment_date", F.col("adjustment_date").cast("date")))
write_silver(df, "fee_adjustments", "One row per fee reversal already made by an agent.")

# transactions
df = check_rescued(bronze("transactions"), "transactions")
df = (df.withColumns(money("amount"))
        .withColumn("post_date", F.col("post_date").cast("date")))
write_silver(df, "transactions",
             "One row per item posted to an account (PURCHASE, PAYMENT, FEE, INTEREST). reference_id: P=payments, F=fees, S=statements.")

# system_events — add Eastern Time columns
df = check_rescued(bronze("system_events"), "system_events")
df = (df.withColumn("start_ts_utc", F.col("start_ts_utc").cast("timestamp"))
        .withColumn("end_ts_utc", F.col("end_ts_utc").cast("timestamp"))
        .withColumn("start_ts_et", F.from_utc_timestamp("start_ts_utc", ET))
        .withColumn("end_ts_et", F.from_utc_timestamp("end_ts_utc", ET)))
write_silver(df, "system_events", "One row per system incident. *_et columns are Eastern Time.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation — bronze vs. silver
# MAGIC Expected: every table keeps the same row count **except payments (−83)**; all primary keys unique;
# MAGIC money totals change by at most a few cents (rounding to 2 decimals) — **except payments**, whose total drops by
# MAGIC the amount of the 83 duplicates (about $44K). That drop is the double-counting you would have had if you had joined on bronze.

# COMMAND ----------

checks = [("customers", "customer_id", None), ("accounts", "account_id", "credit_limit"),
          ("statements", "statement_id", "new_balance"), ("payments", "payment_id", "amount"),
          ("fees", "fee_id", "fee_amount"), ("fee_adjustments", "adjustment_id", "amount"),
          ("transactions", None, "amount"), ("system_events", "event_id", None)]
rows = []
for t, pk, amt in checks:
    b, s = bronze(t), spark.table(f"{SCHEMA}.silver_{t}")
    s_n = s.count()
    unique = (s.select(pk).distinct().count() == s_n) if pk else None
    b_sum = float(b.agg(F.sum(amt)).first()[0]) if amt else None
    s_sum = float(s.agg(F.sum(amt)).first()[0]) if amt else None
    rows.append((t, b.count(), s_n, unique, b_sum, s_sum))
display(spark.createDataFrame(rows, "table string, bronze_rows long, silver_rows long, pk_unique boolean, bronze_sum double, silver_sum double"))

# COMMAND ----------

# Statement balance identity must still hold after rounding to DECIMAL
bad = spark.sql(f"""
    SELECT count(*) FROM {SCHEMA}.silver_statements
    WHERE abs(previous_balance - payments - credits_adjustments + purchases
              + fees_charged + interest_charged - new_balance) > 0.01
""").first()[0]
assert bad == 0, f"{bad} statements break the balance identity"
print("✅ Balance identity holds for all statements")
