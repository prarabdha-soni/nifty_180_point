"""
Execution simulation and transaction cost -- spec section 12.

Spec 12.1:
    transaction_cost = abs(executed_qty) * futures_fill_price * 0.00015
    "Calculate cost on every actual simulated futures execution. Do not
     deduct a fixed number of points per trade."

R9 (DECISIONS.md): the spec fills at futures_ltp with no slippage. V1 keeps
slippage at 0.0 so the baseline ledger is the spec's, but the parameter is
wired in from day one so V2 can measure its impact without a code change.

R10: stt_sell_side_pct is also 0.0 in V1. Real STT on index futures applies
to the sell side only, which makes true cost asymmetric. Left off so the
V1 control model matches the spec exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import datetime as dt

from .config import Config
from .state import Side


@dataclass
class Execution:
    """One simulated futures fill. Spec 18 requires every field here."""
    timestamp: dt.datetime
    trade_id: int
    side: str                # "BUY" or "SELL"
    qty: int
    spot_signal_price: float
    futures_reference_price: float   # observed LTP before slippage
    futures_fill_price: float        # price actually used for P&L
    reason: str
    transaction_cost: float
    slippage_cost: float
    position_after: int


class ExecutionSimulator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.executions: list[Execution] = []

    # ------------------------------------------------------------------
    def fill_price(self, futures_ltp: float, side: str) -> float:
        """Slippage always works against the trader."""
        slip = self.cfg.slippage_points_per_side
        if slip == 0.0:
            return futures_ltp
        return futures_ltp + slip if side == "BUY" else futures_ltp - slip

    def cost_for(self, qty: int, fill_price: float, side: str) -> float:
        qty = abs(qty)
        cost = qty * fill_price * self.cfg.transaction_cost_per_side
        if side == "SELL" and self.cfg.stt_sell_side_pct:
            cost += qty * fill_price * self.cfg.stt_sell_side_pct
        return cost

    # ------------------------------------------------------------------
    def record(
        self,
        *,
        timestamp: dt.datetime,
        trade_id: int,
        side: str,
        qty: int,
        spot_signal_price: float,
        futures_ltp: float,
        reason: str,
        position_after: int,
    ) -> Execution:
        assert side in ("BUY", "SELL"), side
        assert qty > 0, f"non-positive execution qty {qty}"

        fill = self.fill_price(futures_ltp, side)
        cost = self.cost_for(qty, fill, side)
        slip_cost = abs(fill - futures_ltp) * qty

        ex = Execution(
            timestamp=timestamp,
            trade_id=trade_id,
            side=side,
            qty=qty,
            spot_signal_price=spot_signal_price,
            futures_reference_price=futures_ltp,
            futures_fill_price=fill,
            reason=reason,
            transaction_cost=cost,
            slippage_cost=slip_cost,
            position_after=position_after,
        )
        self.executions.append(ex)
        return ex

    # ------------------------------------------------------------------
    @staticmethod
    def side_for(direction: Side, opening: bool) -> str:
        """Opening a LONG buys; closing a LONG sells."""
        if opening:
            return "BUY" if direction == Side.LONG else "SELL"
        return "SELL" if direction == Side.LONG else "BUY"
