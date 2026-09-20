#!/usr/bin/env python3
"""
validate_data.py -- sanity report for a merged engine CSV before backtesting.

    python validate_data.py data/nifty_3y.csv
    python validate_data.py data/nifty_3y.csv --json data/nifty_3y.validation.json

Checks (README "Data contract" layout: timestamp,trade_date,spot_ltp,
futures_ltp,futures_contract_expiry):

  * schema, sort order, duplicate timestamps, trade_date == timestamp date
  * session count, date range, rows per session, short sessions
  * missing minutes per session against the 09:15..15:29 one-minute grid
  * suspicious ticks: zero / negative / NaN prices, and > 2 % moves between
    consecutive minutes inside a session (overnight gaps reported separately)
  * spot-vs-futures basis distribution, per contract, with robust outliers
  * rollovers: every change of futures_contract_expiry, the price gap and the
    basis jump at the roll, and whether the old contract was used past expiry
  * expiry sanity: every row's expiry >= its trade_date

Verdict: CLEAN (exit 0), WARNINGS (exit 0), FAIL (exit 2). Hard failures are
schema / ordering / duplicate / zero-price / expired-contract problems -- the
things that make an engine run meaningless rather than merely noisy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

REQUIRED = ["timestamp", "trade_date", "spot_ltp", "futures_ltp", "futures_contract_expiry"]


def fmt(v, nd=2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    return f"{v:,.{nd}f}"


class Report:
    def __init__(self):
        self.lines: List[str] = []
        self.data: Dict = {}
        self.warnings: List[str] = []
        self.failures: List[str] = []

    def h(self, title: str) -> None:
        self.lines += ["", title, "-" * len(title)]

    def p(self, s: str = "") -> None:
        self.lines.append(s)

    def warn(self, s: str) -> None:
        self.warnings.append(s)
        self.lines.append(f"  ! WARN  {s}")

    def fail(self, s: str) -> None:
        self.failures.append(s)
        self.lines.append(f"  X FAIL  {s}")


def load(path: str, rep: Report) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED if c not in df.columns]
    rep.h("Schema")
    rep.p(f"  columns: {list(df.columns)}")
    if missing:
        rep.fail(f"missing required columns: {missing}")
        raise SystemExit(2)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["futures_contract_expiry"] = pd.to_datetime(df["futures_contract_expiry"]).dt.date
    df["spot_ltp"] = pd.to_numeric(df["spot_ltp"], errors="coerce")
    df["futures_ltp"] = pd.to_numeric(df["futures_ltp"], errors="coerce")
    df["_minute"] = df["timestamp"].dt.floor("min")      # grid checks work per minute
    tpm = len(df) / max(df["_minute"].nunique(), 1)
    rep.data["ticks_per_minute"] = round(tpm, 2)
    rep.p(f"  rows: {len(df):,}   ({tpm:.2f} ticks per minute"
          + (" -- OHLC-expanded bars" if tpm > 1.5 else "") + ")")
    return df


def check_order_and_dupes(df: pd.DataFrame, rep: Report) -> None:
    rep.h("Ordering and duplicates")
    if not df["timestamp"].is_monotonic_increasing:
        rep.fail("timestamps are not sorted ascending")
    else:
        rep.p("  timestamps sorted: yes")
    dup = df[df["timestamp"].duplicated(keep=False)]
    rep.data["duplicate_timestamps"] = int(dup["timestamp"].nunique())
    if len(dup):
        rep.fail(f"{dup['timestamp'].nunique():,} duplicated timestamps "
                 f"({len(dup):,} rows); first: {dup['timestamp'].iloc[0]}")
    else:
        rep.p("  duplicate timestamps: none")
    mismatch = df[df["timestamp"].dt.date != df["trade_date"]]
    if len(mismatch):
        rep.fail(f"{len(mismatch):,} rows where trade_date != timestamp date")
    else:
        rep.p("  trade_date matches timestamp date: yes")
    wk = df[df["timestamp"].dt.weekday >= 5]
    if len(wk):
        rep.warn(f"{len(wk):,} rows fall on a weekend ({wk['trade_date'].nunique()} dates) "
                 "-- special sessions, or a timezone problem")


def check_sessions(df: pd.DataFrame, rep: Report, t0: dt.time, t1: dt.time) -> None:
    rep.h("Sessions")
    by = df.groupby("trade_date")
    counts = by.size()
    grid_minutes = int((dt.datetime.combine(dt.date.today(), t1)
                        - dt.datetime.combine(dt.date.today(), t0)).total_seconds() // 60) + 1
    rep.data.update({
        "sessions": int(len(counts)), "first_date": str(counts.index.min()),
        "last_date": str(counts.index.max()),
        "rows_per_session": {"min": int(counts.min()), "median": float(counts.median()),
                             "max": int(counts.max())},
        "expected_minutes_per_session": grid_minutes,
    })
    rep.p(f"  sessions: {len(counts):,}   range: {counts.index.min()} .. {counts.index.max()}")
    mins = df.groupby("trade_date")["_minute"].nunique()
    rep.p(f"  minutes/session: min {mins.min()}  median {mins.median():.0f}  max {mins.max()}"
          f"   (full grid {t0.strftime('%H:%M')}..{t1.strftime('%H:%M')} = {grid_minutes}; "
          f"rows/session median {counts.median():.0f})")
    cal_days = np.busday_count(counts.index.min(), counts.index.max() + dt.timedelta(days=1))
    rep.p(f"  weekdays in range: {cal_days}  -> {cal_days - len(counts)} weekdays with no "
          f"session (holidays or gaps)")

    times = df["_minute"].dt.time
    outside = df[(times < t0) | (times > t1)]
    rep.data["rows_outside_grid"] = int(len(outside))
    if len(outside):
        rep.warn(f"{len(outside):,} rows outside {t0.strftime('%H:%M')}..{t1.strftime('%H:%M')} "
                 f"(e.g. {outside['timestamp'].iloc[0]}) -- the engine will process them")

    # missing minutes per session
    inside = df[(times >= t0) & (times <= t1)]
    present = inside.groupby("trade_date")["_minute"].nunique()
    missing = (grid_minutes - present).clip(lower=0)
    rep.data["missing_minutes_total"] = int(missing.sum())
    rep.data["missing_minutes_by_session"] = {str(k): int(v) for k, v in missing.items() if v}
    buckets = {"0": int((missing == 0).sum()), "1-5": int(((missing >= 1) & (missing <= 5)).sum()),
               "6-30": int(((missing > 5) & (missing <= 30)).sum()),
               "31-100": int(((missing > 30) & (missing <= 100)).sum()),
               ">100": int((missing > 100).sum())}
    rep.p(f"  missing minutes (vs grid): total {int(missing.sum()):,} across "
          f"{int((missing > 0).sum())} sessions")
    rep.p("  sessions by missing-minute count: " + "  ".join(f"{k}: {v}" for k, v in buckets.items()))
    worst = missing.sort_values(ascending=False).head(10)
    worst = worst[worst > 0]
    if len(worst):
        rep.p("  worst sessions: " + ", ".join(f"{d} (-{m})" for d, m in worst.items()))
    short = missing[missing > grid_minutes * 0.5]
    if len(short):
        rep.warn(f"{len(short)} session(s) have < 50% of the grid: "
                 f"{', '.join(str(d) for d in short.index[:8])}"
                 f"{' ...' if len(short) > 8 else ''} -- half-days (Muhurat) or data gaps")
    frac_missing = missing.sum() / (grid_minutes * len(counts))
    if frac_missing > 0.02:
        rep.warn(f"{frac_missing:.1%} of grid minutes missing overall")
    # systematic holes: the same minute-of-day absent in many sessions is a
    # feed defect, not random dropout
    grid = [(dt.datetime.combine(dt.date(2000, 1, 1), t0) + dt.timedelta(minutes=i)).time()
            for i in range(grid_minutes)]
    present_by_day = inside.groupby("trade_date")["_minute"].apply(lambda x: set(x.dt.time))
    n_sessions = len(present_by_day)
    freq = {t: sum(1 for sset in present_by_day if t not in sset) for t in grid}
    systematic = {t: n for t, n in freq.items() if n >= max(3, 0.2 * n_sessions)}
    rep.data["systematic_missing_minutes"] = {t.strftime("%H:%M"): n for t, n in systematic.items()}
    if systematic:
        ts = sorted(systematic)
        rep.warn(f"systematic hole: {len(ts)} minute(s) of day ({ts[0].strftime('%H:%M')}.."
                 f"{ts[-1].strftime('%H:%M')}) missing in {max(systematic.values())}/{n_sessions} "
                 "sessions -- a feed defect; the engine sees no ticks there (no stops, no extremes, "
                 "prev_close taken from the last bar present)")
        first_bad = missing[missing > 0].index.min()
        rep.p(f"      first affected session: {first_bad}")


def check_prices(df: pd.DataFrame, rep: Report, jump_pct: float) -> None:
    rep.h("Suspicious ticks")
    for col in ("spot_ltp", "futures_ltp"):
        bad = df[df[col].isna() | (df[col] <= 0)]
        rep.data[f"{col}_zero_or_nan"] = int(len(bad))
        if len(bad):
            rep.fail(f"{col}: {len(bad):,} zero/negative/NaN rows (first {bad['timestamp'].iloc[0]})")
        else:
            rep.p(f"  {col}: no zero/negative/NaN")
    # intra-session jumps
    jumps_out = {}
    for col in ("spot_ltp", "futures_ltp"):
        pct = df.groupby("trade_date")[col].pct_change().abs()
        big = df[pct > jump_pct].assign(pct=pct[pct > jump_pct])
        jumps_out[col] = int(len(big))
        if len(big):
            rep.warn(f"{col}: {len(big):,} minute-to-minute moves > {jump_pct:.1%} "
                     f"(largest {big['pct'].max():.2%} at {big.loc[big['pct'].idxmax(), 'timestamp']})")
            for _, r in big.nlargest(5, "pct").iterrows():
                rep.p(f"      {r['timestamp']}  {col} {r[col]:,.2f}  move {r['pct']:.2%}")
        else:
            rep.p(f"  {col}: no minute-to-minute move > {jump_pct:.1%}")
    rep.data["intrasession_jumps"] = jumps_out
    # overnight gaps -- informational, they drive the spec-10 gap regime
    closes = df.groupby("trade_date")["spot_ltp"].last()
    opens = df.groupby("trade_date")["spot_ltp"].first()
    gap = (opens - closes.shift(1)) / closes.shift(1)
    gap = gap.dropna()
    rep.data["overnight_gaps"] = {"count_over_0.3pct": int((gap.abs() > 0.003).sum()),
                                  "count_over_2pct": int((gap.abs() > 0.02).sum()),
                                  "max_pct": float(gap.abs().max()) if len(gap) else None}
    if len(gap):
        rep.p(f"  overnight spot gaps: {int((gap.abs() > 0.003).sum())} over 0.30% (gap-regime "
              f"threshold), {int((gap.abs() > 0.02).sum())} over 2%, largest "
              f"{gap.abs().max():.2%} on {gap.abs().idxmax()}")


def check_basis(df: pd.DataFrame, rep: Report) -> None:
    rep.h("Basis (futures - spot)")
    b = df["futures_ltp"] - df["spot_ltp"]
    bp = b / df["spot_ltp"] * 100
    q = b.quantile([0.01, 0.05, 0.5, 0.95, 0.99])
    rep.data["basis_points"] = {"mean": float(b.mean()), "std": float(b.std()),
                                "min": float(b.min()), "p1": float(q[0.01]), "p5": float(q[0.05]),
                                "p50": float(q[0.5]), "p95": float(q[0.95]), "p99": float(q[0.99]),
                                "max": float(b.max())}
    rep.data["basis_pct"] = {"mean": float(bp.mean()), "p50": float(bp.median())}
    rep.p(f"  points: mean {b.mean():.2f}  std {b.std():.2f}  min {b.min():.2f}  "
          f"p1 {q[0.01]:.2f}  p5 {q[0.05]:.2f}  p50 {q[0.5]:.2f}  p95 {q[0.95]:.2f}  "
          f"p99 {q[0.99]:.2f}  max {b.max():.2f}")
    rep.p(f"  percent: mean {bp.mean():.3f}%  median {bp.median():.3f}%")
    neg = int((b < 0).sum())
    rep.data["basis_negative_rows"] = neg
    rep.p(f"  negative basis (backwardation) rows: {neg:,} ({neg / len(b):.2%})")
    if neg / len(b) > 0.10:
        rep.warn("more than 10% of rows in backwardation -- check the two columns are the same "
                 "instrument family and aligned on the same minute")
    # robust outliers: median +- 5 * MAD, computed per contract because the
    # basis shrinks towards each expiry
    flagged = []
    per_contract = {}
    for exp, g in df.groupby("futures_contract_expiry"):
        gb = g["futures_ltp"] - g["spot_ltp"]
        med = gb.median()
        mad = (gb - med).abs().median() * 1.4826 or gb.std() or 1.0
        z = (gb - med).abs() / mad
        out = g[z > 5].assign(basis=gb[z > 5], z=z[z > 5])
        per_contract[str(exp)] = {"rows": int(len(g)), "basis_median": float(med),
                                  "basis_mad": float(mad), "outliers": int(len(out)),
                                  "first": str(g['trade_date'].iloc[0]),
                                  "last": str(g['trade_date'].iloc[-1])}
        flagged.append(out)
    rep.data["basis_by_contract"] = per_contract
    rep.p("  per contract (expiry: sessions, median basis, robust sd, outliers):")
    for exp, s in per_contract.items():
        rep.p(f"      {exp}: {s['first']}..{s['last']}  median {s['basis_median']:7.2f}  "
              f"sd {s['basis_mad']:6.2f}  outliers {s['outliers']}")
    out = pd.concat(flagged) if flagged else pd.DataFrame()
    rep.data["basis_outliers"] = int(len(out))
    if len(out):
        rep.warn(f"{len(out):,} basis outliers (> 5 robust sd from the contract median); "
                 f"{len(out) / len(df):.2%} of rows")
        for _, r in out.nlargest(5, "z").iterrows():
            rep.p(f"      {r['timestamp']}  spot {r['spot_ltp']:,.2f}  fut {r['futures_ltp']:,.2f}"
                  f"  basis {r['basis']:.2f}  ({r['z']:.1f} sd)")
    else:
        rep.p("  outliers: none")
    # same-day deviation: the basis drifts over a contract's life, so compare
    # each row with its own session's median rather than the whole contract
    dev = b - b.groupby(df["trade_date"]).transform("median")
    thresh = 0.0025 * df["spot_ltp"]                       # 0.25% of the index (~60 pts at 24k)
    bad = df[dev.abs() > thresh].assign(dev=dev[dev.abs() > thresh])
    rep.data["basis_same_day_outliers"] = int(len(bad))
    rep.p(f"  deviation from same-session median: p1 {dev.quantile(0.01):.1f}  "
          f"p99 {dev.quantile(0.99):.1f}  min {dev.min():.1f}  max {dev.max():.1f}")
    if len(bad):
        tod = bad["timestamp"].dt.strftime("%H:%M").value_counts()
        rep.warn(f"{len(bad):,} row(s) deviate > 0.25% of the index from their session's median "
                 f"basis; by time of day: { {k: int(v) for k, v in tod.head(4).items()} } "
                 + ("(15:28/15:29 = the index's official-close print, a known artifact)"
                    if set(tod.index) <= {"15:28", "15:29"} else ""))
        for _, r in bad.reindex(bad["dev"].abs().sort_values(ascending=False).index).head(5).iterrows():
            rep.p(f"      {r['timestamp']}  spot {r['spot_ltp']:,.2f}  fut {r['futures_ltp']:,.2f}"
                  f"  deviation {r['dev']:+.1f}")


def check_rolls(df: pd.DataFrame, rep: Report) -> None:
    rep.h("Rollovers")
    exp = df["futures_contract_expiry"]
    change = exp != exp.shift(1)
    idx = list(np.flatnonzero(change.values))[1:]     # skip the first row
    rolls = []
    for i in idx:
        prev, cur = df.iloc[i - 1], df.iloc[i]
        b_prev = prev["futures_ltp"] - prev["spot_ltp"]
        b_cur = cur["futures_ltp"] - cur["spot_ltp"]
        rolls.append({
            "roll_date": str(cur["trade_date"]),
            "old_expiry": str(prev["futures_contract_expiry"]),
            "new_expiry": str(cur["futures_contract_expiry"]),
            "old_last_ts": str(prev["timestamp"]), "new_first_ts": str(cur["timestamp"]),
            "old_last_fut": float(prev["futures_ltp"]), "new_first_fut": float(cur["futures_ltp"]),
            "fut_gap_points": float(cur["futures_ltp"] - prev["futures_ltp"]),
            "fut_gap_pct": float((cur["futures_ltp"] - prev["futures_ltp"]) / prev["futures_ltp"]),
            "basis_jump_points": float(b_cur - b_prev),
            "old_used_through_expiry": bool(prev["trade_date"] == prev["futures_contract_expiry"]),
            "old_used_past_expiry": bool(prev["trade_date"] > prev["futures_contract_expiry"]),
        })
    rep.data["rollovers"] = rolls
    rep.p(f"  contracts in file: {exp.nunique()}   rolls detected: {len(rolls)}")
    if rolls:
        rep.p("  roll date     old expiry -> new expiry     fut gap (pts / %)   basis jump   old last row")
        for r in rolls:
            tag = ("on expiry day" if r["old_used_through_expiry"]
                   else "BEFORE expiry" if not r["old_used_past_expiry"] else "PAST EXPIRY")
            rep.p(f"  {r['roll_date']}   {r['old_expiry']} -> {r['new_expiry']}   "
                  f"{r['fut_gap_points']:8.2f} / {r['fut_gap_pct']:+.2%}   "
                  f"{r['basis_jump_points']:8.2f}   {tag}")
        early = [r for r in rolls if not r["old_used_through_expiry"] and not r["old_used_past_expiry"]]
        if early:
            rep.warn(f"{len(early)} roll(s) happened before the old contract's expiry day "
                     "(engine's expiry-day square-off will not fire for those)")
    past = df[df["trade_date"] > df["futures_contract_expiry"]]
    if len(past):
        rep.fail(f"{len(past):,} rows trade a contract PAST its expiry "
                 f"(first {past['timestamp'].iloc[0]}, expiry {past['futures_contract_expiry'].iloc[0]})")
    else:
        rep.p("  no row uses a contract past its expiry: ok")
    # expected monthly cadence
    if len(rolls):
        spans = [(pd.Timestamp(r["new_expiry"]) - pd.Timestamp(r["old_expiry"])).days for r in rolls]
        odd = [s for s in spans if not 25 <= s <= 36]
        if odd:
            rep.warn(f"{len(odd)} roll(s) with a non-monthly expiry gap (days: {odd}) -- "
                     "a skipped contract or a non-near-month contract")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a merged engine CSV")
    ap.add_argument("path")
    ap.add_argument("--json", help="write the machine-readable report here")
    ap.add_argument("--session-start", default="09:15")
    ap.add_argument("--session-end", default="15:29", help="bar-open time of the last bar")
    ap.add_argument("--jump-pct", type=float, default=0.02)
    args = ap.parse_args()
    t0 = dt.datetime.strptime(args.session_start, "%H:%M").time()
    t1 = dt.datetime.strptime(args.session_end, "%H:%M").time()

    rep = Report()
    rep.p(f"DATA VALIDATION  {args.path}")
    df = load(args.path, rep)
    check_order_and_dupes(df, rep)
    check_sessions(df, rep, t0, t1)
    check_prices(df, rep, args.jump_pct)
    check_basis(df, rep)
    check_rolls(df, rep)

    verdict = "FAIL" if rep.failures else ("WARNINGS" if rep.warnings else "CLEAN")
    rep.h("Verdict")
    rep.p(f"  {verdict}   ({len(rep.failures)} failure(s), {len(rep.warnings)} warning(s))")
    for f in rep.failures:
        rep.p(f"    FAIL  {f}")
    for w in rep.warnings:
        rep.p(f"    WARN  {w}")
    print("\n".join(rep.lines))
    rep.data["verdict"] = verdict
    rep.data["failures"] = rep.failures
    rep.data["warnings"] = rep.warnings
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rep.data, fh, indent=2, default=str)
    return 2 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
