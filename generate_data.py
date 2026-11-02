"""
Synthetic US credit card data with one planted "risk event".

Usage:
    python generate_data.py --accounts 10000 --seed 42 --out data

Outputs (CSV):
    data/customers.csv, accounts.csv, statements.csv, payments.csv,
    fees.csv, fee_adjustments.csv, transactions.csv, system_events.csv
    answer_key/remediation_answer_key.csv  (DON'T peek until you finish!)

All dates are 2026. Statement cycles close in May..Sep 2026.
See SPEC.md for the business rules this simulation follows.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ET = "America/New_York"
OUTAGE_START = pd.Timestamp("2026-08-03 06:00", tz=ET)
OUTAGE_END = pd.Timestamp("2026-08-07 22:00", tz=ET)
BACKLOG_POST_DATE = pd.Timestamp("2026-08-08")
SEED_MONTH = 4                      # April statement = starting state (not exported)
CYCLE_MONTHS = [5, 6, 7, 8, 9]      # exported statement months
DUE_DAYS = 25
LATE_FEE_FIRST, LATE_FEE_REPEAT = 30.00, 41.00
MIN_DUE_FLOOR = 25.00


def min_due(new_bal, fees, interest):
    raw = np.maximum(MIN_DUE_FLOOR, np.round(0.01 * new_bal + fees + interest, 2))
    return np.where(new_bal > 0, np.minimum(raw, np.round(new_bal, 2)), 0.0)


def stmt_dates(year, month, cycle_day):
    return pd.to_datetime(pd.DataFrame({"year": year, "month": month, "day": cycle_day}))


def main(n_accounts, seed, out_dir):
    rng = np.random.default_rng(seed)
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    key_dir = Path("answer_key"); key_dir.mkdir(exist_ok=True)

    # ---------------- customers & accounts ----------------
    n_cust = int(n_accounts / 1.15)
    cust_ids = np.array([f"C{1000000 + i}" for i in range(n_cust)])
    fico = rng.normal(710, 60, n_cust).clip(540, 845).round().astype(int)
    fico_band = pd.cut(fico, [0, 619, 679, 739, 900],
                       labels=["<620", "620-679", "680-739", "740+"]).astype(str)
    segment = np.where((fico >= 740) & (rng.random(n_cust) < 0.8), "UPMARKET", "MAINSTREET")
    states = rng.choice(["VA", "MD", "DC", "NY", "TX", "CA", "FL", "MN", "IL", "GA"], n_cust)
    customers = pd.DataFrame({
        "customer_id": cust_ids, "home_state": states, "fico_band": fico_band,
        "segment": segment,
        "customer_since": pd.to_datetime("2012-01-01")
        + pd.to_timedelta(rng.integers(0, 365 * 13, n_cust), unit="D"),
    })

    owner_idx = np.concatenate([np.arange(n_cust),
                                rng.choice(n_cust, n_accounts - n_cust, replace=False)])
    owner_idx.sort()
    n = len(owner_idx)
    acct_ids = np.array([f"A{2000000 + i}" for i in range(n)])
    a_fico = fico[owner_idx]
    product = np.where(a_fico < 620, "SECURED_CARD",
                       rng.choice(["CASHBACK_CARD", "TRAVEL_CARD", "STUDENT_CARD"], n,
                                  p=[0.6, 0.3, 0.1]))
    limit = np.where(product == "SECURED_CARD", rng.choice([300, 500, 1000], n),
                     np.round(rng.lognormal(np.log(np.maximum(4000 + (a_fico - 620) * 60, 1500)), 0.35, n), -2))
    limit = np.maximum(limit, 300)
    apr = np.select([a_fico < 620, a_fico < 680, a_fico < 740], [0.2999, 0.2749, 0.2449], 0.2199)
    apr = np.round(apr + rng.choice([0, 0.01, 0.02], n), 4)
    cycle_day = rng.integers(1, 29, n)
    autopay = rng.random(n) < 0.30
    behavior = rng.choice(["TRANSACTOR", "REVOLVER", "MIN_PAYER"], n, p=[0.5, 0.35, 0.15])
    behavior = np.where(a_fico < 620, rng.choice(["REVOLVER", "MIN_PAYER"], n), behavior)
    spend = np.minimum(rng.lognormal(np.log(600), 0.6, n), limit * 0.6)

    # ---------------- seed (April) state ----------------
    bal = np.select([behavior == "TRANSACTOR", behavior == "REVOLVER"],
                    [spend * rng.uniform(0.6, 1.4, n), limit * rng.uniform(0.2, 0.7, n)],
                    limit * rng.uniform(0.4, 0.9, n))
    bal = np.round(np.minimum(bal, limit), 2)
    bal[rng.random(n) < 0.05] = 0.0                    # some idle accounts
    prev_min = min_due(bal, 0, 0)
    prev_stmt = stmt_dates(2026, SEED_MONTH, cycle_day)
    prev_due = prev_stmt + pd.Timedelta(days=DUE_DAYS)

    sys_late_hist, cor_late_hist = [], []
    stmt_rows, pay_rows, fee_rows, txn_rows, key_rows = [], [], [], [], []
    pending_waivers = np.zeros(n)                     # credits applied next cycle
    waiver_rows = []
    pay_seq = fee_seq = 0

    for m in CYCLE_MONTHS:
        stmt = stmt_dates(2026, m, cycle_day)
        c_start = prev_stmt + pd.Timedelta(days=1)
        days = (stmt - prev_stmt).dt.days.to_numpy()
        need_pay = bal > 0

        # ---- payment behavior for previous statement ----
        r = rng.random(n)
        manual = ~autopay
        is_late = manual & (r < 0.04)
        is_short = manual & (r >= 0.04) & (r < 0.06)
        is_none = manual & (r >= 0.06) & (r < 0.07)
        paid = need_pay & ~is_none

        full = bal.copy()
        amt = np.select(
            [behavior == "TRANSACTOR", behavior == "REVOLVER"],
            [full, np.round(rng.uniform(prev_min, np.maximum(prev_min, full)), 2)],
            prev_min)
        amt = np.where(autopay & (behavior != "TRANSACTOR"), prev_min, amt)
        amt = np.where(is_short, np.round(prev_min * rng.uniform(0.3, 0.9, n), 2), amt)
        amt = np.where(paid, np.minimum(amt, full), 0.0)

        days_before = rng.choice(np.arange(15), n,
                                 p=(lambda w: w / w.sum())(np.array([15, 12, 10, 9, 8, 7, 6, 6, 5, 5, 4, 4, 3, 3, 3], float)))
        days_before = np.where(autopay, 0, days_before)
        late_days = rng.integers(1, 4, n)
        offset = np.where(is_late, late_days, -days_before)
        recv_day = prev_due + pd.to_timedelta(offset, unit="D")
        hour = np.where(autopay, 7, rng.choice(np.arange(24), n,
                        p=(lambda w: w / w.sum())(np.array([1,1,1,1,1,2,3,4,5,6,6,6,6,6,6,6,6,6,6,6,5,4,3,2], float))))
        minute = rng.integers(0, 60, n)
        recv_et = (recv_day + pd.to_timedelta(hour, unit="h") + pd.to_timedelta(minute, unit="m")
                   ).dt.tz_localize(ET)
        # keep late payments inside this cycle
        recv_et = recv_et.where(recv_et.dt.tz_localize(None).dt.normalize() <= stmt,
                                (stmt + pd.Timedelta(hours=10)).dt.tz_localize(ET))
        in_outage = (recv_et >= OUTAGE_START) & (recv_et <= OUTAGE_END)
        # outage backlog posts 8/8; if that is after cycle end, move receipt before outage
        bad = in_outage & (stmt < BACKLOG_POST_DATE)
        if bad.any():
            recv_et = recv_et.where(~bad, pd.Timestamp("2026-08-02 15:00", tz=ET))
            in_outage = (recv_et >= OUTAGE_START) & (recv_et <= OUTAGE_END)
        recv_date = recv_et.dt.tz_localize(None).dt.normalize()
        posted = recv_date.where(~in_outage, BACKLOG_POST_DATE)

        pd_due = prev_due
        cor_on_time = (recv_date <= pd_due).to_numpy()
        sys_on_time = (posted <= pd_due).to_numpy()
        enough = amt >= prev_min - 0.005
        cor_late = need_pay & (~paid | ~cor_on_time | ~enough)
        sys_late = need_pay & (~paid | ~sys_on_time | ~enough)

        def tier(hist, late):
            prior = np.zeros(n, bool)
            for h in hist[-6:]:
                prior |= h
            return np.where(late, np.where(prior, LATE_FEE_REPEAT, LATE_FEE_FIRST), 0.0)

        fee_sys = tier(sys_late_hist, sys_late)
        fee_cor = tier(cor_late_hist, cor_late)
        sys_late_hist.append(sys_late); cor_late_hist.append(cor_late)

        paid_full = amt >= full - 0.005
        cor_grace = ~need_pay | (paid & cor_on_time & paid_full)
        sys_grace = ~need_pay | (paid & sys_on_time & paid_full)

        # ---- purchases ----
        avail = np.maximum(0, limit - (bal - amt))
        purch_total = np.round(np.minimum(spend * rng.uniform(0.5, 1.5, n), avail), 2)
        purch_total[rng.random(n) < 0.05] = 0.0

        # ---- interest (simplified ADB, see SPEC.md) ----
        def adb(credit_date):
            d_before = (credit_date - c_start).dt.days.to_numpy().clip(0, days)
            d_before = np.where(paid, d_before, days)
            return (bal * d_before + (bal - amt) * (days - d_before)) / days + purch_total * 0.5

        adb_sys, adb_cor = adb(posted), adb(recv_date)
        int_sys = np.where(sys_grace, 0.0, np.round(np.maximum(adb_sys, 0) * apr / 365 * days, 2))
        int_cor = np.where(cor_grace, 0.0, np.round(np.maximum(adb_cor, 0) * apr / 365 * days, 2))

        credits = pending_waivers.copy(); pending_waivers[:] = 0
        new_bal = np.round(bal - amt + purch_total + fee_sys + int_sys - credits, 2)
        new_min = min_due(new_bal, fee_sys, int_sys)
        stmt_ids = np.array([f"S{m:02d}{x[1:]}" for x in acct_ids])

        # ---- rows: statements ----
        stmt_rows.append(pd.DataFrame({
            "statement_id": stmt_ids, "account_id": acct_ids,
            "cycle_start_date": c_start.dt.date, "statement_date": stmt.dt.date,
            "days_in_cycle": days, "due_date": (stmt + pd.Timedelta(days=DUE_DAYS)).dt.date,
            "previous_balance": np.round(bal, 2), "payments": np.round(amt, 2),
            "credits_adjustments": np.round(credits, 2), "purchases": purch_total,
            "fees_charged": fee_sys, "interest_charged": int_sys,
            "avg_daily_balance": np.round(np.maximum(adb_sys, 0), 2),
            "new_balance": new_bal, "minimum_due": new_min,
        }))

        # ---- rows: payments ----
        idx = np.where(paid)[0]
        pids = np.array([f"P{pay_seq + i:08d}" for i in range(len(idx))]); pay_seq += len(idx)
        channel = np.where(autopay[idx], "AUTOPAY",
                           rng.choice(["ONLINE", "MOBILE", "PHONE"], len(idx), p=[.45, .45, .1]))
        p = pd.DataFrame({
            "payment_id": pids, "account_id": acct_ids[idx], "channel": channel,
            "amount": amt[idx],
            "received_ts_utc": recv_et.iloc[idx].dt.tz_convert("UTC").dt.strftime("%Y-%m-%d %H:%M:%S"),
            "posted_date": posted.iloc[idx].dt.date.to_numpy(),
            "payment_status": "POSTED",
        })
        pay_rows.append(p)

        # ---- rows: fees + waivers ----
        fidx = np.where(fee_sys > 0)[0]
        fids = np.array([f"F{fee_seq + i:07d}" for i in range(len(fidx))]); fee_seq += len(fidx)
        assessed = (pd_due + pd.Timedelta(days=1)).iloc[fidx]
        fee_rows.append(pd.DataFrame({
            "fee_id": fids, "account_id": acct_ids[fidx], "statement_id": stmt_ids[fidx],
            "fee_type": "LATE_FEE", "fee_amount": fee_sys[fidx],
            "assessed_date": assessed.dt.date.to_numpy()}))
        waived = np.zeros(n)
        if m == 8:   # customers called in after the August statement
            wrongful = (fee_sys[fidx] > 0) & ~cor_late[fidx]
            prob = np.where(wrongful, 0.15, 0.05)
            w = rng.random(len(fidx)) < prob
            widx = fidx[w]
            waived[widx] = fee_sys[widx]
            pending_waivers[widx] = fee_sys[widx]
            wdate = stmt.iloc[widx] + pd.to_timedelta(rng.integers(1, 12, len(widx)), unit="D")
            waiver_rows.append(pd.DataFrame({
                "adjustment_id": [f"ADJ{i:06d}" for i in range(len(widx))],
                "fee_id": fids[w], "account_id": acct_ids[widx],
                "adjustment_date": wdate.dt.date.to_numpy(), "amount": fee_sys[widx],
                "reason_code": "GOODWILL_WAIVER",
                "agent_id": rng.choice([f"AG{i:03d}" for i in range(40)], len(widx))}))

        # ---- rows: transactions ----
        t = []
        cnt = np.where(purch_total > 0, rng.poisson(9, n) + 1, 0)
        rep = np.repeat(np.arange(n), cnt)
        if len(rep):
            w = rng.gamma(1.0, 1.0, len(rep))
            share = w / np.bincount(rep, w, n)[rep]
            amts = np.round(purch_total[rep] * share, 2)
            amts[np.r_[np.cumsum(cnt)[cnt > 0] - 1]] += np.round(
                purch_total[cnt > 0] - np.bincount(rep, amts, n)[cnt > 0], 2)
            off = (rng.random(len(rep)) * days[rep]).astype(int)
            t.append(pd.DataFrame({
                "account_id": acct_ids[rep], "txn_type": "PURCHASE",
                "post_date": (c_start.iloc[rep].reset_index(drop=True)
                              + pd.to_timedelta(off, unit="D")).dt.date,
                "amount": amts,
                "merchant_category": rng.choice(
                    ["GROCERY", "DINING", "GAS", "TRAVEL", "ONLINE_RETAIL", "UTILITIES", "OTHER"],
                    len(rep), p=[.25, .2, .12, .08, .2, .07, .08]),
                "reference_id": None}))
        t.append(pd.DataFrame({"account_id": p.account_id, "txn_type": "PAYMENT",
                               "post_date": p.posted_date, "amount": -p.amount,
                               "merchant_category": None, "reference_id": p.payment_id}))
        t.append(pd.DataFrame({"account_id": acct_ids[fidx], "txn_type": "FEE",
                               "post_date": assessed.dt.date.to_numpy(), "amount": fee_sys[fidx],
                               "merchant_category": None, "reference_id": fids}))
        iidx = np.where(int_sys > 0)[0]
        t.append(pd.DataFrame({"account_id": acct_ids[iidx], "txn_type": "INTEREST",
                               "post_date": stmt.iloc[iidx].dt.date.to_numpy(),
                               "amount": int_sys[iidx], "merchant_category": None,
                               "reference_id": stmt_ids[iidx]}))
        txn_rows.append(pd.concat(t, ignore_index=True))

        # ---- answer key (per account per cycle) ----
        key_rows.append(pd.DataFrame({
            "account_id": acct_ids, "statement_month": m,
            "fee_charged": fee_sys, "fee_correct": fee_cor,
            "interest_charged": int_sys, "interest_correct": int_cor,
            "fee_waived_already": waived}))

        bal, prev_min, prev_stmt, prev_due = new_bal, new_min, stmt, stmt + pd.Timedelta(days=DUE_DAYS)

    # ---------------- account status ----------------
    closed = rng.random(n) < 0.03
    accounts = pd.DataFrame({
        "account_id": acct_ids, "customer_id": cust_ids[owner_idx], "product": product,
        "open_date": (pd.to_datetime("2014-01-01")
                      + pd.to_timedelta(rng.integers(0, 365 * 11, n), unit="D")).date,
        "credit_limit": limit, "purchase_apr": apr, "statement_cycle_day": cycle_day,
        "autopay_enrolled": autopay,
        "account_status": np.where(closed, "CLOSED", "OPEN"),
        "closed_date": np.where(closed, (pd.Timestamp("2026-09-15")
                                         + pd.to_timedelta(rng.integers(0, 30, n), unit="D")).date, None),
    })

    payments = pd.concat(pay_rows, ignore_index=True)
    # outage replay created duplicate rows for some backlog payments
    backlog = payments[pd.to_datetime(payments.posted_date) == BACKLOG_POST_DATE]
    dups = backlog.sample(frac=0.04, random_state=seed)
    payments = pd.concat([payments, dups]).sort_values(["received_ts_utc", "payment_id"])

    system_events = pd.DataFrame([
        ["EVT-0712", "STATEMENT_PRINT_VENDOR", "2026-07-20 13:00", "2026-07-21 02:00",
         "Paper statement print delay; e-statements unaffected", "RESOLVED"],
        ["EVT-0803", "PAYMENT_POSTING_ENGINE", "2026-08-03 10:00", "2026-08-08 02:00",
         "Payment posting queue stalled; payments accepted but not posted. Backlog replayed 8/8.", "RESOLVED"],
        ["EVT-0819", "REWARDS_LEDGER", "2026-08-19 15:30", "2026-08-19 18:10",
         "Rewards points accrual delayed", "RESOLVED"],
        ["EVT-0902", "MOBILE_APP", "2026-09-02 01:00", "2026-09-02 03:00",
         "Planned maintenance window", "RESOLVED"],
    ], columns=["event_id", "system_name", "start_ts_utc", "end_ts_utc", "description", "status"])

    # ---------------- write ----------------
    customers.to_csv(out / "customers.csv", index=False)
    accounts.to_csv(out / "accounts.csv", index=False)
    pd.concat(stmt_rows).to_csv(out / "statements.csv", index=False)
    payments.to_csv(out / "payments.csv", index=False)
    pd.concat(fee_rows).to_csv(out / "fees.csv", index=False)
    pd.concat(waiver_rows).to_csv(out / "fee_adjustments.csv", index=False)
    pd.concat(txn_rows).sort_values(["account_id", "post_date"]).to_csv(
        out / "transactions.csv", index=False)
    system_events.to_csv(out / "system_events.csv", index=False)

    k = pd.concat(key_rows)
    k["wrongful_fee"] = np.round(k.fee_charged - k.fee_correct, 2)
    k["wrongful_interest"] = np.round(k.interest_charged - k.interest_correct, 2)
    k = k[(k.wrongful_fee > 0) | (k.wrongful_interest > 0)].copy()
    fee_type = np.select([(k.fee_correct == 0) & (k.wrongful_fee > 0), k.wrongful_fee > 0],
                         ["WRONGFUL_LATE_FEE", "FEE_TIER_OVERCHARGE"], "")
    int_type = np.select([(k.interest_correct == 0) & (k.wrongful_interest > 0),
                          k.wrongful_interest > 0],
                         ["LOST_GRACE_INTEREST", "POSTING_DELAY_INTEREST"], "")
    k["harm_type"] = [("|".join(x for x in (a, b) if x)) for a, b in zip(fee_type, int_type)]
    k["fee_refund"] = np.round(np.maximum(k.wrongful_fee - k.fee_waived_already, 0), 2)
    acct = k.groupby("account_id").agg(
        fee_refund=("fee_refund", "sum"), interest_refund=("wrongful_interest", "sum"),
        fee_waived_already=("fee_waived_already", "sum"),
        harm_types=("harm_type", lambda s: "|".join(sorted({y for x in s for y in x.split("|") if y}))),
    ).reset_index()
    acct = acct.merge(accounts[["account_id", "account_status"]], on="account_id")
    acct["remediation_total"] = np.round(acct.fee_refund + acct.interest_refund, 2)
    acct = acct[acct.remediation_total > 0]
    acct["remediation_method"] = np.where(acct.account_status == "CLOSED", "MAILED_CHECK",
                                          "STATEMENT_CREDIT")
    acct.to_csv(key_dir / "remediation_answer_key.csv", index=False)
    k.to_csv(key_dir / "remediation_detail_by_cycle.csv", index=False)
    print(f"accounts={n:,}  payments={len(payments):,}  remediated accounts={len(acct):,}  "
          f"total=${acct.remediation_total.sum():,.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    main(a.accounts, a.seed, a.out)
