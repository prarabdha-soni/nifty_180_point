"""
State model -- spec section 4.

Every field named in the spec's State dict exists here. Fields the spec
declared but never assigned are marked with the DECISIONS.md rule that
now assigns them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional
import datetime as dt


class Side(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1

    @property
    def opposite(self) -> "Side":
        return Side(-int(self))

    @property
    def label(self) -> str:
        return {1: "LONG", -1: "SHORT", 0: "FLAT"}[int(self)]


class PendingType(str):
    """R4: assigned by pending_manager. Spec declared it, never set it."""
    FIRST_ENTRY = "FIRST_ENTRY"
    REENTRY = "REENTRY"


@dataclass
class MarketSnapshot:
    timestamp: dt.datetime
    trade_date: dt.date
    spot_ltp: Optional[float]
    futures_ltp: Optional[float]
    futures_contract_expiry: Optional[dt.date]
    source: str  # "SPOT" or "FUT"


@dataclass
class State:
    # --- position ---------------------------------------------------------
    position: Side = Side.FLAT
    qty: int = 0
    spot_entry_price: Optional[float] = None
    futures_entry_price: Optional[float] = None
    entry_timestamp: Optional[dt.datetime] = None
    best_spot_price: Optional[float] = None
    worst_spot_price: Optional[float] = None
    mfe_points: float = 0.0
    mae_points: float = 0.0
    early_stop_active: bool = False
    partial_done: bool = False
    trade_id: Optional[int] = None
    entry_reason: str = "ENTRY"
    overnight_flag: bool = False
    breaker_triggered_this_trade: bool = False
    gap_regime_flag_this_trade: bool = False
    early_stop_count_this_trade: int = 0

    # --- flat-state references -------------------------------------------
    running_high: Optional[float] = None
    running_low: Optional[float] = None
    futures_running_high: Optional[float] = None
    futures_running_low: Optional[float] = None

    # --- breaker ----------------------------------------------------------
    consecutive_early_stops: int = 0
    deferred_flip_active: bool = False
    deferred_flip_price: Optional[float] = None   # R4: audit only; R is authoritative
    deferred_started_at: Optional[dt.datetime] = None
    deferred_unprotected_seconds: float = 0.0

    # --- pending slot -----------------------------------------------------
    pending_side: Optional[Side] = None
    pending_spot_level: Optional[float] = None
    pending_futures_level: Optional[float] = None
    pending_type: Optional[str] = None            # R4

    # --- carry across sessions -------------------------------------------
    carry_anchor_side: Optional[Side] = None      # R4
    carry_anchor_spot_level: Optional[float] = None
    carry_anchor_futures_level: Optional[float] = None
    carry_anchor_expiry: Optional[dt.date] = None  # R7: roll detection

    # --- gap regime -------------------------------------------------------
    gap_regime: bool = False
    gap_fill_price: Optional[float] = None
    gap_hold_only_partial: bool = False
    gap_tested: bool = False                      # R4
    suppress_first_entries: bool = False          # R4
    suppress_reversals: bool = False              # R4
    trading_disabled_today: bool = False

    # --- session bookkeeping ---------------------------------------------
    trade_date: Optional[dt.date] = None
    session_spot_high: Optional[float] = None
    session_spot_low: Optional[float] = None
    session_spot_open: Optional[float] = None
    session_spot_close: Optional[float] = None
    session_fut_high: Optional[float] = None
    session_fut_low: Optional[float] = None
    prev_spot_high: Optional[float] = None
    prev_spot_low: Optional[float] = None
    prev_spot_close: Optional[float] = None
    prev_fut_high: Optional[float] = None
    prev_fut_low: Optional[float] = None
    current_contract_expiry: Optional[dt.date] = None
    is_expiry_day: bool = False
    expiry_squareoff_done: bool = False

    # --- latest market ----------------------------------------------------
    last_spot: Optional[float] = None
    last_futures: Optional[float] = None
    last_timestamp: Optional[dt.datetime] = None

    # --- post-exit handoff (R2, FLAT_ONLY_PLUS_TRADE_BEST mode) -----------
    last_trade_best: Optional[float] = None
    last_trade_worst: Optional[float] = None

    # ----------------------------------------------------------------------
    def is_flat(self) -> bool:
        return self.position == Side.FLAT

    def has_pending(self) -> bool:
        return self.pending_side is not None

    def clear_pending(self) -> None:
        self.pending_side = None
        self.pending_spot_level = None
        self.pending_futures_level = None
        self.pending_type = None

    def clear_position(self) -> None:
        self.position = Side.FLAT
        self.qty = 0
        self.spot_entry_price = None
        self.futures_entry_price = None
        self.entry_timestamp = None
        self.best_spot_price = None
        self.worst_spot_price = None
        self.mfe_points = 0.0
        self.mae_points = 0.0
        self.early_stop_active = False
        self.partial_done = False
        self.trade_id = None
        self.overnight_flag = False
        self.breaker_triggered_this_trade = False
        self.gap_regime_flag_this_trade = False
        self.early_stop_count_this_trade = 0
        self.deferred_flip_active = False
        self.deferred_flip_price = None
        self.deferred_started_at = None
        self.deferred_unprotected_seconds = 0.0

    def r_points(self, spot_ltp: float) -> float:
        """Spec 7: normalized P&L. R = position * (spot_ltp - spot_entry_price)."""
        if self.position == Side.FLAT or self.spot_entry_price is None:
            return 0.0
        return int(self.position) * (spot_ltp - self.spot_entry_price)

    def drawback(self, spot_ltp: float) -> float:
        return self.mfe_points - self.r_points(spot_ltp)
