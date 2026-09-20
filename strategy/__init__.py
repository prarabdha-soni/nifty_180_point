"""NIFTY 180-Point Swing Strategy -- V1 backtest engine."""
from .config import Config, DEFAULT_CONFIG
from .state import State, Side, PendingType
from .backtester import Backtester, BacktestResult
from .data import MarketData, synth_events
from .metrics import compute_metrics, format_metrics

__all__ = [
    "Config", "DEFAULT_CONFIG", "State", "Side", "PendingType",
    "Backtester", "BacktestResult", "MarketData", "synth_events",
    "compute_metrics", "format_metrics",
]
__version__ = "1.0.0"
