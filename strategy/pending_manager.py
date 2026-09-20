"""
Pending entry slot -- spec section 5.4, plus the carry-anchor rules of
spec section 11 that the source spec declared but never implemented.

Spec 5.4:
    Pending SHORT: cancel if spot rises back above the frozen spot level;
                   fire if futures reaches the frozen futures level.
    Pending LONG:  mirror.
    "The actual spot entry reference is the current spot price when futures
     confirms, not the original trigger price."

R4 resolutions applied here:
  * pending_type is assigned. A pending created by the normal flat-state
    entry evaluation is FIRST_ENTRY. One created by
    arm_opposite_reentry_if_applicable() after a trail or deferred exit is
    REENTRY. Spec 11 distinguishes them at 15:30 but never set the field.
  * carry_anchor_* is written at session close for an unconfirmed
    FIRST_ENTRY pending, and restored at the next session start.

R7: a carry anchor's frozen futures level belongs to the contract that was
current when it was created. Across a roll the basis changes, so the anchor
is discarded rather than applied to a different contract.
"""

from __future__ import annotations

from typing import Optional
import datetime as dt

from .config import Config
from .state import State, Side, PendingType


class PendingManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------
    def create(
        self,
        st: State,
        side: Side,
        spot_level: float,
        futures_level: Optional[float],
        pending_type: str,
    ) -> None:
        st.pending_side = side
        st.pending_spot_level = spot_level
        st.pending_futures_level = futures_level
        st.pending_type = pending_type

    # ------------------------------------------------------------------
    def process(
        self, st: State, spot_ltp: float, futures_ltp: Optional[float]
    ) -> Optional[Side]:
        """
        Spec 13 step 3: "cancellation/retrace is checked before firing".

        Returns the side to open, or None (either cancelled or still waiting).
        """
        if not st.has_pending():
            return None

        side = st.pending_side
        s_level = st.pending_spot_level
        f_level = st.pending_futures_level

        if side == Side.SHORT:
            if spot_ltp > s_level:                  # retrace -> cancel
                st.clear_pending()
                return None
            if futures_ltp is not None and f_level is not None and futures_ltp <= f_level:
                return Side.SHORT
            return None

        if side == Side.LONG:
            if spot_ltp < s_level:
                st.clear_pending()
                return None
            if futures_ltp is not None and f_level is not None and futures_ltp >= f_level:
                return Side.LONG
            return None

        return None

    # ------------------------------------------------------------------
    # Carry anchor (spec 11)
    # ------------------------------------------------------------------
    def store_carry_anchor(self, st: State) -> bool:
        """
        At session end: a FIRST_ENTRY pending is stored; a REENTRY pending is
        discarded. Returns True if an anchor was stored.
        """
        if not st.has_pending():
            return False

        if st.pending_type == PendingType.FIRST_ENTRY:
            st.carry_anchor_side = st.pending_side
            st.carry_anchor_spot_level = st.pending_spot_level
            st.carry_anchor_futures_level = st.pending_futures_level
            st.carry_anchor_expiry = st.current_contract_expiry
            st.clear_pending()
            return True

        st.clear_pending()   # trail / deferred re-entry pending is discarded
        return False

    def clear_carry_anchor(self, st: State) -> None:
        st.carry_anchor_side = None
        st.carry_anchor_spot_level = None
        st.carry_anchor_futures_level = None
        st.carry_anchor_expiry = None

    def restore_carry_anchor(self, st: State, contract_expiry: Optional[dt.date]) -> bool:
        """
        At session start, re-arm a stored anchor as a live FIRST_ENTRY pending.
        R7: discarded if the futures contract has rolled.
        """
        if st.carry_anchor_side is None:
            return False

        rolled = (
            self.cfg.discard_carry_anchor_on_roll
            and st.carry_anchor_expiry is not None
            and contract_expiry is not None
            and contract_expiry != st.carry_anchor_expiry
        )
        if rolled:
            self.clear_carry_anchor(st)
            return False

        self.create(
            st,
            side=st.carry_anchor_side,
            spot_level=st.carry_anchor_spot_level,
            futures_level=st.carry_anchor_futures_level,
            pending_type=PendingType.FIRST_ENTRY,
        )
        self.clear_carry_anchor(st)
        return True
