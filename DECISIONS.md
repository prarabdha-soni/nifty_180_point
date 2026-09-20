# V1 Implementation Decisions

The spec instruction is *"reproduce V1 exactly first. Do not optimize,
simplify, or reinterpret logic."* Everywhere the spec was unambiguous, this
engine follows it literally. This document lists every place it was not, what
V1 does instead, and the config switch that reproduces the alternative
reading.

Nothing here is an optimisation. Every entry is a place where the spec had two
readings or no reading at all, and code cannot abstain.

**How to use this document:** read it before signing off the baseline ledger.
Each item is a decision you are implicitly signing off. Where a decision is
marked **material**, the two readings produce materially different ledgers and
you should look at both before committing.

---

## R1 — The breaker reset condition makes the breaker dead code · **material**

**Spec 9.**

```
if MFE >= A or trailing_stop_fires or position_becomes_flat:
    consecutive_early_stops = 0
```

**Spec 8.2** models every early-stop reversal as *"a close of the old trade
plus a new opposite entry"*. So the position passes through FLAT on **every
single early stop**. Read literally, the counter resets each time it
increments, never reaches `B = 2`, and the whole of section 9 — breaker,
deferred flip, unprotected interval — is unreachable code.

**V1 resets `consecutive_early_stops` on:**

- `MFE >= A` (the trade armed — the flip worked)
- a trail-stop exit
- going flat **without** an immediate reversal (expiry square-off, adverse gap)

and **not** on the momentary flat inside a reversal.

Switch: `breaker_reset_on_reversal_flat=True` reproduces the literal,
breaker-disabled reading. `test_literal_reset_reading_makes_the_breaker_dead_code`
demonstrates it.

---

## R2 — Do reference extremes widen while a position is open? · **material**

**Spec 5.1** reads like a plain day extreme:

```
running_high = max(previous_spot_high, highest_spot_today)
```

**Spec 13 step 2** gates the update:

```
if position == FLAT:
    update_spot_reference_extremes()
```

These conflict. Under the step-2 reading, the high or low made *during* a
trade is never recorded — and then **spec 8.4's
`arm_opposite_reentry_if_applicable()` has nothing to arm from**, because
`running_high - 180` sits far away from wherever the trail actually exited.

**V1 default: `extreme_tracking="ALWAYS"`** — extremes widen on every tick,
in or out of a position. This makes section 8.4 coherent.

Alternatives:

| Mode | Behaviour |
|---|---|
| `ALWAYS` (default) | In-trade extremes count. A trail exit usually arms an immediate opposite re-entry. |
| `FLAT_ONLY` | Literal section 13 reading. In-trade extremes discarded; armed re-entry almost never fires. |
| `FLAT_ONLY_PLUS_TRADE_BEST` | Flat-only, but on exit the references are seeded with the trade's best and worst spot price. |

**This is the single largest behavioural fork in the engine.** Under `ALWAYS`,
a trail exit that gave back exactly `T` sits precisely at
`running_high - D` (since `T = D = 180`), so the opposite side is triggered by
construction and section 8.4's *"does not automatically reverse"* becomes
close to a distinction without a difference. Under `FLAT_ONLY` that clause
means what it says, but `arm_opposite_reentry_if_applicable()` is vestigial.

Run the baseline both ways before sign-off. `test_exits.py` has the
three-mode comparison on one identical price path.

---

## R3 — Nothing resets the counter after the deferred flip executes

**Spec 9** never resets `consecutive_early_stops` when the flip fires. Left as
written, the counter is still at `B` when the new opposite position opens, so
that position is *born in breaker state* and the strategy never returns to
normal stop behaviour for the rest of the run.

**V1 resets the counter and clears `deferred_flip_active` on flip execution.**

Switch: `breaker_reset_on_deferred_flip=False`.

---

## R4 — Six state fields are declared but never assigned

Spec section 4 declares these; no rule in the spec ever writes them.

| Field | V1 rule |
|---|---|
| `pending_type` | `FIRST_ENTRY` when created by flat-state entry evaluation; `REENTRY` when created by `arm_opposite_reentry_if_applicable()`. Spec 11 depends on this distinction at 15:30. |
| `carry_anchor_side` / `_price` | Written at session close from an unconfirmed `FIRST_ENTRY` pending; restored as a live pending at the next session start. A `REENTRY` pending is discarded (spec 11). |
| `deferred_flip_price` | Stored as `spot_entry ± (S + T)` for audit only. The `R <= -(S+T)` test stays authoritative. |
| `suppress_first_entries` | Set during a favourable gap regime (spec 10.1 *"DISALLOW normal new entries"*), on adverse-gap disable, and after expiry square-off. |
| `suppress_reversals` | Same triggers. |
| `gap_tested` | Set once the gap test runs, or once the 09:20 window closes without an eligible tick, so it runs at most once per session. |

---

## R5 — The gap logic only ever applies to a carried position

**Spec 10:** `directional_gap = position * (opening_spot - previous_spot_close)`

When `position == FLAT` this is identically zero, so neither the favourable
nor the adverse branch can fire. **A qualifying 0.30% gap on a flat morning
does nothing at all.**

**V1 implements this literally.** It follows from the arithmetic and is
probably intended — the source strategy's gap rules are about protecting an
overnight position, not about filtering entries.

Switch: `gap_applies_when_flat=True` adds an entry-suppression branch for
the flat case.

---

## R6 — MFE is undefined when the gap regime retires · **material**

**Spec 10.1** engages the regime on a favourable gap and retires it when spot
returns to the previous close. It never says what happens to MFE.

A LONG carried into a +300 point favourable gap accumulates `MFE ≈ 300`. The
regime retires **exactly** when spot returns to the previous close — at which
point `drawback = MFE - R ≈ 300 ≥ T`, so the trail fires on the very next
tick. Every favourable gap that fills would close the position instantly.

**V1 default: `gap_retirement_mfe_mode="REBASE"`** — MFE is rebased to the
current `R` on retirement, so the position resumes with a clean trail.

Switch: `"CARRY"` reproduces the instant-trail reading.
`test_carry_mode_fires_the_trail_on_the_very_next_tick` demonstrates it.

### R6b — Does the early stop return on retirement?

A favourable gap sets `early_stop_active = False`. **V1 keeps it off**,
consistent with section 8.1's *"permanent for this trade"* language.

Switch: `gap_retirement_rearms_early_stop=True`.

---

## R7 — No futures rollover rule exists

The spec carries positions overnight and force-closes on expiry day at 15:00,
but never mentions the contract roll. `futures_entry_price` and
`futures_running_high/low` belong to a specific contract; across a roll the
basis shifts and those levels are meaningless.

**V1 on a detected roll (the `futures_contract_expiry` field changes between
sessions):**

- reset `futures_running_high` / `futures_running_low` and reseed from the new
  contract's own prints
- discard any carry anchor, because its frozen futures level belongs to the
  old contract's basis
- emit a `FUTURES_ROLL` event, and a warning if a position was somehow still
  open (expiry square-off should have prevented it)

Switch: `discard_carry_anchor_on_roll=False`.

---

## R8 — Determinism is required but the merge order is undefined

**Spec 1:** *"the same input data and configuration must produce the same
trade ledger."*
**Spec 3:** *"merge by timestamp while retaining event order."*

Two events sharing a timestamp have no defined order, so the same file can
produce two different ledgers — which contradicts spec 1 directly.

**V1 sorts by `(timestamp, source_rank, original_index)`** with `source_rank`
from `tie_break_order`, default `("FUT", "SPOT")`. Futures first means the
spot tick that drives the state machine always sees the freshest futures
price. The sort is stable, so equal keys keep file order.

This is **not** cosmetic. `test_spot_first_tiebreak_delays_the_same_entry_by_one_tick`
shows the same data entering a trade under one order and parking a pending
under the other.

Switch: `tie_break_order=("SPOT", "FUT")`.

---

## R9 — No slippage model

The spec fills at `futures_ltp`. For a strategy that reverses this often, on
390 qty, an optimistic fill assumption compounds across every execution.

**V1 sets `slippage_points_per_side = 0.0`**, so the baseline ledger is
exactly the spec's. The parameter is wired through `ExecutionSimulator` from
day one, always applied against the trader, so V2 can measure its impact
without a code change.

At 390 qty against NIFTY futures depth, real slippage should be close to
zero — this is a measurement hook, not an expected cost.

---

## R10 — The cost model is symmetric; real futures cost is not

**Spec 12.1:** `0.00015` per side, both sides.

STT on index futures applies to the **sell side only**, so real round-trip
cost is asymmetric. **V1 keeps `stt_sell_side_pct = 0.0`** so the control
model matches the spec. Reconcile the number against an actual contract note
before trusting absolute P&L; it will not change the sign of anything.

---

## Decisions not forced by ambiguity

These are implementation choices where the spec is silent and the choice is
inconsequential, recorded for completeness.

| Choice | V1 behaviour |
|---|---|
| Session references | Derived from the previous trading session present in the dataset. The first session is a warm-up (references built, no trading) unless `--references` supplies them. |
| Reference window at session start | `running_high/low` reset to the previous session's extremes, then widen intraday. The window is two sessions, per spec 5.1. |
| Position open at dataset end | Closed at the final tick with `exit_reason=DATASET_END` and excluded from headline metrics, so the ledger balances. Disable with `close_at_dataset_end=False`. |
| Armed re-entry side | `arm_opposite_reentry_if_applicable()` only arms the **opposite** side. If it is not triggered, normal flat-state logic resumes on the next tick. |
| Sharpe / Sortino basis | Daily **net P&L in rupees**, annualised by `sqrt(252)`. A P&L-Sharpe, not a return-on-capital Sharpe — the strategy has no defined capital base. |
| Expiry day detection | `trade_date == futures_contract_expiry`. |

---

## Two properties worth knowing before you read the results

Neither is a bug. Both follow from the spec as written and both shape how the
output should be read.

**1. A trail exit can be a loss of up to `-120` points.**
The trail needs `MFE ≥ A = 60` and fires at `MFE - T = MFE - 180`. At the
minimum arming level that is `R = -120`, double the early stop. Spec 16's own
trail row (SHORT, best 24,260, exit 24,440) is exactly this case, at
`R = -120`. The metrics block therefore splits trail exits into winners and
losers rather than reporting them as one bucket.

**2. The breaker converts a bounded loss into an unbounded one.**
The full chop sequence is `-60`, reverse, `-60`, then hold to `-240`:
**-360 points** from one episode, and the position is unstopped throughout the
final leg. Combined with overnight carry, that unstopped interval can span a
night with only the 09:16 adverse-gap rule as a backstop — and that rule fires
*after* the gap is realised.

`test_full_chop_episode_costs_S_plus_S_plus_S_T` pins the arithmetic.
The diagnostics block reports `breaker_episodes`,
`breaker_worst_mae_points`, `breaker_unprotected_seconds_total` and
`breaker_held_overnight` separately so the baseline ledger can settle the
question with data rather than opinion.

---

## Sign-off checklist

Before running any V2 parameter work:

- [ ] `python validate_v1.py` reports 10/10
- [ ] `python -m pytest tests/ -q` passes
- [ ] Baseline run reconciles (`all_ok: true` in the run output)
- [ ] Baseline run repeated with `extreme_tracking="FLAT_ONLY"` (R2) and the
      difference understood
- [ ] Baseline run repeated with `gap_retirement_mfe_mode="CARRY"` (R6) and
      the difference understood
- [ ] `breaker_*` diagnostics reviewed and the tail risk accepted or the rule
      changed
- [ ] Transaction cost reconciled against a real contract note (R10)
- [ ] Date range checked for the fixed-`D` problem: `D = 180` is 1.5% of NIFTY
      at 12,000 and 0.74% at 24,300, so a multi-year backtest silently blends
      two different strategies. Report results in percentage-of-index terms
      alongside points, or restrict the window.
- [ ] `results/config.json` archived alongside `results/trades.csv`
