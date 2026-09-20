"""
Morning gap logic -- spec section 10.

Spec 10:
    gap_pct        = (opening_spot - previous_spot_close) / previous_spot_close
    qualifying_gap = abs(gap_pct) >= G
    directional_gap = position * (opening_spot - previous_spot_close)

R5 -- note that directional_gap is identically zero when position == FLAT,
so as written the whole of section 10 applies only to a position carried
overnight. A qualifying gap on a flat morning does nothing at all. V1
implements that literally; cfg.gap_applies_when_flat=True adds an
entry-suppression branch for the flat case.

R6 -- the spec never says what happens to MFE when the favourable-gap
regime retires. A LONG carried into a +300 point favourable gap accumulates
MFE ~300; the regime retires exactly when spot returns to the previous
close, at which point drawback is already >= T and the trail fires on the
very next tick. V1 rebases MFE to current R on retirement. Set
cfg.gap_retirement_mfe_mode="CARRY" to reproduce the instant-trail reading.

R6b -- a favourable gap sets early_stop_active=False. The spec does not say
whether it returns on retirement. V1 keeps it off (consistent with the
"permanent for this trade" language in section 8.1).

Spec 10.2 -- the adverse-gap rule is explicitly a simulation convention,
not a broker rule. Trades closed this way carry exit_reason
ADVERSE_GAP_ASSUMED_MANUAL_EXIT and are reported separately in metrics.
"""

from __future__ import annotations

from typing import Optional
import datetime as dt

from .config import Config
from .state import State, Side


class GapManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._gap_direction: int = 0

    # ------------------------------------------------------------------
    def should_run_gap_test(self, st: State, ts: dt.datetime, is_spot: bool) -> str:
        """Returns "RUN", "SKIP" or "WAIT"."""
        if st.gap_tested:
            return "WAIT"
        t = ts.time()
        if t > self.cfg.t_gap_end:
            return "SKIP"                       # window closed, no late test
        if is_spot and t >= self.cfg.t_gap_start:
            return "RUN"
        return "WAIT"

    # ------------------------------------------------------------------
    def evaluate(
        self, st: State, opening_spot: float, futures_ltp: Optional[float]
    ) -> Optional[str]:
        """
        Runs the one-per-session gap test. Returns:
            None            no qualifying / no directional gap
            "FAVOURABLE"    gap regime engaged
            "ADVERSE"       caller must close the position at futures_ltp
            "FLAT_SUPPRESS" qualifying gap while flat (only if enabled)
        """
        st.gap_tested = True

        if st.prev_spot_close is None:
            return None

        gap_pct = (opening_spot - st.prev_spot_close) / st.prev_spot_close
        if abs(gap_pct) < self.cfg.gap_threshold:
            return None

        directional_gap = int(st.position) * (opening_spot - st.prev_spot_close)

        if st.is_flat():
            if self.cfg.gap_applies_when_flat:
                st.suppress_first_entries = True
                return "FLAT_SUPPRESS"
            return None

        self._gap_direction = 1 if opening_spot > st.prev_spot_close else -1

        if directional_gap > 0:
            self._engage_favourable(st)
            return "FAVOURABLE"

        if directional_gap < 0:
            st.trading_disabled_today = True
            st.suppress_first_entries = True
            st.suppress_reversals = True
            return "ADVERSE"

        return None

    # ------------------------------------------------------------------
    def _engage_favourable(self, st: State) -> None:
        st.gap_regime = True
        st.gap_fill_price = st.prev_spot_close
        st.gap_hold_only_partial = True
        st.early_stop_active = False
        st.suppress_first_entries = True     # "DISALLOW normal new entries"
        st.suppress_reversals = True
        st.gap_regime_flag_this_trade = True

    # ------------------------------------------------------------------
    def check_gap_fill(self, st: State, spot_ltp: float) -> bool:
        """Spec 13 step 1. Runs first on every spot tick."""
        if not st.gap_regime:
            return False
        if st.is_flat():                     # position gone -> regime is moot
            self.retire(st, spot_ltp)
            return True

        filled = (
            spot_ltp <= st.gap_fill_price if self._gap_direction > 0
            else spot_ltp >= st.gap_fill_price
        )
        if filled:
            self.retire(st, spot_ltp)
            return True
        return False

    # ------------------------------------------------------------------
    def retire(self, st: State, spot_ltp: float) -> None:
        st.gap_regime = False
        st.gap_fill_price = None
        st.gap_hold_only_partial = False
        st.suppress_first_entries = st.trading_disabled_today
        st.suppress_reversals = st.trading_disabled_today
        self._gap_direction = 0

        if st.is_flat():
            return

        if self.cfg.gap_retirement_mfe_mode == "REBASE":
            r = st.r_points(spot_ltp)
            st.mfe_points = max(r, 0.0)
            st.best_spot_price = spot_ltp if r > 0 else st.spot_entry_price

        if self.cfg.gap_retirement_rearms_early_stop:
            st.early_stop_active = st.mfe_points < self.cfg.early_arm_distance

    # ------------------------------------------------------------------
    def trailing_suspended(self, st: State) -> bool:
        return st.gap_regime

    def partial_allowed(self, st: State) -> bool:
        """During a favourable gap: ALLOW partial profit only."""
        return True

    def early_stop_allowed(self, st: State) -> bool:
        return not st.gap_regime
