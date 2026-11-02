# Risk Event Simulation — Specification

> A simulated US credit card **risk event**. The goal is to run the full remediation workflow end to end:
> **identify impacted accounts → confirm root cause → calculate remediation → produce the remediation file → validate.**
> Every table and column below includes its business meaning.

---

## 0. How to Use This Spec

| Step | What to do |
|---|---|
| 1 | Read §1–3 (incident ticket + data model) and sketch how the tables relate |
| 2 | Load the data into Databricks (Bronze → Silver) and profile it |
| 3 | Use the business rules in §4–5 to determine who was impacted and by how much |
| 4 | Produce the deliverables in §6 and run every validation check |

---

## 1. Incident Ticket

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

The ticket is intentionally vague, as real tickets are. Point 4 is where an analyst who asks *why* adds the most value.

---

## 2. Data Model

```
customers 1 ──< accounts 1 ──< statements 1 ──< fees 1 ──< fee_adjustments
                     │                │
                     │                └── (each statement summarizes that cycle's payments / purchases / fees / interest)
                     ├──< payments
                     └──< transactions   (ledger of every posted item: PURCHASE / PAYMENT / FEE / INTEREST)

system_events  (not linked to accounts; system incident log used for root cause analysis)
```

**Analogy:** `statements` is the cover page of the monthly bill; `transactions` is the itemized detail on the back. `payments` records **when the customer paid**; a `PAYMENT` row in `transactions` records **when the money was posted** to the account. On a normal day those two dates are the same, but…

Time range: statement dates fall between **May and September 2026**, one statement per account per month.

---

## 3. Tables and Columns

### 3.1 `customers` (~8,700 rows)

| Column | Type | Business meaning |
|---|---|---|
| customer_id | string | Customer ID (one customer can hold multiple cards) |
| home_state | string | State of residence |
| fico_band | string | Credit score band. FICO (300–850) is the most widely used US credit score; it drives credit limit, APR, and product eligibility |
| segment | string | `MAINSTREET` (mass market) / `UPMARKET` (high credit, high spend). Used for servicing and call routing |
| customer_since | date | Date the customer relationship began |

### 3.2 `accounts` (10,000 rows)

| Column | Type | Business meaning |
|---|---|---|
| account_id | string | Card account ID |
| customer_id | string | FK → customers |
| product | string | Card product. `SECURED_CARD` = secured card for thin or rebuilding credit; requires a security deposit |
| open_date | date | Account open date |
| credit_limit | number | Credit limit |
| purchase_apr | number | Purchase APR (0.2449 = 24.49%). APR = Annual Percentage Rate |
| statement_cycle_day | int | Day of month the statement closes (1–28); cycles differ by account |
| autopay_enrolled | bool | Enrolled in AutoPay (debited on the due date) |
| account_status | string | `OPEN` / `CLOSED` |
| closed_date | date | Account close date |

### 3.3 `statements` (50,000 rows = 10,000 accounts × 5 cycles)

| Column | Business meaning |
|---|---|
| statement_id | Statement ID |
| cycle_start_date / statement_date | Start and close date of the billing cycle |
| days_in_cycle | Number of days in the cycle (used for interest) |
| due_date | **Payment due date** = statement date + 25 days |
| previous_balance | Balance carried from the prior statement |
| payments | Payments posted during the cycle |
| credits_adjustments | Credits posted during the cycle (e.g., fee waivers by agents) |
| purchases | Purchases posted during the cycle |
| fees_charged | Fees assessed during the cycle (late fees only in this dataset) |
| interest_charged | Interest charged for the cycle |
| avg_daily_balance | Average Daily Balance (ADB), the basis for interest |
| new_balance | previous − payments − credits + purchases + fees + interest |
| minimum_due | Minimum payment due |

### 3.4 `payments`

| Column | Business meaning |
|---|---|
| payment_id | Payment ID |
| account_id | FK → accounts |
| channel | `AUTOPAY` / `ONLINE` / `MOBILE` / `PHONE` |
| amount | Payment amount |
| received_ts_utc | **Timestamp the bank received the payment (UTC)** |
| posted_date | **Date the payment was posted to the account** |
| payment_status | Status |

### 3.5 `fees`

| Column | Business meaning |
|---|---|
| fee_id | Fee ID |
| account_id / statement_id | Account, and the statement the fee appears on |
| fee_type | `LATE_FEE` |
| fee_amount | $30 or $41 (see rules) |
| assessed_date | Date assessed (the day after the due date) |

### 3.6 `fee_adjustments`

Individual fee reversals made by agents after customers called in to complain.

| Column | Business meaning |
|---|---|
| adjustment_id | Adjustment ID |
| fee_id | The fee that was reversed |
| account_id | Account |
| adjustment_date | Reversal date |
| amount | Amount reversed |
| reason_code | `GOODWILL_WAIVER` = discretionary waiver to preserve the relationship |
| agent_id | Agent who processed it |

### 3.7 `transactions` (~555,000 rows)

| Column | Business meaning |
|---|---|
| account_id | Account |
| txn_type | `PURCHASE` / `PAYMENT` / `FEE` / `INTEREST` |
| post_date | Posting date |
| amount | Amount (PAYMENT is negative, i.e., reduces the balance owed) |
| merchant_category | Merchant category (PURCHASE only) |
| reference_id | Related payment_id / fee_id / statement_id |

### 3.8 `system_events`

| Column | Business meaning |
|---|---|
| event_id | Incident ID |
| system_name | System affected |
| start_ts_utc / end_ts_utc | Incident start and end (UTC) |
| description | Description |
| status | Status |

---

## 4. Business Rules

These describe how company policy **should** work. The job is to find gaps between **policy** and **what the system actually did**.

| # | Rule | Detail |
|---|---|---|
| R1 | **On-time payment** | A payment is on time if it is **received by 11:59 PM Eastern Time (ET) on the due date**. Receipt time is what counts, not posting time |
| R2 | Normal processing | Payments post on the day they are received (posted_date = received date in ET) |
| R3 | **Late** | No payment received by the due date, or the amount received is < minimum_due |
| R4 | **Late fee** | Assessed the day after the due date when late. **$30** if no late fee in the prior 6 cycles; otherwise **$41** |
| R5 | **Grace period** | If the prior statement's new_balance is **paid in full by its due date**, no interest is charged this cycle. Otherwise interest applies |
| R6 | **Interest (simplified)** | `interest = round(ADB × APR / 365 × days_in_cycle, 2)`<br>`ADB = (prev_bal × d_before + (prev_bal − payment) × d_after) / days_in_cycle + purchases × 0.5`<br>`d_before` = days from cycle start to the payment posting date; `d_after = days_in_cycle − d_before`. With no payment, `d_before = days_in_cycle` |
| R7 | Minimum due | `max($25, 1% × new_balance + cycle fees + cycle interest)`, capped at new_balance |
| R8 | Timestamps | All system timestamps are stored in **UTC** |

### Glossary

| Term | Meaning |
|---|---|
| Transactor | Pays the full balance every month; typically pays no interest |
| Revolver | Pays less than the full balance and carries the rest; pays interest |
| Grace period | Interest-free period earned by paying in full; lost once a balance is carried |
| DPD (Days Past Due) | Days delinquent. Accounts are reported to the credit bureaus only at **30+ DPD** |
| Remediation | Restoring customers to the position they would have been in had the error not occurred |

---

## 5. Remediation Policy (from the Risk Manager)

1. **Make the customer whole:** refund any fee or interest that would not have been charged if the payment had posted on the day it was received.
2. Amounts already reversed by agents **must not be refunded again**.
3. Interest on interest (extra interest from a fee being added to the balance) is **out of scope** for this event.
4. Method: `OPEN` accounts → `STATEMENT_CREDIT`; `CLOSED` accounts → `MAILED_CHECK`.
5. Refund any amount > $0; there is no minimum threshold.

---

## 6. Deliverables

| # | Deliverable | Format |
|---|---|---|
| D1 | Root cause + impact window | Short write-up + supporting queries |
| D2 | Impacted account list | Table `remediation_file`: account_id, fee_refund, interest_refund, remediation_total, remediation_method, harm_types |
| D3 | Impact summary | Accounts and $ by harm type |
| D4 | Validation report | Result of every check below |
| D5 | Campaign config | `campaign_config.json` (simulated input to the fulfillment platform) |
| D6 | One-page memo | For the Risk Manager |

### Required Validation Checks

- [ ] No duplicate account_id in the remediation file
- [ ] No negative amounts
- [ ] Each account's refund ≤ the amount actually charged
- [ ] No fee already waived by an agent is refunded again
- [ ] Account counts and totals reconcile at every step (impacted population → final file)
- [ ] All CLOSED accounts use MAILED_CHECK

---

## 7. Simplifications vs. the Real World

| Real world | Simplification here |
|---|---|
| Interest accrues on daily balances; purchase and cash APRs differ | Simplified ADB formula (R6) with a single APR |
| Grace period usually requires two consecutive pay-in-full cycles to restore | Restored after one pay-in-full cycle |
| Many fee types (annual, foreign transaction, over-limit) | Late fees only |
| Late fee amounts are governed by regulation (CARD Act / Reg Z) | Fixed at $30 / $41 |
| Payments can be returned | No returned payments |

---

## 8. Regenerating the Data

```bash
python generate_data.py --accounts 10000 --seed 42 --out data
```

Change `--seed` for a fresh dataset to practice again, or use `--accounts 100000` to practice performance at scale.
