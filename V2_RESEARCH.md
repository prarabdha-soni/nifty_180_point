# V2 Research — pre-registration

**Written and committed 2026-09-28, before any V2 variant was run.** The git
history of this file is the proof. The pre-registered section (everything
above "Results") is not edited after that commit. Results are appended below
it.

## Why

The owner asked to "improve, learn and fix" the model after the first paper
week (21–28 Sep 2026). That week had 2 decisions, 7 executions and 6 closed
trades, and every one of them lost money (−₹202,663). Spec 19 says not to
optimise before V1 sign-off, and the owner had earlier said not to tune. The
owner has now asked for this explicitly, so it is their decision. The
safeguards below are what make the result worth anything:

- **V1 stays the control.** Nothing in V1's defaults changes. Every V2 idea is
  an opt-in config switch whose default reproduces V1 exactly (the R1–R10
  pattern). V1 is verified byte-identical before and after.
- **Hypotheses come from failures we observed**, not a search over every knob.
- **The selection rule is fixed here, before any number exists.**
- **There is one look at an untouched holdout.** If the candidate fails there,
  nothing is promoted, and I do not go back and pick the runner-up.
- **A winner is forward-tested next to V1** on the paper page. It never
  replaces V1.

## Data: 3-year spot proxy

Only about 4 months of real per-contract futures exist (Angel One does not
serve expired contracts). The study therefore uses the cached 1-minute NIFTY 50
spot series (2023-09-20 onward) as a proxy:

- futures = spot on every bar (open, high, low, close); bars expanded to
  4 ticks per minute, both intra-bar orderings (`fetch_data.expand_ohlc`);
- no `futures_contract_expiry`, so no expiry square-off and no roll. Historical
  expiry dates shift with holidays and are not fabricated;
- consequences: the futures-confirmation leg is always met when the spot leg
  is (the proxy cannot reproduce entries that futures would have refused, as
  on 21 Sep); transaction costs are computed on spot notional; no basis P&L.
  All variants share these biases, so **comparisons between variants are more
  trustworthy than absolute numbers**.

**Calibration (pre-registered check):** run V1 on the proxy over the
CALIBRATION window and compare it with the real-futures in-sample runs over the
same 56 sessions (`results/insample_always`, `results/insample_always_favfirst`).
If the proxy's trade count differs by more than ±30%, or the sign of net P&L
differs, the owner is told before any variant result is presented.

## Windows

| window | dates | sessions | index range | 180 pts as % | use |
|---|---|---|---|---|---|
| TRAIN | 2023-09-21 … 2025-06-30 | 438 | 18,844–26,273 | 0.69–0.96% | consistency check (rule c) |
| VALIDATE | 2025-07-01 … 2025-12-31 | 126 | 24,341–26,311 | 0.68–0.74% | selection (rules a, b) |
| HOLDOUT-P | 2026-01-01 … 2026-05-29 | 99 | 22,186–26,371 | 0.68–0.81% | one final look |
| CALIBRATION | 2026-06-01 … 2026-08-18 | 56 | 23,080–24,774 | 0.73–0.78% | V1 only, proxy vs real |
| never touched | 2026-08-19 … 2026-09-18 | 22 | — | — | original futures holdout |

Each window runs as its own backtest. The session before the window is the
warm-up, and every window starts flat.

## Variants

| id | overrides | kind | promotable | tied to observed failure |
|---|---|---|---|---|
| V1 | — | control | — | — |
| H1_24k | `distance_scale_ref=24000` | grid (22k/24k/26k) | yes | fixed 180 pts = 0.68–0.96% of index over 3 yrs |
| H1_22k | `distance_scale_ref=22000` | grid neighbour | no | |
| H1_26k | `distance_scale_ref=26000` | grid neighbour | no | |
| H2 | `extreme_tracking=FLAT_ONLY` | categorical | yes | mechanical re-entry chains (R2) |
| H3 | `breaker_enabled=false` | categorical | yes | breaker's unstopped leg (−360 pts worst case) |
| H4_90 | `early_stop_distance=90, early_arm_distance=90` | grid (75/90/120/150) | yes | 4 of 6 paper stops fired at 09:15 |
| H4_120 | `early_stop_distance=120, early_arm_distance=120` | grid | yes | same |
| H4_75, H4_150 | S=A=75 / 150 | grid neighbours | no | |
| H5 | `gap_applies_when_flat=true` | categorical | yes | entries straight into gap mornings |
| S150, S210, S240 | `swing_distance=trail_distance=` 150/210/240 | sensitivity | **no** | is 180 on a plateau? |

`distance_scale_ref=R` multiplies D, S, A, P, T and the futures-confirm distance
by `prev_session_close / R` at each session start. At an index level of R it
is identical to V1, and away from R the distances scale with the index.

## Metrics (every run, both orderings)

Net ₹ after costs; max drawdown ₹; sum of per-trade points ÷ entry spot
(% terms); completed trades; decisions (fresh entries); win rate; profit
factor; worst sequence (net of one decision's whole chain); share of
profitable calendar quarters; net per calendar year.

## Selection rule (fixed)

A variant is **promoted to the holdout** only if all of these hold:

- **(a)** On VALIDATE, under **both** orderings: net(v) > net(V1) **and**
  maxDD(v) < maxDD(V1).
- **(b)** Grid hypotheses only: both grid neighbours also have net > net(V1) on
  VALIDATE under both orderings (a plateau, not a spike). Categorical
  hypotheses have no neighbour condition.
- **(c)** On TRAIN, under both orderings: net(v) ≥ net(V1). A variant that only
  wins on the shorter window is treated as noise.

**Combinations:** if two or more variants are promoted, test at most 2 pairwise
combinations of the top 2 (by VALIDATE net averaged over orderings). A
combination must satisfy (a) and (c), **and** beat each of its components on
VALIDATE net averaged over orderings.

**Final pick:** the promoted single or combination with the best VALIDATE net,
averaged over orderings. It gets one run on HOLDOUT-P. It **passes** if net >
V1 and maxDD ≤ V1 there under both orderings. If it fails, nothing is promoted
and the result is reported as it is.

**Sensitivity runs (S150/S210/S240)** are reported but never promoted.

**If nothing is promoted**, V1 remains the only model and the report says so.
That is a legitimate outcome.

## After a pass

The candidate is added to the paper page as an extra tab, starting from the
promotion date, next to V1. It is judged forward after about 3 months (about
25 trades), together with the real near-month futures history that has built
up by then. It is not traded, and V1 is not replaced, on the strength of this
study alone.

---

## Results

*(appended after the runs; the section above is not edited)*

### Run of 2026-09-28 (`v2_study.py`, 56 backtests + calibration)

**Calibration (pre-registered check): within tolerance.** V1 on the proxy vs
real futures over the same 55 sessions: trades 23 vs 22 (adverse-first),
23 vs 23 (favourable-first); both net positive. Bias: the proxy found 5 of 8
fresh entries, ~60% of entries at the same minute, and net ≈ ⅓ of real
(₹1.0 L vs ₹3.1 L) — no basis P&L, no futures-confirmation filter.

**Outcome: nothing promoted. V1 stays the only model. HOLDOUT-P not opened.**

| rule | H1_24k | H2 | H3 | H4_90 | H4_120 | H5 |
|---|---|---|---|---|---|---|
| (a) VALIDATE beats V1 net & DD, both orderings | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| (b) grid neighbours beat V1 | ✗ (H1_26k) | n/a | n/a | ✗ | ✗ | n/a |
| (c) TRAIN not worse than V1 | ✗ (fav) | ✗ | ✗ | ✓ | ✓ | ✓ |

With nothing promoted, the combination step did not apply (so the ambiguity in
"at most 2 pairwise combinations of the top 2" never had to be resolved; the
runner was committed with the strictest reading — one pair from different
families — before the run).

**Net ₹ (adverse-first / favourable-first):**

| variant | TRAIN (438 sessions) | VALIDATE (126) |
|---|---|---|
| V1 | −27.1 L / −42.4 L | −1.1 L / −0.9 L |
| H1_24k (% of index) | −20.3 L / −43.1 L | +2.7 L / +2.7 L |
| H2 (FLAT_ONLY) | −29.0 L / −43.3 L | −5.5 L / −4.8 L |
| H3 (no breaker) | −41.3 L / −70.8 L | −1.5 L / −3.0 L |
| H4 S=A=75 / 90 / 120 / 150 | −12.2 / −9.5 / −6.4 / +0.8 L (adv) | −0.2 / −0.5 / −6.6 / +0.7 L (adv) |
| H5 (gap filter when flat) | −24.6 L / −42.2 L | −1.3 L / −1.2 L |
| S150 / S210 / S240 (reference) | −30.6 / −40.0 / −29.3 L (adv) | +0.9 / −0.0 / +0.2 L (adv) |

Full table: `results/v2/summary.csv`; rule evaluation: `results/v2/selection.json`;
page: https://nifty-180-report.vercel.app/v2

### What was learned (post-hoc — these are leads, not results)

1. **V1 loses before costs over 2023-09 … 2025-06 on the proxy**: gross −₹18.7 L
   / −₹33.4 L, costs ₹8.5–9.0 L, profit factor 0.73 / 0.62 over 310 / 329
   trades; 2024 alone −₹21.6 L / −₹29.8 L. The positive June–August 2026
   in-sample was the exception. Even allowing for the proxy's bias, V1 as
   specified should not be traded.
2. **Where it loses (TRAIN, adverse-first):** trail exits +₹40.3 L (146);
   early stops −₹31.7 L (99); breaker deferred flips −₹20.6 L (20); adverse-gap
   exits −₹15.2 L (45). The trend-capture works; the protection machinery
   costs more than it saves.
3. **The 60-point early stop is inside the noise**: the TRAIN daily range
   exceeded 60 points on 97% of days (median 179). Widening S=A improved TRAIN
   at nearly every step (adv −27.1 → −12.2 → −9.5 → −6.4 → +0.8 L), but
   VALIDATE did not confirm (S=120 was the worst variant there).
4. **The breaker is protective**: removing it (H3) made losses far worse.
5. **Re-entry chains (R2) and gap-morning entries (R5) are not the problem.**
6. **% distances (H1)** helped on VALIDATE, not on TRAIN: not robust.

Any next study must be pre-registered afresh. The only untouched data left is
HOLDOUT-P (2026-01 … 2026-05) and the forward paper run; using HOLDOUT-P for a
new hypothesis formed from these results spends it.
