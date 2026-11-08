-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Scenario 1 — "Who zeroed out August?"
-- MAGIC
-- MAGIC **Monday, 9:05 AM.** The Risk Manager messages you on Slack:
-- MAGIC
-- MAGIC > *"The late fee report I pulled this morning shows **$0 for all of August**. Last week August was over $44K.
-- MAGIC > Did something happen to the fees table? I need to know what happened, and I need it fixed before the 11 AM review."*
-- MAGIC
-- MAGIC You'll work on a practice copy: `workspace.risk_sim.scn01_fees`.
-- MAGIC
-- MAGIC **Your job:** (1) confirm the problem, (2) find out exactly what changed, when, and how, (3) decide how to fix it **safely**, (4) prove it's fixed.
-- MAGIC
-- MAGIC **How to work:** the queries are written for you. Before each one, answer the 🔮 question. After running, compare.
-- MAGIC
-- MAGIC **Prerequisite:** `02_silver` has been run.

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 0. Setup — run this cell, don't study it
-- MAGIC It recreates "last week's" table and then replays what happened over the weekend. (Peeking spoils the exercise.)

-- COMMAND ----------

DROP TABLE IF EXISTS workspace.risk_sim.scn01_fees;

CREATE TABLE workspace.risk_sim.scn01_fees AS
SELECT fee_id, account_id, statement_id, fee_type, fee_amount, assessed_date
FROM workspace.risk_sim.silver_fees;

-- Friday 6:40 PM — a colleague's "cleanup" script
UPDATE workspace.risk_sim.scn01_fees
SET fee_amount = 0
WHERE assessed_date >= '2026-08-01';

-- Saturday 10:15 AM — Operations, ticket OPS-2291: three July fees confirmed as charged in error
UPDATE workspace.risk_sim.scn01_fees
SET fee_amount = 0
WHERE fee_id IN ('F0001188', 'F0001236', 'F0001315');

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 1. Confirm the problem
-- MAGIC 🔮 The Risk Manager said August is $0. Before running: what do you expect for **May–July**? For **September**?

-- COMMAND ----------

SELECT date_trunc('month', assessed_date) AS month,
       count(*)                           AS fees,
       sum(fee_amount)                    AS total_dollars
FROM workspace.risk_sim.scn01_fees
GROUP BY 1
ORDER BY 1;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ✍️ **What did you find?** Is it only August? (One sentence.)

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 2. Investigate — what changed, when, and how?
-- MAGIC Every Delta table keeps a **history**: every write becomes a new **version**, with who did it, when, and the exact command.
-- MAGIC It's like "version history" in Google Docs — and in a regulated bank, it's your audit trail.
-- MAGIC
-- MAGIC 🔮 How many versions do you expect to see?

-- COMMAND ----------

DESCRIBE HISTORY workspace.risk_sim.scn01_fees;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC The useful columns:
-- MAGIC
-- MAGIC | Column | Tells you |
-- MAGIC |---|---|
-- MAGIC | `version` | 0 = created; each change adds 1 |
-- MAGIC | `timestamp` | When |
-- MAGIC | `userName` | Who (here it's always you, because the setup cell ran everything) |
-- MAGIC | `operation` | What kind of change: CREATE, UPDATE, DELETE, MERGE… |
-- MAGIC | `operationParameters` | **The exact condition used** — look for `predicate` |
-- MAGIC | `operationMetrics` | How many rows changed (`numUpdatedRows`) |
-- MAGIC
-- MAGIC ✍️ Answer:
-- MAGIC 1. Which version broke August? What was its `predicate`?
-- MAGIC 2. Read that predicate closely. Did it touch **only** August? (Compare with step 1.)
-- MAGIC 3. Is there a change **after** the bad one? What did it do — and was it legitimate?

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 3. Look into the past (time travel)
-- MAGIC You can query the table **as it was** at any version. This is how you answer "what did the data look like before the incident?"
-- MAGIC
-- MAGIC 🔮 Version 0 is the original. What should August's total be there?

-- COMMAND ----------

SELECT 'v0 (original)' AS version, date_trunc('month', assessed_date) AS month, sum(fee_amount) AS total_dollars
FROM workspace.risk_sim.scn01_fees VERSION AS OF 0
GROUP BY 2
UNION ALL
SELECT 'current', date_trunc('month', assessed_date), sum(fee_amount)
FROM workspace.risk_sim.scn01_fees
GROUP BY 2
ORDER BY month, version;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 4. Decide how to fix it ⚖️ — the real decision
-- MAGIC
-- MAGIC | | **Option A: RESTORE** | **Option B: Targeted fix** |
-- MAGIC |---|---|---|
-- MAGIC | Command | `RESTORE TABLE ... TO VERSION AS OF 0` | `MERGE` only the rows the bad script damaged, using version 0 as the source |
-- MAGIC | Analogy | Roll the whole document back to Friday | Undo only the bad paragraph |
-- MAGIC | Speed | One line | A few lines |
-- MAGIC | Risk | ? | ? |
-- MAGIC
-- MAGIC Run the query below **before** deciding. It lists every row where version 0 and today differ, grouped by which change caused it.
-- MAGIC
-- MAGIC 🔮 If you chose Option A, what would happen to the `OPS-2291` rows? What would that mean for those 3 customers?

-- COMMAND ----------

SELECT CASE WHEN cur.fee_id IN ('F0001188', 'F0001236', 'F0001315') THEN 'OPS-2291 (Saturday, legitimate)'
            ELSE 'Cleanup script (Friday)' END       AS caused_by,
       count(*)                                     AS rows_changed,
       sum(v0.fee_amount)                           AS dollars_in_v0,
       sum(cur.fee_amount)                          AS dollars_now
FROM workspace.risk_sim.scn01_fees cur
JOIN workspace.risk_sim.scn01_fees VERSION AS OF 0 v0 USING (fee_id)
WHERE cur.fee_amount <> v0.fee_amount
GROUP BY 1;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ✍️ **Your decision:** A or B? One sentence on why.
-- MAGIC
-- MAGIC (Decide before scrolling. The next section is Option B.)

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 5. Fix — Option B (targeted)
-- MAGIC Put back the **original amount** for every fee the Friday script zeroed — and **leave Ops' 3 waivers alone**.
-- MAGIC
-- MAGIC `MERGE` = "match rows between two tables, and update only the ones that meet a condition." You'll meet it again in Week 3.
-- MAGIC
-- MAGIC 🔮 How many rows should this update? (Use the numbers from step 4.)

-- COMMAND ----------

MERGE INTO workspace.risk_sim.scn01_fees AS cur
USING (
  SELECT fee_id, fee_amount
  FROM workspace.risk_sim.scn01_fees VERSION AS OF 0
) AS v0
ON cur.fee_id = v0.fee_id
WHEN MATCHED
  AND cur.fee_amount <> v0.fee_amount
  AND cur.fee_id NOT IN ('F0001188', 'F0001236', 'F0001315')   -- keep OPS-2291's waivers
THEN UPDATE SET cur.fee_amount = v0.fee_amount;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 6. Prove it's fixed
-- MAGIC Never say "fixed" without evidence. Compare against the original, month by month.
-- MAGIC
-- MAGIC 🔮 Which month should still differ from version 0, and by how much?

-- COMMAND ----------

SELECT date_trunc('month', cur.assessed_date)   AS month,
       sum(v0.fee_amount)                       AS v0_dollars,
       sum(cur.fee_amount)                      AS now_dollars,
       sum(cur.fee_amount) - sum(v0.fee_amount) AS difference
FROM workspace.risk_sim.scn01_fees cur
JOIN workspace.risk_sim.scn01_fees VERSION AS OF 0 v0 USING (fee_id)
GROUP BY 1
ORDER BY 1;

-- COMMAND ----------

DESCRIBE HISTORY workspace.risk_sim.scn01_fees;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC 🔮 Look at the history again. Did the bad update **disappear**? Why is it a good thing that it didn't?

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 7. Report back ✍️
-- MAGIC Write the Slack reply to the Risk Manager — 3 to 4 sentences: **what happened, impact, what you did, how you verified.**
-- MAGIC
-- MAGIC Then one line for yourself: **how could this have been prevented?** (Think about the Friday script's `WHERE` clause, and about who should be allowed to run UPDATE on a production table.)
