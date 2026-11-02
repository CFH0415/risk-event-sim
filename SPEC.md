# Risk Event Simulation — 資料規格 (SPEC)

> 模擬一個美國信用卡的 **risk event**，讓你從「找出受影響帳戶 → 查 root cause → 算 remediation 金額 → 產出修復名單 → 驗證」完整跑一遍。
> 業務知識邊做邊學：每張表、每個欄位都附「業務意義」。

---

## 0. 怎麼開始

| 步驟 | 做什麼 |
|---|---|
| 1 | 讀第 1–3 節（事故單 + 資料模型），先在腦中畫出表格關係 |
| 2 | 照 `WEEK1_DATABRICKS.md` 把資料載進 Databricks |
| 3 | 第 2 週才開始「辦案」，用第 4–5 節的業務規則判斷誰被影響 |
| 4 | 全部做完才能打開 `answer_key.zip` 對答案 |

---

## 1. 事故單（Incident Ticket）

> **From:** Risk Manager, US Card Payments
> **Priority:** High
>
> Since mid-August we've seen a spike in customer complaints: customers say they paid on time in early August but were still charged a late fee, and some say their interest charges look higher than usual. Agents have already waived some fees one by one when customers called in.
>
> We believe this is related to a payments platform incident in early August, but we need data to confirm.
>
> **Please:**
> 1. Confirm the root cause and the exact impact window.
> 2. Size the impacted population (number of accounts, total $).
> 3. Propose a remediation file: who gets what amount, by which method.
> 4. Tell us if anything else (beyond late fees) was affected.

這張單故意寫得模糊，跟真實工作一樣。第 4 點是主管說的「會問 why 的 DA」要表現的地方。

---

## 2. 資料模型（Domain Model）

```
customers 1 ──< accounts 1 ──< statements 1 ──< fees 1 ──< fee_adjustments
                     │                │
                     │                └── (一期帳單包含當期的 payments / purchases / fees / interest)
                     ├──< payments
                     └──< transactions   (所有入帳動作的明細帳：PURCHASE / PAYMENT / FEE / INTEREST)

system_events  （跟帳戶無直接關聯；系統事故紀錄，查 root cause 用）
```

**比喻**：`statements` 是每月的「對帳單封面」，`transactions` 是對帳單背面的「明細」。`payments` 是「收款紀錄」，記錄客戶**什麼時候付款**；`transactions` 裡的 PAYMENT 記錄錢**什麼時候入帳**。這兩個時間平常一樣，但……

時間範圍：帳單結帳日落在 **2026 年 5–9 月**，每個帳戶每月一張帳單。

---

## 3. 表格與欄位

### 3.1 `customers`（約 8,700 筆）

| 欄位 | 型別 | 業務意義 |
|---|---|---|
| customer_id | string | 客戶 ID（一個客戶可以有多張卡） |
| home_state | string | 居住州 |
| fico_band | string | 信用分數區間。FICO 是美國最常用的信用分數（300–850），影響額度、利率、能拿到哪種卡 |
| segment | string | `MAINSTREET`（一般客群）/ `UPMARKET`（高信用、高消費客群）。主管提到的 call routing 就是依這個分流 |
| customer_since | date | 成為客戶的日期 |

### 3.2 `accounts`（10,000 筆）

| 欄位 | 型別 | 業務意義 |
|---|---|---|
| account_id | string | 卡片帳戶 ID |
| customer_id | string | FK → customers |
| product | string | 卡別。`SECURED_CARD` = 擔保卡，給信用紀錄不足的人，需先存保證金 |
| open_date | date | 開卡日 |
| credit_limit | number | 信用額度 |
| purchase_apr | number | 消費年利率（0.2449 = 24.49%）。APR = Annual Percentage Rate |
| statement_cycle_day | int | 每月結帳日（1–28）。每個人的帳單週期不同 |
| autopay_enrolled | bool | 是否設定自動扣款（在到期日當天扣） |
| account_status | string | `OPEN` / `CLOSED` |
| closed_date | date | 關戶日 |

### 3.3 `statements`（50,000 筆 = 10,000 帳戶 × 5 期）

| 欄位 | 業務意義 |
|---|---|
| statement_id | 帳單 ID |
| cycle_start_date / statement_date | 這期帳單週期的開始／結帳日 |
| days_in_cycle | 週期天數（算利息用） |
| due_date | **繳款到期日** = 結帳日 + 25 天 |
| previous_balance | 上期餘額 |
| payments | 本期入帳的還款 |
| credits_adjustments | 本期的調整（例如客服退的費用） |
| purchases | 本期消費 |
| fees_charged | 本期收的費用（這裡只有 late fee） |
| interest_charged | 本期收的利息 |
| avg_daily_balance | 平均每日餘額（Average Daily Balance, ADB），利息的計算基礎 |
| new_balance | 本期餘額 = previous − payments − credits + purchases + fees + interest |
| minimum_due | 本期最低應繳 |

### 3.4 `payments`

| 欄位 | 業務意義 |
|---|---|
| payment_id | 付款 ID |
| account_id | FK → accounts |
| channel | `AUTOPAY` / `ONLINE` / `MOBILE` / `PHONE` |
| amount | 付款金額 |
| received_ts_utc | **銀行收到付款的時間（UTC 時區）** |
| posted_date | **付款入帳到帳戶的日期** |
| payment_status | 狀態 |

### 3.5 `fees`

| 欄位 | 業務意義 |
|---|---|
| fee_id | 費用 ID |
| account_id / statement_id | 屬於哪個帳戶、出現在哪期帳單 |
| fee_type | `LATE_FEE` |
| fee_amount | $30 或 $41（見規則） |
| assessed_date | 收費日（到期日隔天） |

### 3.6 `fee_adjustments`

客服在客戶打電話來抱怨後，個別退費的紀錄。

| 欄位 | 業務意義 |
|---|---|
| adjustment_id | 調整 ID |
| fee_id | 退的是哪一筆費用 |
| account_id | 帳戶 |
| adjustment_date | 退費日 |
| amount | 退費金額 |
| reason_code | `GOODWILL_WAIVER` = 客服基於客戶關係主動減免 |
| agent_id | 處理的客服 |

### 3.7 `transactions`（約 55 萬筆）

| 欄位 | 業務意義 |
|---|---|
| account_id | 帳戶 |
| txn_type | `PURCHASE` / `PAYMENT` / `FEE` / `INTEREST` |
| post_date | 入帳日 |
| amount | 金額（PAYMENT 為負數，代表減少欠款） |
| merchant_category | 消費類別（僅 PURCHASE） |
| reference_id | 對應的 payment_id / fee_id / statement_id |

### 3.8 `system_events`

| 欄位 | 業務意義 |
|---|---|
| event_id | 事故 ID |
| system_name | 出問題的系統 |
| start_ts_utc / end_ts_utc | 事故起訖時間（UTC） |
| description | 描述 |
| status | 狀態 |

---

## 4. 業務規則（Business Rules）

這是「公司政策應該怎麼運作」。你的工作是比對**政策**跟**系統實際做的**有沒有落差。

| # | 規則 | 說明 |
|---|---|---|
| R1 | **準時付款的定義** | 付款只要在到期日當天 **美東時間 (ET) 23:59 前收到**，就算準時。依據是「收到時間」，不是入帳時間 |
| R2 | 正常作業 | 付款收到當天就會入帳（posted_date = 收到日期，ET） |
| R3 | **逾期（late）** | 到期日前沒收到付款，或收到的金額 < minimum_due |
| R4 | **Late fee** | 逾期時收取，在到期日隔天收費。過去 6 期內沒有 late fee → **$30**；有 → **$41** |
| R5 | **Grace period（寬限期）** | 如果上期帳單的 new_balance **在到期日前全額付清**，本期就不收利息。沒付清 → 本期收利息 |
| R6 | **利息計算（簡化版）** | `interest = round(ADB × APR / 365 × days_in_cycle, 2)`<br>`ADB = (prev_bal × d_before + (prev_bal − payment) × d_after) / days_in_cycle + purchases × 0.5`<br>`d_before` = 從週期開始到付款入帳日的天數；`d_after = days_in_cycle − d_before`。沒付款時 `d_before = days_in_cycle` |
| R7 | Minimum due | `max($25, 1% × new_balance + 本期 fees + 本期 interest)`，但不超過 new_balance |
| R8 | 時間儲存 | 系統時間戳記一律存 **UTC** |

### 名詞小抄

| 名詞 | 白話 |
|---|---|
| Transactor | 每月全額付清的人，通常不付利息 |
| Revolver | 只付部分金額、讓餘額滾到下個月的人，會付利息 |
| Grace period | 「全額付清就免息」的優惠。一旦沒全額付清就失去 |
| DPD (Days Past Due) | 逾期天數。**逾期 30 天以上**才會回報給信用局（credit bureau） |
| Remediation | 公司出錯後，把客戶「恢復到沒出錯時應有的狀態」 |

---

## 5. Remediation Policy（風險經理給的原則）

1. **Make the customer whole**：凡是「如果付款在收到當天就入帳，就不會被收」的費用或利息，都要退。
2. 已經被客服退過的金額，**不能再退一次**。
3. 「利息的利息」（費用被算進餘額後產生的額外利息）這次 **out of scope**。
4. 退款方式：帳戶 `OPEN` → `STATEMENT_CREDIT`；`CLOSED` → `MAILED_CHECK`。
5. 金額 > $0 就要退，沒有最低門檻。

---

## 6. 交付物（Deliverables）

| # | 交付物 | 格式 |
|---|---|---|
| D1 | Root cause + 影響時間窗 | 一段文字 + 支持的查詢 |
| D2 | 受影響帳戶名單 | 表格 `remediation_file`：account_id, fee_refund, interest_refund, remediation_total, remediation_method, harm_types |
| D3 | Impact summary | 依 harm type 分的帳戶數、金額 |
| D4 | 驗證報告 | 每個檢查的結果（見下方） |
| D5 | Campaign 設定檔 | `campaign_config.json`（模擬 Fractal 的輸入） |
| D6 | 一頁 memo | 給 risk manager 看的 |

### 必要的驗證檢查

- [ ] 名單裡沒有重複的 account_id
- [ ] 沒有負數金額
- [ ] 每個帳戶的退費 ≤ 實際被收的金額
- [ ] 已退過的 fee 沒有被重複退
- [ ] 每一步的帳戶數、總金額能對得上（從 impacted population → 最終名單）
- [ ] CLOSED 帳戶都用 MAILED_CHECK

---

## 7. 跟真實世界的差異（簡化）

| 真實世界 | 這裡的簡化 |
|---|---|
| 利息按每日餘額逐日計算，消費、現金預借利率不同 | 用 R6 的簡化 ADB 公式，只有一種 APR |
| Grace period 失去後，通常要連續兩期全額付清才恢復 | 下一期全額付清就恢復 |
| 有很多種費用（年費、海外交易費、超額費） | 只有 late fee |
| Late fee 金額受法規（CARD Act / Reg Z）規範 | 固定 $30 / $41 |
| 付款可能被退票（returned payment） | 沒有 |

---

## 8. 重新產生資料

```bash
python generate_data.py --accounts 10000 --seed 42 --out data
```

換 `--seed` 可以得到一份新的資料，練第二次時用。`--accounts 100000` 可以練大資料量下的效能。
