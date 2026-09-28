# V3 Research — published strategy families on NIFTY (pre-registration)

**Written and committed 2026-09-29, before any candidate below was run on
NIFTY data.** The section above "Results" is not edited after that commit.

## Why

The V2 study found that V1 loses before costs over 2023-09 … 2025-06 (proxy)
and that none of its variants fixed it. The owner asked for "the most working
algo from top research that can work on NIFTY". This study takes the
strategy families with the strongest published evidence and asks one question
of each: **with its rules frozen as published and with today's Indian futures
costs, does it make money on NIFTY?**

Three facts shape the design:

1. **Published edges shrink.** McLean & Pontiff (J. Finance 2016): anomaly
   returns are about 58% lower after publication. The independent replication
   of Zarattini et al. (2024) confirms the edge historically, but finds a
   Sharpe of about 0 since 2025, and tuning made it worse.
2. **The papers are mostly pre-cost and non-Indian.** Baltussen et al. (JFE
   2021) cover 60+ futures markets but no Indian market, report pre-cost
   Sharpes, and say the strategy "might not be exploitable … after accounting
   for transaction costs".
3. **Indian futures costs rose sharply.** STT on the futures sell side was 0.01%,
   then 0.0125% from 2023-04, 0.02% from 2024-10, and **0.05% from 2026-04-01**.
   A strategy that trades daily now pays about 0.067% per round trip, which is
   roughly 17% of notional a year.

**No tuning.** Every candidate runs with its published parameters. Where NIFTY
data forces a deviation (index spot has no volume, so no VWAP), the deviation
is stated here in advance.

## Data

- **Intraday candidates:** the same 1-minute NIFTY 50 spot series as V2,
  used as a proxy for futures (futures = spot), from the committed cache.
  Exits use the **15:27 bar close** (price at 15:28), not the 15:29 bar,
  because the index's last bars carry the official-close print (known feed
  artifact, CLAUDE.md).
- **Daily candidate:** NIFTY 50 daily closes from Angel One (`ONE_DAY`),
  1995-07-12 onward.

## Candidates (rules frozen)

**C1 — Market intraday momentum** (Gao, Han, Li & Zhou, JFE 2018; Baltussen,
Da, Lammers & Martens, JFE 2021). Signal r_ROD = return from the previous
session's 15:28 price to today's 15:00 price. At 15:00 go long 1× if
r_ROD > 0, short if < 0. Exit at 15:28. One round trip per day.

**C2 — Noise-area intraday momentum** (Zarattini, Aziz & Barbon, SFI 2024).
- For minute *m* of the session, σ(m) = mean over the previous 14 sessions of
  |close(m) / open − 1|.
- Bands: UB(m) = max(open, prevClose)·(1+σ(m)) and
  LB(m) = min(open, prevClose)·(1−σ(m)).
- Decisions every 30 minutes, at 09:45, 10:15, … 15:15. Hold +1 if close > UB,
  −1 if close < LB, else flat. Flat at 15:28.
- *Deviations:* no VWAP (the index has no volume), so the band is the trailing
  stop. 1× notional, not the paper's 2%/day volatility sizing, so the cost
  arithmetic stays comparable across candidates.

**C3 — Time-series momentum** (Moskowitz, Ooi & Pedersen, JFE 2012; Hurst, Ooi
& Pedersen). At each month-end, the position for the next month is
sign(12-month return), long or short 1×. Futures must roll monthly, so one
round trip is charged every month a position is held, plus one per flip.
*Reference only, not promotable:* the long/flat version (flat instead of short).

**C4 — Overnight drift** (Lou, Polk & Skouras 2019; documented for NIFTY; the
Kamath 2026 SSRN paper asks whether it survives Indian frictions). Long 1×
from 15:28 to the next session's 09:15 open. One round trip per day.

**Benchmarks (reported, never promoted):** buy & hold 1× with monthly roll
costs, and V1 on the proxy re-costed with `stt_sell_side_pct` at the
applicable rate.

## Costs (per round trip, % of notional)

STT on the sell leg at the schedule above, plus 0.017% for everything else:
exchange and SEBI charges, 0.002% stamp duty, brokerage of ₹20/order plus GST
on a one-lot notional, and 1 index point of slippage per side. Results are
reported under two cost schedules:
- **historical:** the STT rate in force on each trade date;
- **today:** 0.05% STT applied throughout, i.e. "would it work if started now".
  **Pass/fail is judged on this one.**

## Windows

| candidate | develop / check | confirm | one look |
|---|---|---|---|
| C1, C2, C4 | TRAIN 2023-09-21 … 2025-06-30 | VALIDATE 2025-07-01 … 2025-12-31 | HOLDOUT-P 2026-01-01 … 2026-05-29 |
| C3 | 1995-07 … 2010-12 | 2011-01 … 2025-12 | 2026-01 … 2026-05 (report only; too short to judge) |

TRAIN and VALIDATE were used by V2 for V1 variants, but none of these
candidates has been run on them. 2026-08-19 … 2026-09-18 stays untouched.

## Metrics

Annualised Sharpe of daily net returns (0 on days with no position; √252),
total net return, max drawdown, trades, hit rate, and **average gross return
per trade vs the round-trip cost**. That last one is the simplest test of
whether an edge can pay for itself.

## Pass rule (fixed)

- **C1, C2, C4:** net Sharpe ≥ 0.5 at today's costs on **both** TRAIN and
  VALIDATE, **and** average gross return per trade ≥ 1.5 × today's
  round-trip cost on TRAIN. A pass earns one HOLDOUT-P run, which passes if
  net Sharpe > 0 at today's costs.
- **C3:** net Sharpe ≥ 0.3 at today's costs in **both** halves, **and** a
  smaller max drawdown than buy & hold over 1995-07 … 2025-12.
- A candidate that passes is forward-tested as a paper tab next to V1. It is
  not traded on the strength of this study alone.
- If nothing passes, the report says so and names what came closest, and why.

---

## Results

*(appended after the runs; the section above is not edited)*
