"""
Reference levels and entry triggers -- spec section 5.

R2 (DECISIONS.md) resolves the contradiction between spec 5.1
("running_high = max(previous_spot_high, highest_spot_today)", which reads
like a plain day extreme) and spec 13 step 2 (which only calls
update_spot_reference_extremes() while FLAT).

Three modes:
  ALWAYS                     extremes widen on every tick, in or out of a
                             position. Makes section 8.4's
                             arm_opposite_reentry_if_applicable() reachable.
  FLAT_ONLY                  literal section 13 reading. In-trade extremes are
                             discarded, so an armed re-entry almost never fires.
  FLAT_ONLY_PLUS_TRADE_BEST  flat-only, but on exit the references are seeded
                             with the trade's best and worst spot price.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .config import Config
from .state import State, Side


class SignalEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Reference extremes (spec 5.1)
    # ------------------------------------------------------------------
    def update_spot_reference_extremes(self, st: State, spot_ltp: float) -> None:
        st.session_spot_high = spot_ltp if st.session_spot_high is None \
            else max(st.session_spot_high, spot_ltp)
        st.session_spot_low = spot_ltp if st.session_spot_low is None \
            else min(st.session_spot_low, spot_ltp)

        base_high = st.prev_spot_high if st.prev_spot_high is not None else spot_ltp
        base_low = st.prev_spot_low if st.prev_spot_low is not None else spot_ltp

        rh = max(base_high, st.session_spot_high)
        rl = min(base_low, st.session_spot_low)

        # "References only widen; they never narrow."
        st.running_high = rh if st.running_high is None else max(st.running_high, rh)
        st.running_low = rl if st.running_low is None else min(st.running_low, rl)

    def update_futures_reference_extremes(self, st: State, futures_ltp: float) -> None:
        st.session_fut_high = futures_ltp if st.session_fut_high is None \
            else max(st.session_fut_high, futures_ltp)
        st.session_fut_low = futures_ltp if st.session_fut_low is None \
            else min(st.session_fut_low, futures_ltp)

        base_high = st.prev_fut_high if st.prev_fut_high is not None else futures_ltp
        base_low = st.prev_fut_low if st.prev_fut_low is not None else futures_ltp

        fh = max(base_high, st.session_fut_high)
        fl = min(base_low, st.session_fut_low)

        st.futures_running_high = fh if st.futures_running_high is None \
            else max(st.futures_running_high, fh)
        st.futures_running_low = fl if st.futures_running_low is None \
            else min(st.futures_running_low, fl)

    def should_update_extremes(self, st: State) -> bool:
        mode = self.cfg.extreme_tracking
        if mode == "ALWAYS":
            return True
        return st.is_flat()

    def seed_references_from_trade(
        self, st: State, best_spot: Optional[float], worst_spot: Optional[float]
    ) -> None:
        """FLAT_ONLY_PLUS_TRADE_BEST: widen references with the trade's extremes."""
        if self.cfg.extreme_tracking != "FLAT_ONLY_PLUS_TRADE_BEST":
            return
        for px in (best_spot, worst_spot):
            if px is None:
                continue
            st.running_high = px if st.running_high is None else max(st.running_high, px)
            st.running_low = px if st.running_low is None else min(st.running_low, px)

    # ------------------------------------------------------------------
    # Trigger levels (spec 5.2)
    # ------------------------------------------------------------------
    def spot_triggers(self, st: State) -> Tuple[Optional[float], Optional[float]]:
        D = self.cfg.swing_distance
        short_trig = None if st.running_high is None else st.running_high - D
        long_trig = None if st.running_low is None else st.running_low + D
        return short_trig, long_trig

    def futures_triggers(self, st: State) -> Tuple[Optional[float], Optional[float]]:
        E = self.cfg.confirm_distance
        short_trig = None if st.futures_running_high is None else st.futures_running_high - E
        long_trig = None if st.futures_running_low is None else st.futures_running_low + E
        return short_trig, long_trig

    # ------------------------------------------------------------------
    # Entry evaluation (spec 5.3)
    # ------------------------------------------------------------------
    def evaluate_entry(
        self, st: State, spot_ltp: float, futures_ltp: Optional[float]
    ) -> Tuple[Optional[Side], bool]:
        """
        Returns (side, futures_confirmed).
          (None, False)   no spot trigger
          (SHORT, True)   immediate short -- spot and futures both satisfied
          (SHORT, False)  spot satisfied, futures not -- caller creates pending

        Spec 5.3: "If both sides are true on the same tick, SHORT takes precedence."
        """
        s_short, s_long = self.spot_triggers(st)
        f_short, f_long = self.futures_triggers(st)

        short_spot_ok = s_short is not None and spot_ltp <= s_short
        long_spot_ok = s_long is not None and spot_ltp >= s_long

        if not short_spot_ok and not long_spot_ok:
            return None, False

        if short_spot_ok:                       # SHORT precedence
            confirmed = (
                futures_ltp is not None
                and f_short is not None
                and futures_ltp <= f_short
            )
            return Side.SHORT, confirmed

        confirmed = (
            futures_ltp is not None
            and f_long is not None
            and futures_ltp >= f_long
        )
        return Side.LONG, confirmed
