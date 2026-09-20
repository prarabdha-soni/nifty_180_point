"""
Market data loading and deterministic event ordering -- spec sections 3 and 14.

R8 -- spec 1 requires determinism ("the same input data and configuration
must produce the same trade ledger") while spec 3 only says to "merge by
timestamp while retaining event order". Two events sharing a timestamp have
no defined order, so the same file could produce two different ledgers.
V1 sorts by (timestamp, source_rank, original_index) with source_rank taken
from cfg.tie_break_order, default ("FUT", "SPOT") -- futures first, so the
spot tick that drives the state machine always sees the freshest futures
price. The sort is stable, so equal keys keep file order.

Supported input layouts
-----------------------
merged  one CSV, spec section 3's minimum record:
        timestamp, trade_date, spot_ltp, futures_ltp, futures_contract_expiry
        Each row is one synchronized snapshot and is processed as a spot tick
        carrying the concurrent futures price.

split   two CSVs, which is what most tick vendors actually ship:
        spot.csv     timestamp, ltp
        futures.csv  timestamp, ltp, expiry
        Interleaved into a single ordered event stream.

Session reference levels (previous_spot_high/low/close, previous_futures
high/low) are derived from the previous trading session present in the
dataset. The first session is a warm-up: references are built, no trading.
Pass a reference CSV to override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, List, Dict
import datetime as dt

import pandas as pd

from .config import Config
from .state import MarketSnapshot


@dataclass
class SessionReference:
    trade_date: dt.date
    prev_spot_high: Optional[float]
    prev_spot_low: Optional[float]
    prev_spot_close: Optional[float]
    prev_fut_high: Optional[float]
    prev_fut_low: Optional[float]
    contract_expiry: Optional[dt.date]
    is_expiry_day: bool
    is_warmup: bool


def _to_date(v) -> Optional[dt.date]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return pd.to_datetime(v).date()


class MarketData:
    """Holds an ordered event stream plus per-session reference levels."""

    def __init__(self, events: List[MarketSnapshot], cfg: Config):
        self.cfg = cfg
        self.events = events
        self.references: Dict[dt.date, SessionReference] = {}
        self._build_references()

    # ------------------------------------------------------------------
    @classmethod
    def from_merged_csv(cls, path: str, cfg: Config) -> "MarketData":
        df = pd.read_csv(path)
        required = {"timestamp", "spot_ltp", "futures_ltp"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"merged CSV missing columns: {sorted(missing)}")

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        if "trade_date" in df.columns:
            dates = pd.to_datetime(df["trade_date"]).dt.date
        else:
            dates = df["timestamp"].dt.date
        if "futures_contract_expiry" in df.columns:
            expiries = pd.to_datetime(df["futures_contract_expiry"]).dt.date
        else:
            expiries = pd.Series([None] * len(df))

        events = [
            MarketSnapshot(
                timestamp=ts.to_pydatetime(),
                trade_date=d,
                spot_ltp=float(s),
                futures_ltp=float(f),
                futures_contract_expiry=e,
                source="BOTH",
            )
            for ts, d, s, f, e in zip(
                df["timestamp"], dates, df["spot_ltp"], df["futures_ltp"], expiries
            )
        ]
        events = cls._order(events, cfg)
        return cls(events, cfg)

    # ------------------------------------------------------------------
    @classmethod
    def from_split_csv(
        cls, spot_path: str, futures_path: str, cfg: Config
    ) -> "MarketData":
        sdf = pd.read_csv(spot_path)
        fdf = pd.read_csv(futures_path)

        for name, d, cols in (("spot", sdf, {"timestamp", "ltp"}),
                              ("futures", fdf, {"timestamp", "ltp"})):
            missing = cols - set(d.columns)
            if missing:
                raise ValueError(f"{name} CSV missing columns: {sorted(missing)}")

        sdf["timestamp"] = pd.to_datetime(sdf["timestamp"])
        fdf["timestamp"] = pd.to_datetime(fdf["timestamp"])

        f_exp = (
            pd.to_datetime(fdf["expiry"]).dt.date
            if "expiry" in fdf.columns
            else pd.Series([None] * len(fdf))
        )

        events: List[MarketSnapshot] = []
        for ts, px in zip(sdf["timestamp"], sdf["ltp"]):
            events.append(MarketSnapshot(ts.to_pydatetime(), ts.date(), float(px),
                                         None, None, "SPOT"))
        for ts, px, e in zip(fdf["timestamp"], fdf["ltp"], f_exp):
            events.append(MarketSnapshot(ts.to_pydatetime(), ts.date(), None,
                                         float(px), e, "FUT"))

        events = cls._order(events, cfg)
        return cls(events, cfg)

    # ------------------------------------------------------------------
    @staticmethod
    def _order(events: List[MarketSnapshot], cfg: Config) -> List[MarketSnapshot]:
        """R8: deterministic total order."""
        rank = {src: i for i, src in enumerate(cfg.tie_break_order)}
        rank["BOTH"] = max(rank.values()) + 1
        indexed = list(enumerate(events))
        indexed.sort(key=lambda p: (p[1].timestamp, rank.get(p[1].source, 99), p[0]))
        return [e for _, e in indexed]

    # ------------------------------------------------------------------
    def _build_references(self) -> None:
        by_date: Dict[dt.date, dict] = {}
        for ev in self.events:
            d = ev.trade_date
            rec = by_date.setdefault(
                d,
                {"sh": None, "sl": None, "sc": None,
                 "fh": None, "fl": None, "exp": None},
            )
            if ev.spot_ltp is not None:
                rec["sh"] = ev.spot_ltp if rec["sh"] is None else max(rec["sh"], ev.spot_ltp)
                rec["sl"] = ev.spot_ltp if rec["sl"] is None else min(rec["sl"], ev.spot_ltp)
                rec["sc"] = ev.spot_ltp
            if ev.futures_ltp is not None:
                rec["fh"] = ev.futures_ltp if rec["fh"] is None else max(rec["fh"], ev.futures_ltp)
                rec["fl"] = ev.futures_ltp if rec["fl"] is None else min(rec["fl"], ev.futures_ltp)
            if ev.futures_contract_expiry is not None:
                rec["exp"] = ev.futures_contract_expiry

        dates = sorted(by_date)
        for i, d in enumerate(dates):
            prev = by_date[dates[i - 1]] if i > 0 else None
            exp = by_date[d]["exp"]
            self.references[d] = SessionReference(
                trade_date=d,
                prev_spot_high=prev["sh"] if prev else None,
                prev_spot_low=prev["sl"] if prev else None,
                prev_spot_close=prev["sc"] if prev else None,
                prev_fut_high=prev["fh"] if prev else None,
                prev_fut_low=prev["fl"] if prev else None,
                contract_expiry=exp,
                is_expiry_day=(exp is not None and exp == d),
                is_warmup=(i == 0 and self.cfg.warmup_first_session),
            )

    # ------------------------------------------------------------------
    def apply_reference_override(self, path: str) -> None:
        """Optional CSV: trade_date, prev_spot_high, prev_spot_low,
        prev_spot_close, prev_fut_high, prev_fut_low."""
        df = pd.read_csv(path)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for _, row in df.iterrows():
            d = row["trade_date"]
            if d not in self.references:
                continue
            ref = self.references[d]
            for col, attr in (
                ("prev_spot_high", "prev_spot_high"),
                ("prev_spot_low", "prev_spot_low"),
                ("prev_spot_close", "prev_spot_close"),
                ("prev_fut_high", "prev_fut_high"),
                ("prev_fut_low", "prev_fut_low"),
            ):
                if col in df.columns and not pd.isna(row[col]):
                    setattr(ref, attr, float(row[col]))
            ref.is_warmup = False

    # ------------------------------------------------------------------
    def sessions(self) -> Iterator[tuple[dt.date, List[MarketSnapshot]]]:
        current: Optional[dt.date] = None
        bucket: List[MarketSnapshot] = []
        for ev in self.events:
            if ev.trade_date != current:
                if current is not None:
                    yield current, bucket
                current, bucket = ev.trade_date, []
            bucket.append(ev)
        if current is not None:
            yield current, bucket


# ---------------------------------------------------------------------------
# Synthetic tick generator -- used by the validation scenario tests
# ---------------------------------------------------------------------------

def synth_events(
    trade_date: dt.date,
    spot_path: List[float],
    futures_path: Optional[List[float]] = None,
    start: str = "09:16:00",
    step_seconds: int = 1,
    expiry: Optional[dt.date] = None,
) -> List[MarketSnapshot]:
    """Build a merged-format event list walking spot (and futures) price by price."""
    if futures_path is None:
        futures_path = list(spot_path)
    if len(futures_path) != len(spot_path):
        raise ValueError("spot_path and futures_path must be the same length")

    h, m, s = (int(x) for x in start.split(":"))
    t0 = dt.datetime.combine(trade_date, dt.time(h, m, s))
    return [
        MarketSnapshot(
            timestamp=t0 + dt.timedelta(seconds=i * step_seconds),
            trade_date=trade_date,
            spot_ltp=float(sp),
            futures_ltp=float(fp),
            futures_contract_expiry=expiry,
            source="BOTH",
        )
        for i, (sp, fp) in enumerate(zip(spot_path, futures_path))
    ]
