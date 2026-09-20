"""
Trade ledger and P&L accounting -- spec sections 12 and 15.1.

Spec 12.2:
    gross_pnl = direction * matched_qty * (futures_exit_price - futures_entry_price)
    net_pnl   = gross_pnl - sum(all_execution_transaction_costs)
    "Partial exits should be matched against the original entry quantity.
     A reversal creates a realized close of the old position and a new
     opposite entry."

Spec 18 requires trade-level gross, cost and net to reconcile to
strategy-level totals. reconcile() asserts that.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, List
import datetime as dt

from .state import Side
from .execution import Execution


@dataclass
class Trade:
    trade_id: int
    direction: int
    entry_datetime: dt.datetime
    entry_spot: float
    entry_futures: float
    entry_qty: int
    entry_reason: str = "ENTRY"

    partial_datetime: Optional[dt.datetime] = None
    partial_spot: Optional[float] = None
    partial_futures: Optional[float] = None
    partial_qty: int = 0

    exit_datetime: Optional[dt.datetime] = None
    exit_spot: Optional[float] = None
    exit_futures: Optional[float] = None
    exit_qty: int = 0
    exit_reason: Optional[str] = None

    gross_pnl: float = 0.0
    transaction_cost: float = 0.0
    net_pnl: float = 0.0

    maximum_favourable_excursion: float = 0.0
    maximum_adverse_excursion: float = 0.0
    holding_time: Optional[float] = None          # seconds
    early_stop_count: int = 0
    breaker_triggered: bool = False
    gap_regime_flag: bool = False
    overnight_flag: bool = False

    # analytics extras (not in spec 15.1, needed for the reviews you asked for)
    deferred_unprotected_seconds: float = 0.0
    sessions_spanned: int = 1
    entry_trade_date: Optional[dt.date] = None
    exit_trade_date: Optional[dt.date] = None

    @property
    def is_closed(self) -> bool:
        return self.exit_datetime is not None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["direction"] = "LONG" if self.direction == 1 else "SHORT"
        return d


class TradeLedger:
    def __init__(self) -> None:
        self.trades: List[Trade] = []
        self._by_id: dict[int, Trade] = {}
        self._next_id = 1

    # ------------------------------------------------------------------
    def new_trade_id(self) -> int:
        tid = self._next_id
        self._next_id += 1
        return tid

    def open(self, trade: Trade) -> Trade:
        self.trades.append(trade)
        self._by_id[trade.trade_id] = trade
        return trade

    def get(self, trade_id: int) -> Trade:
        return self._by_id[trade_id]

    # ------------------------------------------------------------------
    def finalize(self, trade: Trade, executions: List[Execution]) -> None:
        """Compute gross, cost and net once the trade is closed."""
        d = trade.direction
        gross = 0.0
        if trade.partial_qty:
            gross += d * trade.partial_qty * (trade.partial_futures - trade.entry_futures)
        if trade.exit_qty:
            gross += d * trade.exit_qty * (trade.exit_futures - trade.entry_futures)

        cost = sum(e.transaction_cost for e in executions if e.trade_id == trade.trade_id)

        trade.gross_pnl = gross
        trade.transaction_cost = cost
        trade.net_pnl = gross - cost

        if trade.exit_datetime and trade.entry_datetime:
            trade.holding_time = (trade.exit_datetime - trade.entry_datetime).total_seconds()

    # ------------------------------------------------------------------
    def reconcile(self, executions: List[Execution], tol: float = 1e-6) -> dict:
        """Spec 18: trade-level totals must reconcile to strategy-level totals."""
        closed = [t for t in self.trades if t.is_closed]
        trade_gross = sum(t.gross_pnl for t in closed)
        trade_cost = sum(t.transaction_cost for t in closed)
        trade_net = sum(t.net_pnl for t in closed)

        closed_ids = {t.trade_id for t in closed}
        exec_cost = sum(e.transaction_cost for e in executions if e.trade_id in closed_ids)

        ok_cost = abs(trade_cost - exec_cost) < tol
        ok_net = abs(trade_net - (trade_gross - trade_cost)) < tol

        # quantity conservation: every closed trade must be fully unwound
        ok_qty = all(t.partial_qty + t.exit_qty == t.entry_qty for t in closed)

        return {
            "trades_closed": len(closed),
            "trade_gross_pnl": trade_gross,
            "trade_transaction_cost": trade_cost,
            "trade_net_pnl": trade_net,
            "execution_transaction_cost": exec_cost,
            "cost_reconciles": ok_cost,
            "net_reconciles": ok_net,
            "quantity_reconciles": ok_qty,
            "all_ok": ok_cost and ok_net and ok_qty,
        }
