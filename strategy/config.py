"""
Configuration for the NIFTY 180-Point Swing Strategy backtester.

Spec section 18 requires: "No strategy constant is hard-coded outside
configuration." Every number the engine uses lives here.

Fields are grouped:
  1. Core strategy parameters  -- verbatim from spec section 2
  2. Session / execution       -- verbatim from spec sections 11, 12
  3. Gap-resolution switches   -- NOT in the source spec. These exist because
     the spec was ambiguous or silent. Each one names the spec section it
     resolves. Defaults reproduce the reading documented in DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal, Tuple
import datetime as dt
import json


# ---------------------------------------------------------------------------
# Enumerated resolution modes (see DECISIONS.md)
# ---------------------------------------------------------------------------

ExtremeTracking = Literal["ALWAYS", "FLAT_ONLY", "FLAT_ONLY_PLUS_TRADE_BEST"]
GapMfeMode = Literal["REBASE", "CARRY"]
AdverseGapMode = Literal["EXIT_AT_FIRST_0916_FUTURES_PRICE", "HOLD_AND_DISABLE", "IGNORE"]


def _t(hhmmss: str) -> dt.time:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.time(h, m, s)


@dataclass(frozen=True)
class Config:
    # -----------------------------------------------------------------------
    # 1. Core strategy parameters (spec section 2 -- do not change for V1)
    # -----------------------------------------------------------------------
    swing_distance: float = 180.0            # D  entry reversal distance
    early_stop_distance: float = 60.0        # S  initial adverse stop
    early_arm_distance: float = 60.0         # A  favourable move that disarms S
    partial_profit_distance: float = 140.0   # P  one-time partial exit threshold
    trail_distance: float = 180.0            # T  giveback from best favourable move
    partial_fraction: float = 0.50           # q
    futures_confirm_distance: float | None = None  # eta*D; None => use swing_distance

    breaker_enabled: bool = True
    breaker_limit: int = 2                   # B

    gap_threshold: float = 0.003             # G  0.30%

    transaction_cost_per_side: float = 0.00015   # c  0.015% of futures notional
    slippage_points_per_side: float = 0.0        # V1 baseline = 0 (see DECISIONS R9)
    stt_sell_side_pct: float = 0.0               # V1 baseline = 0 (see DECISIONS R10)

    lot_size: int = 65
    num_lots: int = 6

    # -----------------------------------------------------------------------
    # 2. Session and execution (spec sections 10, 11)
    # -----------------------------------------------------------------------
    gap_check_start: str = "09:16:00"
    gap_check_end: str = "09:20:00"
    expiry_squareoff_time: str = "15:00:00"
    session_end_time: str = "15:30:00"

    # -----------------------------------------------------------------------
    # 3. Gap-resolution switches -- NOT from the source spec
    # -----------------------------------------------------------------------

    # R1 (spec 9): reset consecutive_early_stops on the momentary flat that
    # occurs inside an early-stop reversal? Literal spec reading = True, which
    # makes the breaker unreachable. V1 default = False.
    breaker_reset_on_reversal_flat: bool = False

    # R2 (spec 5.1 vs 13 step 2): do reference extremes widen while a position
    # is open? "ALWAYS" makes section 8.4's arm_opposite_reentry coherent.
    # This is the single largest behavioural fork in the engine.
    extreme_tracking: ExtremeTracking = "ALWAYS"

    # R3 (spec 9): reset the early-stop counter when the deferred flip executes.
    # Spec is silent; without this the strategy never leaves breaker state.
    breaker_reset_on_deferred_flip: bool = True

    # R4 (spec 8.4): may an armed opposite re-entry open on the same tick as
    # the trail exit, if both spot and futures triggers are already satisfied?
    reentry_same_tick: bool = True

    # R5 (spec 10): directional_gap is 0 when flat, so gap logic only ever
    # applies to carried positions. Set True to also suppress entries on a
    # qualifying gap morning while flat.
    gap_applies_when_flat: bool = False

    # R6 (spec 10.1): on gap-regime retirement, rebase MFE to current R, or
    # carry the gap-inflated MFE (which fires the trail immediately)?
    gap_retirement_mfe_mode: GapMfeMode = "REBASE"

    # R6b (spec 10.1): once a favourable gap disarms the early stop, does it
    # come back on retirement? Spec implies permanence; V1 keeps it off.
    gap_retirement_rearms_early_stop: bool = False

    # R7 (spec 11): discard a carried pending anchor across a futures roll,
    # since its frozen futures level belongs to the expired contract's basis.
    discard_carry_anchor_on_roll: bool = True

    # R8 (spec 1 vs 3): deterministic tiebreak when a spot and a futures event
    # share a timestamp. Futures first means the spot tick sees fresh futures.
    tie_break_order: Tuple[str, str] = ("FUT", "SPOT")

    # Spec 10.2: adverse-gap backtest convention.
    adverse_gap_mode: AdverseGapMode = "EXIT_AT_FIRST_0916_FUTURES_PRICE"

    # First session of the dataset has no previous-session reference levels.
    warmup_first_session: bool = True

    # Close any still-open position at the final tick so the ledger balances.
    # Tagged exit_reason=DATASET_END and excluded from headline metrics.
    close_at_dataset_end: bool = True

    # Annualisation basis for Sharpe / Sortino (spec 15.2 does not specify).
    trading_days_per_year: int = 252
    risk_free_annual: float = 0.0

    # -----------------------------------------------------------------------
    # Derived
    # -----------------------------------------------------------------------
    @property
    def base_qty(self) -> int:
        return self.lot_size * self.num_lots

    @property
    def partial_qty(self) -> int:
        return int(round(self.base_qty * self.partial_fraction))

    @property
    def confirm_distance(self) -> float:
        return self.swing_distance if self.futures_confirm_distance is None \
            else self.futures_confirm_distance

    @property
    def deferred_flip_threshold_r(self) -> float:
        """Spec 9: -(S + T). -240 with defaults."""
        return -(self.early_stop_distance + self.trail_distance)

    @property
    def t_gap_start(self) -> dt.time:
        return _t(self.gap_check_start)

    @property
    def t_gap_end(self) -> dt.time:
        return _t(self.gap_check_end)

    @property
    def t_expiry_squareoff(self) -> dt.time:
        return _t(self.expiry_squareoff_time)

    @property
    def t_session_end(self) -> dt.time:
        return _t(self.session_end_time)

    def validate(self) -> None:
        if self.partial_profit_distance >= self.trail_distance + self.early_arm_distance:
            # Not an error, but P >= A + T means the locked-winner property
            # described in DECISIONS.md no longer holds.
            pass
        if not (0.0 < self.partial_fraction < 1.0):
            raise ValueError("partial_fraction must be strictly between 0 and 1")
        if self.breaker_limit < 1:
            raise ValueError("breaker_limit must be >= 1")
        if self.lot_size <= 0 or self.num_lots <= 0:
            raise ValueError("lot_size and num_lots must be positive")
        if set(self.tie_break_order) != {"FUT", "SPOT"}:
            raise ValueError("tie_break_order must be a permutation of ('FUT','SPOT')")

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)


DEFAULT_CONFIG = Config()
