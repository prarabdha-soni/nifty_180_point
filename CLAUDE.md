# CLAUDE.md

## What this is

An event-driven backtester for the *NIFTY 180-Point Swing Strategy, Backtest
Specification V1*. NIFTY **spot** drives the signal and trade-management state
machine; NIFTY **futures** independently confirms entries and is the instrument
that is actually "traded" for fills, transaction costs and P&L.

**V1 is the control model. It is complete and tested. Do not rewrite,
refactor, "simplify" or reinterpret strategy logic.** Every strategy constant
lives in `strategy/config.py`; every ambiguity in the spec is resolved by an
explicit config switch and recorded in `DECISIONS.md`. Read `README.md` and
`DECISIONS.md` before touching anything.

Strategy parameters (do not change for the V1 baseline): D=180 swing distance,
S=60 early stop, A=60 arm distance, P=140 partial, T=180 trail, q=0.50 partial
fraction, B=2 breaker limit, G=0.30% gap threshold, cost 0.015%/side,
qty 65 × 6 lots = 390.

## Module layout

```
strategy/
    config.py            all constants + resolution switches (the only place to change behaviour)
    state.py             spec 4 state model
    signals.py           spec 5 reference extremes (running_high/low) and entry triggers
    pending_manager.py   spec 5.4 pending slot + spec 11 overnight carry anchors
    position_manager.py  spec 6,7,8 open / partial / close / reverse
    breaker.py           spec 9 double-early-stop breaker + deferred flip
    gap_manager.py       spec 10 morning gap regime
    execution.py         spec 12.1 fill pricing and per-execution cost
    pnl.py               spec 12.2, 15.1 trade ledger and reconciliation
    metrics.py           spec 15.2 metrics, attribution and diagnostics
    data.py              CSV loading, deterministic merge (R8), session references
    backtester.py        spec 13 per-tick order, spec 14 session loop — DO NOT reorder the tick handler
tests/
    harness.py               synthetic sessions with fixed reference levels (see below)
    test_entries.py          scenarios 1-3 + entry edges
    test_exits.py            scenarios 4-7 + the R2 three-mode comparison
    test_breaker.py          scenarios 8-10 + R1/R3
    test_gaps.py             spec 10 + R5/R6
    test_tick_precedence.py  determinism, R8, sessions, expiry, R7, costs
run_backtest.py          CLI: --merged | --spot/--futures, --references, --config, --out
make_sample_data.py      synthetic tick generator (NOT market data)
validate_v1.py           spec 16 acceptance report, 10 mandatory scenarios
data/references_template.csv   layout for --references

# real-data tooling (added 2026-09-20; none of it touches strategy/)
fetch_data.py            Angel One SmartAPI -> merged CSV (spot + near-month futures, 1-min, OHLC-expanded by default)
validate_data.py         data-quality report on a merged CSV; run BEFORE any backtest
split_data.py            in-sample / holdout split by date (holdout written read-only)
compare_runs.py          side-by-side metrics for N result dirs + points-vs-%-of-index tables
make_report.py           self-contained HTML report (overview, equity, ledger, per-trade mechanics drawn
                         from the engine's replayed state); LABEL=dir[@data.csv] per run; no external libs
.env.example             credential keys for fetch_data.py (copy to .env; .env is gitignored)
requirements-fetch.txt   extra deps for fetch_data.py only
overrides/               JSON config overrides for the R2 / R6 comparison runs
scripts/scheduled_fetch.sh      weekday-evening fetch + rebuild + validate (never splits/backtests)
scripts/install_schedule.sh     installs it as launchd agent com.nifty180.fetch (--run-now, --remove)
scripts/com.nifty180.fetch.plist  launchd template (__PROJECT__ substituted by the installer)
scripts/deploy_report.sh   regenerate results/report.html and deploy it to Vercel (static, no password)
deploy/                    Vercel project dir (vercel.json; index.html and .vercel/ are generated, gitignored)
```

Note: the top-level `backtester.py` and `config.py` are byte-identical stray
copies of `strategy/backtester.py` and `strategy/config.py`. Nothing imports
them (all imports go through the `strategy.` package). Edit the ones under
`strategy/` if anything ever needs editing. `nifty_v1_engine.tar.gz` is the
original archive the tree was extracted from.

Test harness reference levels (used throughout `tests/` and in DECISIONS.md):
prev_spot_high 24500, prev_spot_low 24200, prev_spot_close 24350, so
spot_short_trigger = 24320 and spot_long_trigger = 24380. Futures = spot + 20.

## Running

Environment: a venv at `.venv/` (Python 3.9.6 — the only interpreter on this
machine; the code runs fine on it). Activate before running anything, since
plain `python` is not on PATH outside the venv:

```bash
source .venv/bin/activate
```

Prove the engine reproduces the spec (expected: 78 passed, 10/10):

```bash
python -m pytest tests/ -q
python validate_v1.py
```

End-to-end on synthetic data:

```bash
python make_sample_data.py --days 60 --out data/sample_ticks.csv
python run_backtest.py --merged data/sample_ticks.csv --out results/
```

The run must print `Reconciliation (spec 18): PASS` and exit 0; it exits
non-zero if trade-level gross/cost/net/quantity do not reconcile.

Real data:

```bash
python run_backtest.py --merged data/nifty_ticks.csv --out results/
python run_backtest.py --spot data/spot.csv --futures data/fut.csv --out results/
python run_backtest.py --merged data/ticks.csv --config overrides.json   # JSON overrides any Config field
```

Outputs in `results/`: `trades.csv`, `executions.csv`, `events.csv`,
`metrics.json`, `config.json` (archive `config.json` with every ledger).

## Real-data workflow

```bash
cp .env.example .env && $EDITOR .env          # ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET
pip install -r requirements-fetch.txt
python fetch_data.py --out data/nifty_3y.csv             # 3y by default; --offline rebuilds from cache
python validate_data.py data/nifty_3y.csv                # verdict CLEAN / WARNINGS / FAIL(exit 2)
python split_data.py data/nifty_3y.csv --holdout-months 12
python run_backtest.py --merged data/nifty_3y_insample.csv --out results/insample_always/
python run_backtest.py --merged data/nifty_3y_insample.csv --config overrides/flat_only.json --out results/insample_flat_only/
python compare_runs.py ALWAYS=results/insample_always FLAT_ONLY=results/insample_flat_only --spot data/nifty_spot_1min.csv
python make_report.py --data data/nifty_3y_insample.csv ALWAYS=results/insample_always FLAT_ONLY=results/insample_flat_only \
    "ALWAYS (favourable-first)=results/insample_always_favfirst@data/nifty_3y_favfirst_insample.csv" --out results/report.html
```
**Dashboard (deployed 2026-09-20):** https://nifty-180-report.vercel.app — the
report as a static page on Vercel, project `nifty-180-report` under the
owner's account, deployed with `scripts/deploy_report.sh` (reads only
`VERCEL_TOKEN` from `.env`). Owner chose NO password: the URL is unlisted and
`noindex`, but anyone holding it can see the trades. Nothing else runs on
Vercel — fetch and backtests stay on the Mac; redeploy after re-running.

`make_report.py` re-runs each result dir's `config.json` on its data file through a
`Backtester` subclass that records state after every `on_spot_tick` (nothing in
`strategy/` is touched) and refuses the state track if the replay does not
reproduce the saved ledger — so a run must be paired with the exact file it was
made on (`@data.csv`).

**Angel One limitation (decided 2026-09-20):** SmartAPI's historical API only
serves LIVE F&O contracts (Angel One admin, SmartAPI forum). Expired NIFTY
futures are not stored and the scrip master has no tokens for them. So
`fetch_data.py` gets the full spot range but futures only for the ~3 listed
contracts: strict near-month coverage is a few weeks; `--nearest-available`
extends it to ~3 months by using the nearest listed contract (with its real
expiry) for dates whose true near month has expired. Owner chose "Angel One
only, partial futures" over Upstox's paid expired-instruments API or a vendor
file. The full spot series is saved to `data/nifty_spot_1min.csv` so futures
from another source can be aligned later without re-fetching.

**Roll convention (decided 2026-09-20):** the expiring contract is used THROUGH
its expiry day; the next contract starts the following session. The engine's
15:00 expiry square-off fires only when `futures_contract_expiry == trade_date`,
so rolling ON expiry day would silently disable it. `validate_data.py` flags
any roll that happens before expiry day.

**Data conventions (ADOPTED 2026-09-20): OHLC-expanded bars are the standard.**
`fetch_data.py` emits 4 ticks per 1-min bar by default (open, low/high,
high/low, close at :00/:15/:30/:45; `--no-expand-ohlc` for close-only,
reference only). Reason: close-only bars flipped the sign of the in-sample
result (-290k close-only vs +307k / +223k OHLC adverse-first / favourable-
first on the same 56 sessions) because the engine never saw the opening print
that the gap logic and morning stops key off; the two OHLC orderings agree on
every morning-driven number. Always report both `--ohlc-order` variants as a
band — the gap is the intra-bar path uncertainty only tick data removes.
Session grid 09:15..15:29; minutes missing on either side are dropped, never
forward-filled, and counted in `data/fetch_report*.json`.

Files: `data/nifty_3y.csv` (canonical, adverse-first), `data/nifty_3y_favfirst.csv`
(other bound), `data/nifty_3y_closeonly.csv` (reference only) — each with
`_insample` / `_holdout` splits (holdouts 0444, never run). Results:
`results/insample_{always,flat_only,r6_carry,always_favfirst}/` on the OHLC
in-sample; `results/*_closeonly/` kept only to show the artefact.

**Scheduled fetch:** `scripts/scheduled_fetch.sh` runs every weekday 18:30 via
launchd (`scripts/install_schedule.sh`; log `data/raw/scheduled_fetch.log`).
Daily rather than weekly because Angel One drops a contract from its API the
day after expiry; expired contracts are recovered from `data/raw/angel/` so
history accumulates (fixed `--start 2023-09-20`). Cache blocks are anchored to
a fixed 25-day calendar grid so any date range reuses them. macOS caveat: a
launchd job cannot read `~/Downloads` (TCC) — the agent exits 126 until either
`/bin/bash` gets Full Disk Access or the project is moved out of Downloads and
the installer re-run. The job never splits or backtests.

**Data status (fetched 2026-09-20, `--nearest-available`):**
`data/nifty_3y.csv` = 28,784 rows, 78 sessions, 2026-06-01..2026-09-18, one
contract (NIFTY29SEP26FUT). Rows before 2026-08-26 are NEAREST_AVAILABLE (the
Sep contract while it was still a back month; its basis behaves differently).
`data/nifty_spot_1min.csv` = full 3y spot, 2023-09-20..2026-09-18.
Known feed defects (both from Angel One, both reproducible on re-request):
(1) the NIFTY 50 index has no 1-min bars 15:15..15:27 (sometimes ..15:29) on
34/78 sessions from 2026-08-03 onward -- futures are complete there, so those
minutes are dropped; (2) the index's 15:28/15:29 bars are its official-close
print and can sit 60-200 pts off the futures. `validate_data.py` warns on
both. A 12-month holdout leaves NO in-sample data (`split_data.py` exits 3);
owner chose a 1-month holdout for a preliminary look (2026-09-20):
in-sample = 56 sessions 2026-06-01..2026-08-18 (`data/nifty_3y_insample.csv`),
holdout = 22 sessions 2026-08-19..2026-09-18 (read-only, NEVER RUN).
Preliminary in-sample runs (all reconcile): `results/insample_always/`,
`results/insample_flat_only/` (R2), `results/insample_r6_carry/` (R6). 27
trades is not evidence of anything; every in-sample futures row is
NEAREST_AVAILABLE (a back-month contract).

**Holdout discipline:** `data/nifty_3y_holdout.csv` is written mode 0444 and
must not be backtested until the in-sample ledger is signed off. Do not tune
parameters on it.

## The three MATERIAL decisions (DECISIONS.md R1, R2, R6)

These are places where the spec had two readings and the two readings produce
materially different ledgers. Each has a config switch. Do not "fix" them —
they are deliberate, tested, and must be signed off by a human before V2.

### R1 — Breaker reset condition (`breaker_reset_on_reversal_flat`, default False)

Spec 9 resets `consecutive_early_stops` whenever the position "becomes flat",
but spec 8.2 models every early-stop reversal as close-then-reopen, so the
position momentarily passes through FLAT on *every* early stop. Read literally,
the counter resets every time it increments and the whole breaker (section 9)
is dead code. V1 does NOT reset on the momentary flat inside a reversal; it
resets on MFE ≥ A, a trail exit, or going flat without an immediate reversal.
`breaker_reset_on_reversal_flat=True` reproduces the literal, breaker-disabled
reading (`test_literal_reset_reading_makes_the_breaker_dead_code`).

### R2 — Do reference extremes widen while a position is open? (`extreme_tracking`, default `"ALWAYS"`)

Spec 5.1 defines `running_high = max(previous_spot_high, highest_spot_today)`;
spec 13 step 2 only updates extremes `if position == FLAT`. These conflict.
V1 default `ALWAYS` updates on every tick, in or out of a trade. Because
T = D = 180, a trail exit that gave back exactly T lands exactly on
`running_high - D`, so a trail exit usually arms an **immediate opposite
re-entry** (`REENTRY_IMMEDIATE`). Under `FLAT_ONLY` (literal spec 13) in-trade
highs/lows are discarded, the opposite trigger stays at the old level, and the
strategy goes flat after a trail. `FLAT_ONLY_PLUS_TRADE_BEST` is the middle
option (flat-only tracking, but seed references from the trade's best/worst on
exit). **This is the single largest behavioural fork in the engine.** The
three-mode comparison on one identical price path is in `tests/test_exits.py`
(`_R2_SPOT = [24350, 24380, 24700, 24520]`). Run the baseline under both
`ALWAYS` and `FLAT_ONLY` before sign-off.

### R6 — MFE when the favourable-gap regime retires (`gap_retirement_mfe_mode`, default `"REBASE"`)

Spec 10.1 engages the gap regime on a favourable opening gap and retires it
when spot returns to the previous close, but never says what happens to MFE.
A LONG carried into a +300 gap has MFE ≈ 300; on retirement spot is back at
the previous close so `drawback = MFE - R ≈ 300 ≥ T` and the trail fires on
the very next tick — every favourable gap that fills would close the position
instantly. V1 rebases MFE to the current R on retirement so the trail resumes
cleanly. `"CARRY"` reproduces the instant-trail reading
(`test_carry_mode_fires_the_trail_on_the_very_next_tick`). Related R6b:
the early stop stays off after retirement (`gap_retirement_rearms_early_stop`,
default False).

## Other things to keep in mind

- The spec's tick order (spec 13) in `strategy/backtester.py` is
  path-dependent. Do not rearrange it.
- Determinism (R8): same-timestamp events are sorted futures-first
  (`tie_break_order=("FUT","SPOT")`). Changing this changes the ledger.
- A trail exit can be a loss of up to -120 points, and a full breaker episode
  costs -360 points with no stop on the final leg — both by spec, not bugs.
  `metrics.json` diagnostics split these out.
- D=180 is a fixed point distance; over multi-year data it is a different
  %-of-index at different price levels. See the sign-off checklist at the end
  of `DECISIONS.md` before any V2 work.
