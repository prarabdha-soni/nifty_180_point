"""
Test harness: run a synthetic tick path through the engine with explicit
session reference levels, so a scenario does not need a warm-up session.

Reference levels used by most scenarios (chosen so that neither trigger is
already live on the first tick, which requires the previous-session range to
be narrower than 2*D):

    prev_spot_high  24500      spot_short_trigger  24320
    prev_spot_low   24200      spot_long_trigger   24380
    prev_spot_close 24350
    prev_fut_high   24520      fut_short_trigger   24340
    prev_fut_low    24220      fut_long_trigger    24400
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Sequence

from strategy.config import Config
from strategy.data import MarketData, SessionReference, synth_events
from strategy.backtester import Backtester, BacktestResult


PREV_SPOT_HIGH = 24500.0
PREV_SPOT_LOW = 24200.0
PREV_SPOT_CLOSE = 24350.0
PREV_FUT_HIGH = 24520.0
PREV_FUT_LOW = 24220.0

D1 = dt.date(2026, 1, 5)
D2 = dt.date(2026, 1, 6)


@dataclass
class Session:
    date: dt.date
    spot: Sequence[float]
    futures: Optional[Sequence[float]] = None
    start: str = "09:16:00"
    step_seconds: int = 60
    expiry: Optional[dt.date] = None
    # reference overrides for this session
    prev_spot_high: Optional[float] = PREV_SPOT_HIGH
    prev_spot_low: Optional[float] = PREV_SPOT_LOW
    prev_spot_close: Optional[float] = PREV_SPOT_CLOSE
    prev_fut_high: Optional[float] = PREV_FUT_HIGH
    prev_fut_low: Optional[float] = PREV_FUT_LOW
    is_expiry_day: bool = False


def fut_from_spot(spot: Sequence[float], basis: float = 20.0) -> List[float]:
    return [p + basis for p in spot]


def build_market(sessions: List[Session], cfg: Config) -> MarketData:
    events = []
    for s in sessions:
        events += synth_events(
            s.date,
            list(s.spot),
            list(s.futures) if s.futures is not None else fut_from_spot(s.spot),
            start=s.start,
            step_seconds=s.step_seconds,
            expiry=s.expiry,
        )
    md = MarketData(MarketData._order(events, cfg), cfg)
    for s in sessions:
        ref = md.references[s.date]
        ref.prev_spot_high = s.prev_spot_high
        ref.prev_spot_low = s.prev_spot_low
        ref.prev_spot_close = s.prev_spot_close
        ref.prev_fut_high = s.prev_fut_high
        ref.prev_fut_low = s.prev_fut_low
        ref.is_warmup = False
        if s.is_expiry_day:
            ref.is_expiry_day = True
            ref.contract_expiry = s.date
    return md


def run(sessions: List[Session], cfg: Optional[Config] = None):
    """Scenario runs keep the terminal position open so it can be asserted on."""
    cfg = cfg or Config(warmup_first_session=False, close_at_dataset_end=False)
    md = build_market(sessions, cfg)
    bt = Backtester(cfg)
    result = bt.run(md)
    return bt, result


def run_path(
    spot: Sequence[float],
    futures: Optional[Sequence[float]] = None,
    cfg: Optional[Config] = None,
    **kw,
):
    """Single-session convenience wrapper."""
    return run([Session(D1, spot, futures, **kw)], cfg)


def closed_trades(result: BacktestResult, include_dataset_end: bool = False):
    return [
        t for t in result.trades
        if t.is_closed and (include_dataset_end or t.exit_reason != "DATASET_END")
    ]


def reasons(result: BacktestResult) -> List[str]:
    return [t.exit_reason for t in result.trades if t.is_closed]


def event_kinds(result: BacktestResult) -> List[str]:
    return [e["event"] for e in result.events_log]
