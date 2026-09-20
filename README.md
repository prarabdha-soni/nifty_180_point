# NIFTY 180-Point Swing Strategy — V1 Backtest Engine

Event-driven backtester implementing *NIFTY 180-Point Swing Strategy,
Backtest Specification V1*. Spot determines the signal and trade-management
state; futures independently confirms entries and is the instrument used for
simulated execution, transaction costs and P&L.

**V1 is the control model.** Every strategy constant lives in
`strategy/config.py`. Every place the spec was ambiguous is resolved by an
explicit switch and recorded in [`DECISIONS.md`](DECISIONS.md). Read that
document before signing off the baseline ledger.

---

## Quickstart

```bash
pip install pandas pytest

# 1. prove the engine reproduces the spec
python -m pytest tests/ -q          # 78 tests
python validate_v1.py               # spec 16 acceptance report, 10/10

# 2. exercise the pipeline on synthetic data (NOT market data)
python make_sample_data.py --days 60 --out data/sample_ticks.csv
python run_backtest.py --merged data/sample_ticks.csv --out results/

# 3. run on real data
python run_backtest.py --merged data/nifty_ticks.csv --out results/
python run_backtest.py --spot data/spot.csv --futures data/fut.csv --out results/
```

---

## Data contract

Tick data is preferred. One-minute bars run fine and are a reasonable
approximation, but will not reproduce exact intra-tick sequencing.

**Merged layout** — spec section 3's minimum record, one synchronized snapshot
per row:

```csv
timestamp,trade_date,spot_ltp,futures_ltp,futures_contract_expiry
2026-01-05 09:16:00,2026-01-05,24350.10,24371.25,2026-01-29
```

Only `timestamp`, `spot_ltp` and `futures_ltp` are strictly required.
`trade_date` defaults to the timestamp's date. Without
`futures_contract_expiry` there is no expiry square-off and no roll detection.

**Split layout** — two files, which is what most tick vendors ship:

```csv
# spot.csv
timestamp,ltp
2026-01-05 09:16:00,24350.10

# futures.csv
timestamp,ltp,expiry
2026-01-05 09:16:00,24371.25,2026-01-29
```

Interleaved into one ordered stream. Events sharing a timestamp are ordered
futures-first so the spot tick sees fresh futures (R8).

**Previous-session levels** are derived from the previous trading session in
the dataset, so the first session is a warm-up with no trading. To trade it,
supply them:

```csv
# references.csv
trade_date,prev_spot_high,prev_spot_low,prev_spot_close,prev_fut_high,prev_fut_low
2026-01-05,24500.00,24200.00,24350.00,24520.00,24220.00
```

```bash
python run_backtest.py --merged data/ticks.csv --references data/references.csv --no-warmup
```

---

## Configuration

Override any field with a JSON file:

```bash
python run_backtest.py --merged data/ticks.csv --config overrides.json
```

```json
{
  "swing_distance": 180,
  "extreme_tracking": "FLAT_ONLY",
  "gap_retirement_mfe_mode": "CARRY",
  "slippage_points_per_side": 0.5
}
```

Core parameters (spec section 2) — **do not change for the V1 baseline**:

| Parameter | Symbol | Default | Meaning |
|---|---|---|---|
| `swing_distance` | D | 180 | Entry reversal distance from extreme |
| `early_stop_distance` | S | 60 | Initial adverse stop |
| `early_arm_distance` | A | 60 | Favourable move that permanently disarms S |
| `partial_profit_distance` | P | 140 | One-time partial exit threshold |
| `trail_distance` | T | 180 | Giveback from best favourable move |
| `partial_fraction` | q | 0.50 | Fraction closed at partial |
| `breaker_limit` | B | 2 | Second consecutive early stop triggers breaker |
| `gap_threshold` | G | 0.003 | 0.30% opening gap threshold |
| `transaction_cost_per_side` | c | 0.00015 | 0.015% of executed futures notional |
| `lot_size` × `num_lots` | | 65 × 6 = 390 | Base quantity |

Resolution switches are documented in [`DECISIONS.md`](DECISIONS.md).
The V2 research variables from spec section 19 are all already configurable —
`futures_confirm_distance` (η·D) is exposed separately from `swing_distance`
so the entry and confirmation distances can be decoupled without touching
code.

---

## Architecture

Follows spec section 17. Signal generation, state transitions, execution
simulation and P&L accounting are separate, so V2 can change parameters or
logic without contaminating the baseline.

```
strategy/
    config.py            all constants + resolution switches
    state.py             spec 4 state model
    signals.py           spec 5 reference extremes and triggers
    pending_manager.py   spec 5.4 pending slot + spec 11 carry anchors
    position_manager.py  spec 6,7,8 open / partial / close / reverse
    breaker.py           spec 9 double early-stop breaker + deferred flip
    gap_manager.py       spec 10 morning gap regime
    execution.py         spec 12.1 fill pricing and per-execution cost
    pnl.py               spec 12.2, 15.1 trade ledger and reconciliation
    metrics.py           spec 15.2 metrics and attribution
    data.py              loading, deterministic merge, session references
    backtester.py        spec 13 tick order, spec 14 session loop
tests/
    test_entries.py          scenarios 1-3 + entry edges
    test_exits.py            scenarios 4-7 + the R2 three-mode comparison
    test_breaker.py          scenarios 8-10 + R1/R3
    test_gaps.py             spec 10 + R5/R6
    test_tick_precedence.py  determinism, R8, sessions, expiry, R7, costs
```

The tick handler follows spec section 13 exactly — *"Do not rearrange this
sequence. The source strategy is path-dependent and same-tick precedence
changes outcomes."*

---

## Outputs

`run_backtest.py --out results/` writes:

| File | Contents |
|---|---|
| `trades.csv` | The spec 15.1 trade ledger, one row per trade |
| `executions.csv` | Every simulated futures fill with spot signal price, futures fill price, qty, reason and cost |
| `events.csv` | Engine event log: entries, pendings, stops, breaker engagements, gap regimes, rolls, carries |
| `metrics.json` | Spec 15.2 metrics, attribution by exit reason, diagnostics |
| `config.json` | The exact configuration used — archive this with the ledger |

Exit reasons: `EARLY_STOP`, `TRAIL_STOP`, `DEFERRED_FLIP`, `EXPIRY_SQUAREOFF`,
`ADVERSE_GAP_ASSUMED_MANUAL_EXIT`, `DATASET_END`.

Spec 10.2 requires adverse-gap results to be reported separately because the
exit is a simulation convention rather than a broker rule — they are tagged
and broken out in both the attribution table and `diagnostics`.

### Diagnostics

Beyond spec 15.2, the metrics block reports:

- `trail_exits_winning` / `trail_exits_losing` — a trail can exit at `R = -120`,
  so one bucket hides two outcomes
- `breaker_episodes`, `breaker_worst_net`, `breaker_worst_mae_points`
- `breaker_unprotected_seconds_total` — time spent with no stop between the
  suppressed second early stop and the deferred flip
- `breaker_held_overnight` — unprotected intervals that spanned a session
- `top5_net_concentration` — what share of net P&L the five best trades carry

---

## Reconciliation

Spec 18 requires trade-level gross, cost and net to reconcile to
strategy-level totals. `run_backtest.py` asserts it on every run and exits
non-zero on failure:

```
Reconciliation (spec 18): PASS
  cost_reconciles            True
  net_reconciles             True
  quantity_reconciles        True
```

`quantity_reconciles` checks that `partial_qty + exit_qty == entry_qty` for
every closed trade, so no quantity is created or lost by a partial or a
reversal.

---

## Before optimising

Spec section 19: *"Do not begin optimization until V1 exactly reproduces the
validation scenarios and the baseline trade ledger is signed off."*

The sign-off checklist is at the end of [`DECISIONS.md`](DECISIONS.md). Two
items on it are not procedural:

1. **Run the baseline under both R2 modes.** `extreme_tracking` decides
   whether a trail exit immediately flips the book. It is the largest
   behavioural fork in the engine and the spec supports both readings.

2. **Check the date range for the fixed-`D` problem.** `D = 180` is 1.5% of
   NIFTY at 12,000 and 0.74% at 24,300. A multi-year backtest at a fixed point
   distance silently tests two different strategies and blends the results.
   Report in percentage-of-index terms alongside points, or restrict the
   window, before treating the curve as one control model.
