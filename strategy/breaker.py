"""
Double early-stop breaker -- spec section 9.

Source spec:
    if early_stop_fires:      consecutive_early_stops += 1
    if MFE >= A or trailing_stop_fires or position_becomes_flat:
                              consecutive_early_stops = 0
    if consecutive_early_stops >= B:
        DO_NOT_CLOSE / DO_NOT_REVERSE / KEEP_CURRENT_POSITION
        deferred_flip_active = True
        deferred_flip_threshold_R = -(S + T)

R1 -- the reset condition as written is self-defeating. Spec 8.2 models
every early-stop reversal as a close plus a new opposite entry, so the
position passes through FLAT on every single early stop. Reading
"position_becomes_flat" literally resets the counter each time, it never
reaches B, and the entire breaker is dead code.

V1 therefore resets the counter on:
    - MFE >= A            (the trade armed: the flip worked)
    - a trail-stop exit
    - going flat WITHOUT an immediate reversal (expiry close, adverse gap)
and NOT on the momentary flat inside a reversal.
Set cfg.breaker_reset_on_reversal_flat=True to reproduce the literal
(breaker-disabled) reading for comparison.

R3 -- the spec never resets the counter after the deferred flip executes.
Left as written, the new position opens already at B and the strategy stays
in breaker state permanently. V1 resets on flip.

Invariant asserted below: when deferred_flip_active is True, MFE < A.
Proof: the early stop can only fire while early_stop_active, which is
disarmed permanently once MFE >= A. So no early stop -- and therefore no
breaker -- can occur after arming. Consequence: the section 8.4 trail
condition (MFE >= A and ...) can never be true during a deferred flip.
That is the unprotected interval the spec flags.
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .state import State, Side


class Breaker:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------
    def register_early_stop(self, st: State) -> bool:
        """
        Called when an early stop condition fires. Returns True if the
        breaker suppresses the close/reverse (i.e. this is the Bth
        consecutive early stop).
        """
        st.consecutive_early_stops += 1
        st.early_stop_count_this_trade += 1

        if not self.cfg.breaker_enabled:
            return False
        return st.consecutive_early_stops >= self.cfg.breaker_limit

    # ------------------------------------------------------------------
    def engage_deferred_flip(self, st: State) -> None:
        st.deferred_flip_active = True
        st.breaker_triggered_this_trade = True
        # Audit-only price corresponding to R = -(S + T). R stays authoritative.
        if st.spot_entry_price is not None:
            st.deferred_flip_price = (
                st.spot_entry_price + int(st.position) * self.cfg.deferred_flip_threshold_r
            )
        assert st.mfe_points < self.cfg.early_arm_distance, (
            "invariant violated: breaker engaged on an armed trade"
        )

    # ------------------------------------------------------------------
    def check_deferred_flip_recovery(self, st: State, spot_ltp: float) -> bool:
        """
        Spec 9: if deferred_flip_active and R >= A, cancel the deferred flip,
        reset the counter and resume normal trailing logic.
        """
        if not st.deferred_flip_active:
            return False
        if st.r_points(spot_ltp) >= self.cfg.early_arm_distance:
            st.deferred_flip_active = False
            st.deferred_flip_price = None
            st.consecutive_early_stops = 0
            return True
        return False

    # ------------------------------------------------------------------
    def deferred_flip_due(self, st: State, spot_ltp: float) -> bool:
        if not st.deferred_flip_active:
            return False
        return st.r_points(spot_ltp) <= self.cfg.deferred_flip_threshold_r

    # ------------------------------------------------------------------
    def on_arm(self, st: State) -> None:
        """MFE reached A -> the flip sequence succeeded, counter resets."""
        st.consecutive_early_stops = 0

    def on_trail_exit(self, st: State) -> None:
        st.consecutive_early_stops = 0

    def on_flat_without_reversal(self, st: State) -> None:
        st.consecutive_early_stops = 0

    def on_reversal_flat(self, st: State) -> None:
        """R1: no-op by default. Only resets under the literal spec reading."""
        if self.cfg.breaker_reset_on_reversal_flat:
            st.consecutive_early_stops = 0

    def on_deferred_flip_executed(self, st: State) -> None:
        """R3."""
        if self.cfg.breaker_reset_on_deferred_flip:
            st.consecutive_early_stops = 0
