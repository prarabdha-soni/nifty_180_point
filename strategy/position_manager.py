"""
Position lifecycle -- spec sections 6, 7, 8.

Spec 8.2: "For ledger clarity, model the reversal internally as a close of
the old trade plus a new opposite entry at the same futures price, even if a
live implementation would submit one net order."  reverse() does exactly
that: two executions, two ledger rows.
"""

from __future__ import annotations

from typing import Optional
import datetime as dt

from .config import Config
from .state import State, Side
from .execution import ExecutionSimulator
from .pnl import Trade, TradeLedger


class PositionManager:
    def __init__(self, cfg: Config, sim: ExecutionSimulator, ledger: TradeLedger):
        self.cfg = cfg
        self.sim = sim
        self.ledger = ledger

    # ------------------------------------------------------------------
    # Open (spec 6)
    # ------------------------------------------------------------------
    def open_position(
        self,
        st: State,
        direction: Side,
        spot_ltp: float,
        futures_ltp: float,
        timestamp: dt.datetime,
        reason: str = "ENTRY",
    ) -> Trade:
        assert direction in (Side.LONG, Side.SHORT)
        assert st.is_flat(), "open_position called with a live position"

        qty = self.cfg.base_qty
        tid = self.ledger.new_trade_id()

        st.position = direction
        st.qty = qty
        st.spot_entry_price = spot_ltp
        st.futures_entry_price = futures_ltp
        st.entry_timestamp = timestamp
        st.best_spot_price = spot_ltp
        st.worst_spot_price = spot_ltp
        st.mfe_points = 0.0
        st.mae_points = 0.0
        st.early_stop_active = True
        st.partial_done = False
        st.trade_id = tid
        st.entry_reason = reason
        st.overnight_flag = False
        st.breaker_triggered_this_trade = False
        st.gap_regime_flag_this_trade = False
        st.early_stop_count_this_trade = 0
        st.deferred_flip_active = False
        st.deferred_flip_price = None
        st.clear_pending()

        side = self.sim.side_for(direction, opening=True)
        self.sim.record(
            timestamp=timestamp,
            trade_id=tid,
            side=side,
            qty=qty,
            spot_signal_price=spot_ltp,
            futures_ltp=futures_ltp,
            reason=reason,
            position_after=int(direction),
        )

        trade = Trade(
            trade_id=tid,
            direction=int(direction),
            entry_datetime=timestamp,
            entry_spot=spot_ltp,
            entry_futures=self.sim.executions[-1].futures_fill_price,
            entry_qty=qty,
            entry_reason=reason,
            entry_trade_date=st.trade_date,
        )
        return self.ledger.open(trade)

    # ------------------------------------------------------------------
    # MFE / MAE (spec 7)
    # ------------------------------------------------------------------
    def update_best_price_and_mfe(self, st: State, spot_ltp: float) -> None:
        if st.is_flat():
            return
        r = st.r_points(spot_ltp)
        if r > st.mfe_points:
            st.mfe_points = r
            st.best_spot_price = spot_ltp
        if r < st.mae_points:
            st.mae_points = r
            st.worst_spot_price = spot_ltp

    # ------------------------------------------------------------------
    # Partial (spec 8.3)
    # ------------------------------------------------------------------
    def execute_partial(
        self, st: State, spot_ltp: float, futures_ltp: float, timestamp: dt.datetime
    ) -> None:
        assert not st.partial_done, "partial already taken for this trade"
        close_qty = self.cfg.partial_qty
        if close_qty <= 0 or close_qty >= st.qty:
            return

        side = self.sim.side_for(st.position, opening=False)
        ex = self.sim.record(
            timestamp=timestamp,
            trade_id=st.trade_id,
            side=side,
            qty=close_qty,
            spot_signal_price=spot_ltp,
            futures_ltp=futures_ltp,
            reason="PARTIAL",
            position_after=int(st.position),
        )

        trade = self.ledger.get(st.trade_id)
        trade.partial_datetime = timestamp
        trade.partial_spot = spot_ltp
        trade.partial_futures = ex.futures_fill_price
        trade.partial_qty = close_qty

        st.qty -= close_qty
        st.partial_done = True

    # ------------------------------------------------------------------
    # Close (spec 8.1, 8.2, 8.4, 10.2, 11)
    # ------------------------------------------------------------------
    def close_position(
        self,
        st: State,
        spot_ltp: float,
        futures_ltp: float,
        timestamp: dt.datetime,
        reason: str,
    ) -> Trade:
        assert not st.is_flat(), "close_position called while flat"

        direction = st.position
        qty = st.qty
        side = self.sim.side_for(direction, opening=False)
        ex = self.sim.record(
            timestamp=timestamp,
            trade_id=st.trade_id,
            side=side,
            qty=qty,
            spot_signal_price=spot_ltp,
            futures_ltp=futures_ltp,
            reason=reason,
            position_after=0,
        )

        trade = self.ledger.get(st.trade_id)
        trade.exit_datetime = timestamp
        trade.exit_spot = spot_ltp
        trade.exit_futures = ex.futures_fill_price
        trade.exit_qty = qty
        trade.exit_reason = reason
        trade.maximum_favourable_excursion = st.mfe_points
        trade.maximum_adverse_excursion = st.mae_points
        trade.early_stop_count = st.early_stop_count_this_trade
        trade.breaker_triggered = st.breaker_triggered_this_trade
        trade.gap_regime_flag = st.gap_regime_flag_this_trade
        trade.overnight_flag = st.overnight_flag
        trade.exit_trade_date = st.trade_date
        if not trade.deferred_unprotected_seconds:
            trade.deferred_unprotected_seconds = st.deferred_unprotected_seconds

        self.ledger.finalize(trade, self.sim.executions)

        best, worst = st.best_spot_price, st.worst_spot_price
        st.clear_position()
        st.last_trade_best = best       # consumed by SignalEngine seeding (R2)
        st.last_trade_worst = worst
        return trade

    # ------------------------------------------------------------------
    def reverse(
        self,
        st: State,
        spot_ltp: float,
        futures_ltp: float,
        timestamp: dt.datetime,
        close_reason: str,
    ) -> tuple[Trade, Trade]:
        """Spec 8.2: close old, open opposite at the same futures price."""
        old_direction = st.position
        closed = self.close_position(st, spot_ltp, futures_ltp, timestamp, close_reason)
        opened = self.open_position(
            st,
            old_direction.opposite,
            spot_ltp,
            futures_ltp,
            timestamp,
            reason=f"REVERSE_{close_reason}",
        )
        return closed, opened
