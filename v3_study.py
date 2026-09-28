#!/usr/bin/env python3
"""
v3_study.py -- run the pre-registered V3 candidates (V3_RESEARCH.md) on NIFTY.

    python v3_study.py                    # TRAIN/VALIDATE (intraday), both halves (C3), pass rule
    python v3_study.py --holdout C1       # the ONE HOLDOUT-P look for a candidate that passed

Rules are coded exactly as registered; nothing is tuned. Intraday candidates use
the cached 1-minute spot bars (proxy: futures = spot); C3 uses Angel One daily
closes from 1995. Returns are per unit of notional (1x), costs per round trip.
Outputs: results/v3/summary.csv, results/v3/daily_<cand>.csv, results/v3/selection.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data as fd          # noqa: E402
import make_proxy_data as mpd    # noqa: E402

OUT = os.path.join(HERE, "results", "v3")
OTHER_COSTS = 0.00017            # per round trip, everything except STT (pre-registered)
WINDOWS = {"TRAIN": ("2023-09-21", "2025-06-30"), "VALIDATE": ("2025-07-01", "2025-12-31"),
           "HOLDOUT-P": ("2026-01-01", "2026-05-29")}
C3_HALVES = {"FIRST": ("1995-07-01", "2010-12-31"), "SECOND": ("2011-01-01", "2025-12-31"),
             "HOLDOUT-P": ("2026-01-01", "2026-05-29")}
EXIT_BAR = "15:27"               # close of this bar = price at 15:28 (avoids the official-close print)


def stt(d: dt.date) -> float:
    if d < dt.date(2023, 4, 1):
        return 0.0001
    if d < dt.date(2024, 10, 1):
        return 0.000125
    if d < dt.date(2026, 4, 1):
        return 0.0002
    return 0.0005


def rt_cost(d: dt.date, schedule: str) -> float:
    return (0.0005 if schedule == "today" else stt(d)) + OTHER_COSTS


# --- intraday data ------------------------------------------------------------------------------
def load_days() -> Dict[dt.date, pd.DataFrame]:
    """{date: bars indexed by 'HH:MM' bar-open time, columns open/high/low/close}"""
    import logging
    logging.getLogger("fetch_data").setLevel(logging.ERROR)
    spot = mpd.load_spot(os.path.join(HERE, "data", "raw", "angel"))
    spot["d"] = spot["timestamp"].dt.date
    spot["hm"] = spot["timestamp"].dt.strftime("%H:%M")
    return {d: g.set_index("hm")[["open", "high", "low", "close"]] for d, g in spot.groupby("d")}


def px(bars: pd.DataFrame, hhmm: str, back: int = 5) -> Optional[float]:
    """Close of bar `hhmm`, else the nearest earlier bar within `back` minutes."""
    t = dt.datetime.strptime(hhmm, "%H:%M")
    for k in range(back + 1):
        key = (t - dt.timedelta(minutes=k)).strftime("%H:%M")
        if key in bars.index:
            return float(bars.at[key, "close"])
    return None


def before(hhmm: str, minutes: int = 1) -> str:
    return (dt.datetime.strptime(hhmm, "%H:%M") - dt.timedelta(minutes=minutes)).strftime("%H:%M")


# --- candidates -----------------------------------------------------------------------------------
def c1(days, dates) -> pd.DataFrame:
    rows = []
    for p, d in zip(dates[:-1], dates[1:]):
        prev, p15, pex = px(days[p], EXIT_BAR), px(days[d], before("15:00")), px(days[d], EXIT_BAR)
        if None in (prev, p15, pex):
            continue
        r_rod = p15 / prev - 1
        if r_rod == 0:
            continue
        pos = 1 if r_rod > 0 else -1
        rows.append({"date": d, "gross": pos * (pex / p15 - 1), "turnover": 2, "trades": 1})
    return pd.DataFrame(rows)


def c2(days, dates) -> pd.DataFrame:
    grid = pd.date_range("2000-01-01 09:15", "2000-01-01 15:27", freq="1min").strftime("%H:%M")
    move = {}
    for d in dates:
        b = days[d]
        o = float(b["open"].iloc[0]) if "09:15" not in b.index else float(b.at["09:15", "open"])
        move[d] = (b["close"] / o - 1).abs().reindex(grid)
    checks = [(dt.datetime(2000, 1, 1, 9, 45) + dt.timedelta(minutes=30 * k)).strftime("%H:%M") for k in range(12)]
    rows = []
    for i in range(14, len(dates)):
        d, p = dates[i], dates[i - 1]
        b = days[d]
        if "09:15" not in b.index:
            continue
        sig = pd.concat([move[x] for x in dates[i - 14:i]], axis=1)
        sigma = sig.mean(axis=1, skipna=True).where(sig.notna().sum(axis=1) >= 10)
        o, pc = float(b.at["09:15", "open"]), px(days[p], EXIT_BAR)
        if pc is None:
            continue
        up, lo = max(o, pc), min(o, pc)
        pos, gross, turn, trades, last = 0, 0.0, 0, 0, None
        for c in checks:
            bar = before(c)
            price = px(b, bar, back=3)
            s = sigma.get(bar)
            if price is None or s is None or np.isnan(s):
                continue
            if pos and last:
                gross += pos * (price / last - 1)
            new = 1 if price > up * (1 + s) else (-1 if price < lo * (1 - s) else 0)
            if new != pos:
                turn += abs(new - pos)
                if new:
                    trades += 1
            pos, last = new, price
        pex = px(b, EXIT_BAR)
        if pos and last and pex:
            gross += pos * (pex / last - 1)
            turn += abs(pos)
        if trades:
            rows.append({"date": d, "gross": gross, "turnover": turn, "trades": trades})
    return pd.DataFrame(rows)


def c4(days, dates) -> pd.DataFrame:
    rows = []
    for d, n in zip(dates[:-1], dates[1:]):
        entry = px(days[d], EXIT_BAR)
        if entry is None or "09:15" not in days[n].index:
            continue
        rows.append({"date": d, "gross": float(days[n].at["09:15", "open"]) / entry - 1, "turnover": 2, "trades": 1})
    return pd.DataFrame(rows)


# --- C3 daily ------------------------------------------------------------------------------------
def load_daily() -> pd.Series:
    path = os.path.join(HERE, "data", "nifty_daily_1995.csv")
    if not os.path.exists(path):
        cl = fd.AngelClient(fd.Credentials.from_env(os.path.join(HERE, ".env")),
                            session_cache=os.path.join(HERE, "data", "raw", "angel_session.json"))
        cl.login()
        rows, a = [], dt.date(1995, 1, 1)
        while a <= dt.date(2026, 5, 29):
            b = min(a + dt.timedelta(days=1800), dt.date(2026, 5, 29))
            cl._space()
            r = cl._api.getCandleData({"exchange": "NSE", "symboltoken": "99926000", "interval": "ONE_DAY",
                                       "fromdate": f"{a} 09:15", "todate": f"{b} 15:30"})
            rows += (r or {}).get("data") or []
            a = b + dt.timedelta(days=1)
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["timestamp"].str[:10])
        df.drop_duplicates("date").sort_values("date")[["date", "close"]].to_csv(path, index=False)
    s = pd.read_csv(path, parse_dates=["date"]).set_index("date")["close"]
    return s[s.index <= "2026-05-29"]


def c3(close: pd.Series, long_flat: bool = False) -> pd.DataFrame:
    """Daily rows with the month's position; costs charged on each month's first day."""
    me = close.groupby(close.index.to_period("M")).tail(1)             # month-end closes
    sig = np.sign(me / me.shift(12) - 1).fillna(0)
    if long_flat:
        sig = sig.clip(lower=0)
    pos_by_month = pd.Series(sig.values, index=me.index.to_period("M") + 1)   # applies next month
    ret = close.pct_change().fillna(0)
    per = close.index.to_period("M")
    pos = pd.Series(per.map(pos_by_month).fillna(0).values, index=close.index)
    first = ~per.duplicated()
    prev = pos.where(first).shift(1).ffill().fillna(0)                 # previous month's position
    turn = ((pos.abs() + prev.abs()) * first).where(first, 0)         # roll or flip = 2 sides/unit
    return pd.DataFrame({"date": close.index.date, "gross": (pos * ret).values,
                         "turnover": turn.values, "trades": ((pos != prev) & first & (pos != 0)).astype(int).values})


def buy_hold(close: pd.Series) -> pd.DataFrame:
    ret = close.pct_change().fillna(0)
    first = ~close.index.to_period("M").duplicated()
    return pd.DataFrame({"date": close.index.date, "gross": ret.values, "turnover": first.astype(int) * 2, "trades": 0})


# --- metrics ---------------------------------------------------------------------------------------
def with_costs(df: pd.DataFrame, schedule: str) -> pd.Series:
    return df["gross"] - df["turnover"] / 2 * df["date"].map(lambda d: rt_cost(d, schedule))


def metrics(df: pd.DataFrame, a: str, b: str, all_days: List[dt.date], schedule: str) -> dict:
    a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    sub = df[(df["date"] >= a) & (df["date"] <= b)]
    days = [d for d in all_days if a <= d <= b]
    net = pd.Series(0.0, index=days)
    if len(sub):
        net.loc[sub["date"].values] = with_costs(sub, schedule).values
    eq = (1 + net).cumprod()
    trades = int(sub["trades"].sum()) if len(sub) else 0
    rt_today = 0.0005 + OTHER_COSTS
    active = sub[sub["turnover"] > 0] if len(sub) else sub
    return {"days": len(days), "trades": trades,
            "sharpe": float(net.mean() / net.std() * np.sqrt(252)) if net.std() > 0 else 0.0,
            "total_return": float(eq.iloc[-1] - 1) if len(eq) else 0.0,
            "max_dd": float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0,
            "hit_rate": float((with_costs(sub, schedule) > 0).mean()) if len(sub) else None,
            "gross_per_trade": float(sub["gross"].sum() / trades) if trades else None,
            "gross_per_trade_over_cost": float(sub["gross"].sum() / trades / rt_today) if trades else None,
            "gross_total": float(sub["gross"].sum()) if len(sub) else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", help="candidate id for its single HOLDOUT-P look (C1/C2/C4/C3)")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    days = load_days()
    dates = sorted(days)
    close = load_daily()
    trading_days = [d.date() for d in close.index]
    runs = {"C1": c1(days, dates), "C2": c2(days, dates), "C4": c4(days, dates)}
    daily = {"C3": c3(close), "C3_longflat": c3(close, long_flat=True), "BUYHOLD": buy_hold(close)}
    if not args.holdout:
        # the holdout months are neither saved nor measured unless --holdout is given
        cut = dt.date(2026, 1, 1)
        runs = {k: v[v["date"] < cut] for k, v in runs.items()}
        daily = {k: v[v["date"] < cut] for k, v in daily.items()}
        for k, v in {**runs, **daily}.items():
            v.to_csv(os.path.join(OUT, f"daily_{k}.csv"), index=False)

    if args.holdout:
        return holdout(args.holdout, runs, daily, dates, trading_days)

    rows = []
    for cid, df in runs.items():
        for w in ("TRAIN", "VALIDATE"):
            for sch in ("today", "historical"):
                rows.append({"cand": cid, "window": w, "schedule": sch, **metrics(df, *WINDOWS[w], dates, sch)})
    for w in ("TRAIN", "VALIDATE"):          # buy & hold on the intraday windows, for scale
        for sch in ("today", "historical"):
            rows.append({"cand": "BUYHOLD", "window": w, "schedule": sch,
                         **metrics(daily["BUYHOLD"], *WINDOWS[w], trading_days, sch)})
    for cid in ("C3", "C3_longflat", "BUYHOLD"):
        for h in ("FIRST", "SECOND"):
            for sch in ("today", "historical"):
                rows.append({"cand": cid, "window": h, "schedule": sch, **metrics(daily[cid], *C3_HALVES[h], trading_days, sch)})
        for sch in ("today", "historical"):
            rows.append({"cand": cid, "window": "FULL", "schedule": sch,
                         **metrics(daily[cid], "1995-07-01", "2025-12-31", trading_days, sch)})
    s = pd.DataFrame(rows)
    s.to_csv(os.path.join(OUT, "summary.csv"), index=False)

    pd.set_option("display.width", 220)
    show = ["cand", "window", "schedule", "days", "trades", "sharpe", "total_return", "max_dd", "hit_rate",
            "gross_per_trade", "gross_per_trade_over_cost"]
    print(s[s.schedule == "today"][show].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # --- pass rule, verbatim from V3_RESEARCH.md --------------------------------------------
    g = lambda c, w, k: float(s[(s.cand == c) & (s.window == w) & (s.schedule == "today")].iloc[0][k] or 0)
    sel = {}
    for cid in ("C1", "C2", "C4"):
        a = g(cid, "TRAIN", "sharpe") >= 0.5 and g(cid, "VALIDATE", "sharpe") >= 0.5
        b = g(cid, "TRAIN", "gross_per_trade_over_cost") >= 1.5
        sel[cid] = {"sharpe_both_windows>=0.5": a, "gross_per_trade>=1.5x_cost": b, "pass": a and b,
                    "train_sharpe": g(cid, "TRAIN", "sharpe"), "validate_sharpe": g(cid, "VALIDATE", "sharpe"),
                    "gross_over_cost": g(cid, "TRAIN", "gross_per_trade_over_cost")}
    a = g("C3", "FIRST", "sharpe") >= 0.3 and g("C3", "SECOND", "sharpe") >= 0.3
    b = g("C3", "FULL", "max_dd") > g("BUYHOLD", "FULL", "max_dd")          # less negative = smaller DD
    sel["C3"] = {"sharpe_both_halves>=0.3": a, "smaller_dd_than_buyhold": b, "pass": a and b,
                 "first_sharpe": g("C3", "FIRST", "sharpe"), "second_sharpe": g("C3", "SECOND", "sharpe"),
                 "full_dd": g("C3", "FULL", "max_dd"), "buyhold_full_dd": g("BUYHOLD", "FULL", "max_dd")}
    json.dump(sel, open(os.path.join(OUT, "selection.json"), "w"), indent=2)
    print("\n=== pass rule (V3_RESEARCH.md), at today's costs ===")
    for k, v in sel.items():
        print(f"  {k}: {'PASS' if v['pass'] else 'fail'}   " + ", ".join(f"{a}={b:.3f}" if isinstance(b, float) else f"{a}={b}"
                                                             for a, b in v.items() if a != "pass"))
    return 0


def holdout(cid, runs, daily, dates, trading_days) -> int:
    marker = os.path.join(OUT, f"holdout_{cid}.json")
    if os.path.exists(marker):
        raise SystemExit(f"{marker} exists -- the holdout is looked at once")
    if cid.startswith("C3"):
        m = metrics(daily[cid], *C3_HALVES["HOLDOUT-P"], trading_days, "today")
    else:
        m = metrics(runs[cid], *WINDOWS["HOLDOUT-P"], dates, "today")
    m["pass"] = m["sharpe"] > 0
    json.dump(m, open(marker, "w"), indent=2)
    print(json.dumps(m, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
