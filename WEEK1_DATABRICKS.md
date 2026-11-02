# 第 1 週：把廚房整理好（Databricks 平台練習）

**目標**：這週不辦案，只練「平台本身」。做完你應該能不靠 AI 回答：資料放在哪、型別對不對、表格被誰改過、怎麼還原。

**時間**：約 6 小時（另外 2 小時讀 SPEC.md 的業務規則）

**AI 使用規則**：
- ✅ 可以問 AI：錯誤訊息是什麼意思、某個語法怎麼寫
- ❌ 不要讓 AI 直接寫 Step 4 的查詢。自己寫，寫完再請 AI review
- 🎯 每個標了「**先預測**」的地方，執行前先把你的猜測寫在 notebook 的 markdown cell

---

## Step 1｜建立 schema 與 volume（30 分）

**比喻**：catalog = 整棟大樓，schema = 一個樓層（部門），table = 檔案櫃，volume = 收發室（放原始檔案）。

Free Edition 預設的 catalog 叫 `workspace`。在 SQL editor 或 notebook 執行：

```sql
CREATE SCHEMA IF NOT EXISTS workspace.risk_sim
  COMMENT 'Synthetic US card risk event: August payment posting outage';

CREATE VOLUME IF NOT EXISTS workspace.risk_sim.raw
  COMMENT 'Raw CSV landing zone';
```

上傳檔案：左側 **Catalog** → `workspace` → `risk_sim` → `raw` → **Upload to this volume**，把 `data/` 裡 8 個 CSV 全部上傳。

> 💡 另一種做法：把 `generate_data.py` 放進 Databricks，直接在 notebook 執行，`--out` 指到 `/Volumes/workspace/risk_sim/raw`。不用上傳 25MB 的檔案，而且之後換 seed 很方便。

---

## Step 2｜建立 bronze tables（1 小時）

**Bronze / Silver / Gold** 是 Databricks 常見的分層方式（Medallion architecture）：

| 層 | 比喻 | 內容 |
|---|---|---|
| Bronze | 剛進貨的食材 | 原始資料，原封不動 |
| Silver | 洗好切好的食材 | 清理、去重、型別正確 |
| Gold | 端上桌的菜 | 業務用的結果（例如 remediation_file） |

```sql
CREATE OR REPLACE TABLE workspace.risk_sim.bronze_payments AS
SELECT *, current_timestamp() AS _ingested_at, _metadata.file_path AS _source_file
FROM read_files(
  '/Volumes/workspace/risk_sim/raw/payments.csv',
  format => 'csv', header => true, inferSchema => true
);
```

其他 7 張表照做（`bronze_customers`, `bronze_accounts`, ...）。

**先預測**：`received_ts_utc` 會被推論成什麼型別？`posted_date` 呢？`autopay_enrolled` 呢？

然後檢查：

```sql
DESCRIBE TABLE workspace.risk_sim.bronze_payments;
```

推論錯的話（例如時間變成 string），記下來。Silver 層要修正。這是金融資料最常出錯的地方之一。

---

## Step 3｜幫表格寫說明（30 分）

這一步同時在練 Databricks 和業務知識。把 SPEC.md 的「業務意義」寫進 catalog：

```sql
COMMENT ON TABLE workspace.risk_sim.bronze_payments IS
  'One row per payment received. received_ts = when the bank received it; posted_date = when it hit the account.';

ALTER TABLE workspace.risk_sim.bronze_payments
  ALTER COLUMN received_ts_utc COMMENT 'Timestamp the payment was received, stored in UTC';
```

至少幫 `payments`、`statements`、`fees` 的關鍵欄位寫上說明，**用你自己的話寫**。寫不出來的欄位，就是你還沒懂的業務概念。

做完去 Catalog Explorer 看看，說明會顯示在 UI 上。在公司，這就是別人認識你的表格的方式。

---

## Step 4｜資料剖析 Data Profiling（2 小時，自己寫）

對每張表回答下面的問題，每題寫一個查詢：

| # | 問題 | 為什麼重要 |
|---|---|---|
| 1 | 筆數跟 CSV 行數（減 header）一樣嗎？ | 確認載入沒漏 |
| 2 | 主鍵真的唯一嗎？（`payment_id`, `fee_id`, `statement_id`...） | 重複會讓 join 後金額變兩倍 |
| 3 | 每個日期欄位的最小、最大值？ | 確認時間範圍合理 |
| 4 | 每個欄位有幾個 null？哪些 null 是合理的？ | `closed_date` 有 null 合理，`amount` 沒有 |
| 5 | 每個帳戶是不是剛好 5 張帳單？ | 檢查資料完整性 |
| 6 | `statements` 的餘額公式成立嗎？`previous − payments − credits + purchases + fees + interest = new_balance` | 這是會計恆等式，不成立代表資料有問題 |
| 7 | 每月的 late fee 筆數、總額？ | **先預測**：每個月應該差不多嗎？ |

> ⚠️ 第 2、7 題可能會讓你發現奇怪的東西。**記下來，但先不要深挖**，那是第 2 週的工作。在 notebook 裡開一個「Open Questions」的 markdown cell 放這些發現。

---

## Step 5｜Delta history 與 time travel（1 小時）

**情境**：你的同事不小心跑錯一個 UPDATE，把八月的 late fee 全改成 0。你要找出發生什麼事並還原。

```sql
-- 1. 先複製一份來練習
CREATE OR REPLACE TABLE workspace.risk_sim.silver_fees AS
SELECT * FROM workspace.risk_sim.bronze_fees;

-- 2. 模擬同事的錯誤
UPDATE workspace.risk_sim.silver_fees
SET fee_amount = 0
WHERE assessed_date BETWEEN '2026-08-01' AND '2026-08-31';

-- 3. 查誰在什麼時候做了什麼
DESCRIBE HISTORY workspace.risk_sim.silver_fees;
```

**先預測**：history 會有幾個版本？

```sql
-- 4. 比對前後差異（版本號換成你看到的）
SELECT 'before' AS v, count(*), sum(fee_amount) FROM workspace.risk_sim.silver_fees VERSION AS OF 0
UNION ALL
SELECT 'after', count(*), sum(fee_amount) FROM workspace.risk_sim.silver_fees;

-- 5. 還原
RESTORE TABLE workspace.risk_sim.silver_fees TO VERSION AS OF 0;
```

**為什麼這對 Risk Events 重要**：查 root cause 時，常要問「事故發生那天，這張表長什麼樣子？」。Time travel 讓你回到過去看資料，而且每次修改都有紀錄可查，這在受監管的金融業非常重要。

---

## Step 6｜Notebook 結構與參數（45 分）

建立 notebook `01_profiling`，照這個結構排：

```
[markdown] 目的、日期、作者
[python]   參數（widgets）
[sql]      Step 4 的查詢
[markdown] 發現 & Open Questions
```

參數範例：

```python
dbutils.widgets.text("schema", "workspace.risk_sim")
dbutils.widgets.text("event_start", "2026-08-01")
dbutils.widgets.text("event_end", "2026-08-31")
```

SQL cell 裡用 `:event_start` 引用參數（named parameter markers）：

```sql
SELECT count(*) FROM IDENTIFIER(:schema || '.bronze_fees')
WHERE assessed_date BETWEEN :event_start AND :event_end;
```

**為什麼要參數化**：下次換一個事故、換一個日期區間，同一份 notebook 改參數就能重跑，不用改 code。

---

## Step 7｜連 GitHub（30 分）

1. 在 GitHub 建 repo `risk-event-sim`，把 `generate_data.py`、`SPEC.md`、這份指南放進去（**不要放 `answer_key/`**，加進 `.gitignore`）
2. Databricks 左側 **Workspace** → **Create** → **Git folder**，貼上 repo URL
3. 把 `01_profiling` notebook 存進 Git folder，commit + push

之後的流程：Claude Code 在你電腦改 code → push → Databricks Git folder pull → 執行。

---

## ✅ 第 1 週自我檢查（不看 AI 能回答嗎？）

- [ ] `workspace.risk_sim.bronze_payments` 這個名字的三段各代表什麼？
- [ ] Bronze 跟 Silver 差在哪？你的 silver 層需要修正哪些型別？
- [ ] 怎麼查一張表過去被誰改過？怎麼看三天前的版本？
- [ ] `received_ts_utc` 和 `posted_date` 的業務意義差在哪？
- [ ] 依 R5，什麼情況下客戶本期不用付利息？
- [ ] 你的 Open Questions 清單上有什麼？
