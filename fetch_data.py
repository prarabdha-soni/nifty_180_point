#!/usr/bin/env python3
"""
fetch_data.py -- NIFTY 50 spot + NIFTY near-month futures, 1-minute bars, from
Angel One SmartAPI, written in the engine's merged CSV layout (README "Data
contract"):

    timestamp,trade_date,spot_ltp,futures_ltp,futures_contract_expiry
    2026-01-05 09:16:00,2026-01-05,24350.10,24371.25,2026-01-29

Usage
-----
    cp .env.example .env            # fill in the four ANGEL_* values
    pip install -r requirements-fetch.txt
    python fetch_data.py --out data/nifty_3y.csv                 # last 3 years
    python fetch_data.py --start 2023-09-20 --end 2026-09-20     # explicit
    python fetch_data.py --offline                               # rebuild from cache only

KNOWN LIMITATION -- read before trusting the futures column
-----------------------------------------------------------
Angel One's historical API serves candles only for contracts that are
currently LIVE. Expired NFO contracts are not stored and the scrip master
carries no tokens for them (confirmed by an Angel One admin on the SmartAPI
forum). Consequently:

  * spot   : full requested range (index history is available)
  * futures: only the ~3 currently listed monthly contracts, each from its
             listing date. As the strict NEAR-MONTH contract, that is only the
             current near month from the day after the previous expiry -- i.e.
             a few weeks. `--nearest-available` widens coverage to ~3 months by
             using, for dates when the true near month has expired, the nearest
             still-listed contract (a real contract with its real expiry, but a
             next- or far-month one at the time).

The merged file only contains minutes where BOTH sides exist. The full spot
series is written separately (`--spot-out`) so it can be re-aligned later
against per-contract futures from another source.

Bar treatment (adopted 2026-09-20): each 1-minute bar is expanded to four ticks
(open, low/high, high/low, close) so the engine sees the true opening print and
the bar's extremes. Close-only bars flipped the sign of the in-sample result;
see expand_ohlc() and CLAUDE.md. `--ohlc-order favourable-first` gives the
other bound of the intra-bar path uncertainty; report both.

Roll convention: the expiring contract is used THROUGH its expiry day; the next
contract starts the following session. The engine's 15:00 expiry square-off
(is_expiry_day = expiry == trade_date) depends on this.

Engineering
-----------
  * credentials from .env (python-dotenv); never logged, never on the CLI
  * every API chunk cached as JSON under --cache-dir; re-runs skip cached
    chunks, so a failed run resumes where it stopped
  * ONE_MINUTE requests are capped at 30 days by the API; --chunk-days splits
  * rate limit: min spacing between calls + exponential backoff on "rate" /
    "access denied" errors (the forum reports false positives well under the
    published 3/s, 180/min)
  * progress logged to stderr and --log-file
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

LOG = logging.getLogger("fetch_data")

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
API_DATE_FMT = "%Y-%m-%d %H:%M"        # SmartAPI fromdate/todate
OUT_TS_FMT = "%Y-%m-%d %H:%M:%S"       # engine merged CSV
SESSION_START = dt.time(9, 15)
SESSION_LAST_BAR = dt.time(15, 29)     # bar-open time of the last 1-min bar
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@dataclass
class Credentials:
    api_key: str
    client_code: str
    mpin: str
    totp_secret: str

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "Credentials":
        try:
            from dotenv import load_dotenv
        except ImportError as e:  # pragma: no cover
            raise FetchError("pip install python-dotenv (see requirements-fetch.txt)") from e
        load_dotenv(env_path)
        vals = {}
        for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET"):
            v = os.environ.get(k, "").strip()
            if not v:
                raise FetchError(f"{k} is not set -- copy .env.example to .env and fill it in")
            vals[k] = v
        return cls(vals["ANGEL_API_KEY"], vals["ANGEL_CLIENT_CODE"],
                   vals["ANGEL_MPIN"], vals["ANGEL_TOTP_SECRET"])


# ---------------------------------------------------------------------------
# Angel One client -- thin, with retry / backoff / spacing
# ---------------------------------------------------------------------------
class AngelClient:
    RATE_LIMIT_MARKERS = ("rate", "access denied", "too many", "ab1004", "ab2001")

    def __init__(self, creds: Credentials, min_interval: float = 0.4,
                 max_retries: int = 6):
        self.creds = creds
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_call = 0.0
        self._api = None

    def login(self) -> None:
        try:
            from SmartApi import SmartConnect  # smartapi-python
            import pyotp
        except ImportError as e:
            raise FetchError("pip install -r requirements-fetch.txt") from e
        api = SmartConnect(api_key=self.creds.api_key)
        totp = pyotp.TOTP(self.creds.totp_secret).now()
        resp = api.generateSession(self.creds.client_code, self.creds.mpin, totp)
        if not resp or not resp.get("status"):
            raise FetchError(f"login failed: {resp.get('message') if resp else 'no response'}")
        self._api = api
        LOG.info("logged in")

    def _space(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def candles(self, exchange: str, token: str, start: dt.datetime,
                end: dt.datetime) -> List[list]:
        """ONE_MINUTE candles [ts, o, h, l, c, v]. Empty list = no data."""
        if self._api is None:
            raise FetchError("not logged in")
        params = {
            "exchange": exchange,
            "symboltoken": str(token),
            "interval": "ONE_MINUTE",
            "fromdate": start.strftime(API_DATE_FMT),
            "todate": end.strftime(API_DATE_FMT),
        }
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            self._space()
            try:
                resp = self._api.getCandleData(params)
            except Exception as e:  # network / SDK error
                resp = {"status": False, "message": f"exception: {e}", "errorcode": "EXC"}
            if resp and resp.get("status"):
                return resp.get("data") or []
            msg = f"{(resp or {}).get('errorcode')}: {(resp or {}).get('message')}"
            low = msg.lower()
            # SmartAPI answers 'no data' as an error for holiday ranges
            if "no data" in low or "nodata" in low:
                return []
            if any(m in low for m in self.RATE_LIMIT_MARKERS) or "exception" in low:
                LOG.warning("attempt %d/%d failed (%s); backing off %.0fs",
                            attempt, self.max_retries, msg, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise FetchError(f"getCandleData failed for {params}: {msg}")
        raise FetchError(f"giving up after {self.max_retries} attempts: {params}")


# ---------------------------------------------------------------------------
# Scrip master
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Contract:
    token: str
    symbol: str
    exchange: str
    expiry: dt.date

    @property
    def label(self) -> str:
        return f"{self.symbol}_{self.expiry.isoformat()}"


class ScripMaster:
    def __init__(self, rows: List[dict]):
        self.rows = rows

    @classmethod
    def load(cls, cache_dir: str, offline: bool, max_age_days: int = 1) -> "ScripMaster":
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, "scrip_master.json")
        fresh = (os.path.exists(path)
                 and (time.time() - os.path.getmtime(path)) < max_age_days * 86400)
        if not fresh and not offline:
            import requests
            LOG.info("downloading scrip master ...")
            r = requests.get(SCRIP_MASTER_URL, timeout=60)
            r.raise_for_status()
            with open(path, "w") as fh:
                fh.write(r.text)
        elif not os.path.exists(path):
            raise FetchError("offline and no cached scrip master")
        with open(path) as fh:
            rows = json.load(fh)
        LOG.info("scrip master: %d instruments (%s)", len(rows), path)
        return cls(rows)

    @staticmethod
    def _parse_expiry(s: str) -> Optional[dt.date]:
        # "30OCT2025"
        try:
            return dt.datetime.strptime(s.strip().upper(), "%d%b%Y").date()
        except Exception:
            return None

    def nifty_spot(self) -> Contract:
        for r in self.rows:
            if r.get("exch_seg") == "NSE" and r.get("symbol", "").lower() == "nifty 50":
                return Contract(str(r["token"]), "NIFTY50", "NSE", dt.date.max)
        # documented token for the index
        LOG.warning("Nifty 50 not found in scrip master; using documented token 99926000")
        return Contract("99926000", "NIFTY50", "NSE", dt.date.max)

    def nifty_futures(self) -> List[Contract]:
        out = []
        for r in self.rows:
            if (r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "FUTIDX"
                    and r.get("name") == "NIFTY"):
                exp = self._parse_expiry(r.get("expiry", ""))
                if exp:
                    out.append(Contract(str(r["token"]), r["symbol"], "NFO", exp))
        out.sort(key=lambda c: c.expiry)
        return out


# ---------------------------------------------------------------------------
# Chunk cache
# ---------------------------------------------------------------------------
class ChunkCache:
    def __init__(self, root: str):
        self.root = root

    def path(self, contract: Contract, start: dt.date, end: dt.date) -> str:
        d = os.path.join(self.root, f"{contract.exchange}_{contract.token}_{contract.label}")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{start.isoformat()}_{end.isoformat()}.json")

    def get(self, contract: Contract, start: dt.date, end: dt.date,
            allow_stale: bool = False) -> Optional[dict]:
        p = self.path(contract, start, end)
        if not os.path.exists(p):
            return None
        try:
            with open(p) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            LOG.warning("unreadable cache file %s -- refetching", p)
            return None
        if not rec.get("final", False):
            if allow_stale:
                LOG.warning("[%s] %s..%s using a non-final cached chunk (fetched %s)",
                            contract.label, start, end, rec.get("fetched_at"))
                return rec
            return None            # partial (touched today) -> refetch
        return rec

    def invalidate_from(self, contract: Contract, since: dt.date) -> int:
        """Delete cached chunks that overlap [since, ...) so they are refetched."""
        d = os.path.join(self.root, f"{contract.exchange}_{contract.token}_{contract.label}")
        if not os.path.isdir(d):
            return 0
        n = 0
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            try:
                _, to_s = name[:-5].split("_")
                if dt.date.fromisoformat(to_s) >= since:
                    os.remove(os.path.join(d, name))
                    n += 1
            except ValueError:
                continue
        return n

    def contracts_on_disk(self, exchange: str = "NFO") -> List[Contract]:
        """Contracts that have cached chunks -- including ones that have since
        expired and vanished from the scrip master. This is how history
        accumulates: run the fetcher regularly and every contract that was ever
        live stays available for stitching."""
        out = []
        if not os.path.isdir(self.root):
            return out
        for name in os.listdir(self.root):
            parts = name.split("_")
            if len(parts) != 4 or parts[0] != exchange:
                continue
            try:
                out.append(Contract(parts[1], parts[2], parts[0], dt.date.fromisoformat(parts[3])))
            except ValueError:
                continue
        return out

    def put(self, contract: Contract, start: dt.date, end: dt.date,
            data: List[list], final: bool) -> None:
        rec = {
            "exchange": contract.exchange, "token": contract.token,
            "label": contract.label, "from": start.isoformat(), "to": end.isoformat(),
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "final": final, "data": data,
        }
        p = self.path(contract, start, end)
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, p)         # atomic: a crash mid-write leaves no bad file


# ---------------------------------------------------------------------------
# Fetching one series in chunks
# ---------------------------------------------------------------------------
CHUNK_EPOCH = dt.date(2000, 1, 1)


def chunk_ranges(start: dt.date, end: dt.date, days: int) -> List[Tuple[dt.date, dt.date]]:
    """Fixed calendar blocks of `days` anchored at CHUNK_EPOCH, covering
    [start, end]. Blocks are never clipped, so the cache key of a block does
    not depend on the range asked for and any later run reuses it."""
    first = (start - CHUNK_EPOCH).days // days
    last = (end - CHUNK_EPOCH).days // days
    return [(CHUNK_EPOCH + dt.timedelta(days=i * days),
             CHUNK_EPOCH + dt.timedelta(days=(i + 1) * days - 1)) for i in range(first, last + 1)]


def candles_to_frame(data: Sequence[list]) -> pd.DataFrame:
    if not data:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(list(data), columns=["timestamp", "open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    df["timestamp"] = ts
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_series(client: Optional[AngelClient], cache: ChunkCache, contract: Contract,
                 start: dt.date, end: dt.date, chunk_days: int, offline: bool,
                 today: dt.date) -> pd.DataFrame:
    chunks = chunk_ranges(start, end, chunk_days)
    frames, n_cached, n_fetched, n_skipped = [], 0, 0, 0
    for i, (a, b) in enumerate(chunks, 1):
        rec = cache.get(contract, a, b, allow_stale=offline)
        if rec is not None:
            n_cached += 1
            src = "cache"
            data = rec["data"]
        elif offline or client is None:
            n_skipped += 1
            LOG.warning("[%s] chunk %d/%d %s..%s not in cache (offline) -- skipped",
                        contract.label, i, len(chunks), a, b)
            continue
        else:
            data = client.candles(contract.exchange, contract.token,
                                  dt.datetime.combine(a, SESSION_START),
                                  dt.datetime.combine(b, dt.time(15, 30)))
            final = b < today                       # today's bars may still grow
            cache.put(contract, a, b, data, final)
            n_fetched += 1
            src = "api"
        LOG.info("[%s] chunk %d/%d %s..%s -> %5d bars (%s)",
                 contract.label, i, len(chunks), a, b, len(data), src)
        frames.append(candles_to_frame(data))
    LOG.info("[%s] done: %d chunks cached, %d fetched, %d skipped",
             contract.label, n_cached, n_fetched, n_skipped)
    frames = [f for f in frames if len(f)]
    if not frames:
        return candles_to_frame([])
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    d = df["timestamp"].dt.date
    return df[(d >= start) & (d <= end)].reset_index(drop=True)   # blocks overhang the range


# ---------------------------------------------------------------------------
# Near-month stitching
# ---------------------------------------------------------------------------
def infer_previous_expiry(first: dt.date, others: Sequence[dt.date]) -> dt.date:
    """The expiry before the earliest live contract is not in the scrip master.
    Infer it as the last <same weekday> of the previous calendar month, which is
    the exchange rule (holidays can shift the true date earlier by a day or two;
    override with --near-month-from if you know it)."""
    weekday = first.weekday()
    prev_month_end = first.replace(day=1) - dt.timedelta(days=1)
    d = prev_month_end
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return d


def assign_contracts(fut_by_contract: Dict[Contract, pd.DataFrame], contracts: List[Contract],
                     nearest_available: bool, near_month_from: Optional[dt.date]
                     ) -> Tuple[pd.DataFrame, dict]:
    """Return one futures frame with the contract chosen per trade date.

    Roll AFTER expiry day: on date d the eligible contracts are those with
    expiry >= d, and the one with the smallest expiry is used. So the expiring
    contract is still used on its own expiry day and the next one starts the
    following session.
    """
    if not contracts:
        return pd.DataFrame(columns=["timestamp", "futures_ltp", "futures_contract_expiry",
                                     "contract", "assignment"]), {}
    expiries = [c.expiry for c in contracts]
    first_valid = near_month_from or (infer_previous_expiry(expiries[0], expiries[1:])
                                      + dt.timedelta(days=1))
    parts, stats = [], {"strict_near_month_rows": 0, "nearest_available_rows": 0,
                        "dropped_pre_near_month_rows": 0, "per_contract": {}}
    for c in contracts:
        df = fut_by_contract.get(c)
        if df is None or df.empty:
            stats["per_contract"][c.label] = {"bars": 0}
            continue
        dates = df["timestamp"].dt.date
        # chosen contract for each date: smallest expiry >= d
        chosen = []
        for d in dates:
            elig = [e for e in expiries if e >= d]
            chosen.append(min(elig) if elig else None)
        chosen = pd.Series(chosen, index=df.index)
        is_this = chosen == c.expiry
        is_near = is_this & (dates >= first_valid)
        keep = is_near | (is_this & nearest_available)
        sub = df.loc[keep, ["timestamp", "open", "high", "low", "close"]].copy()
        sub["assignment"] = "NEAR_MONTH"
        sub.loc[~is_near[keep], "assignment"] = "NEAREST_AVAILABLE"
        sub["futures_contract_expiry"] = c.expiry
        sub["contract"] = c.label
        n_near = int(is_near.sum())
        n_fallback = int((keep & ~is_near).sum())
        n_dropped = int((is_this & ~keep).sum())
        stats["strict_near_month_rows"] += n_near
        stats["nearest_available_rows"] += n_fallback
        stats["dropped_pre_near_month_rows"] += n_dropped
        stats["per_contract"][c.label] = {
            "bars": int(len(df)), "first_bar": str(df["timestamp"].iloc[0]),
            "last_bar": str(df["timestamp"].iloc[-1]), "expiry": c.expiry.isoformat(),
            "near_month_rows": n_near, "nearest_available_rows": n_fallback,
            "dropped_rows": n_dropped,
        }
        parts.append(sub)
    stats["near_month_first_valid_date"] = first_valid.isoformat()
    stats["near_month_first_valid_source"] = "override" if near_month_from else "inferred"
    if not parts:
        return pd.DataFrame(columns=["timestamp", "futures_ltp", "futures_contract_expiry",
                                     "contract", "assignment"]), stats
    out = pd.concat(parts, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    return out, stats


# ---------------------------------------------------------------------------
# Alignment and output
# ---------------------------------------------------------------------------
def session_filter(df: pd.DataFrame) -> pd.DataFrame:
    t = df["timestamp"].dt.time
    return df[(t >= SESSION_START) & (t <= SESSION_LAST_BAR)]


OHLC = ["open", "high", "low", "close"]


def align(spot: pd.DataFrame, fut: pd.DataFrame, price_field: str) -> Tuple[pd.DataFrame, dict]:
    spot = session_filter(spot)
    fut = session_filter(fut)
    s = spot[["timestamp"] + OHLC].rename(columns={c: f"s_{c}" for c in OHLC})
    f = fut[["timestamp"] + OHLC + ["futures_contract_expiry", "contract", "assignment"]
            ].rename(columns={c: f"f_{c}" for c in OHLC})
    merged = s.merge(f, on="timestamp", how="inner")
    merged["spot_ltp"] = merged[f"s_{price_field}"]
    merged["futures_ltp"] = merged[f"f_{price_field}"]
    s_only = set(s["timestamp"]) - set(f["timestamp"])
    f_only = set(f["timestamp"]) - set(s["timestamp"])
    drop: Dict[str, Dict[str, int]] = {}
    for label, ts_set in (("spot_only", s_only), ("futures_only", f_only)):
        by_day: Dict[str, int] = {}
        for ts in ts_set:
            by_day[str(ts.date())] = by_day.get(str(ts.date()), 0) + 1
        drop[label] = dict(sorted(by_day.items()))
    spot_days = set(s["timestamp"].dt.date)
    merged_days = set(merged["timestamp"].dt.date)
    uncovered_days = spot_days - merged_days
    s_only_covered = [ts for ts in s_only if ts.date() in merged_days]
    report = {
        "spot_minutes_in_session": int(len(s)),
        "futures_minutes_in_session": int(len(f)),
        "aligned_minutes": int(len(merged)),
        "aligned_sessions": len(merged_days),
        "spot_sessions_with_no_futures_at_all": len(uncovered_days),
        "spot_minutes_in_uncovered_sessions": len(s_only) - len(s_only_covered),
        "dropped_spot_only_minutes_in_covered_sessions": len(s_only_covered),
        "dropped_futures_only_minutes": len(f_only),
        "dropped_by_session": {k: {d: n for d, n in v.items() if k == "futures_only" or d in
                                   {str(x) for x in merged_days}} for k, v in drop.items()},
    }
    merged["trade_date"] = merged["timestamp"].dt.date
    merged = merged[["timestamp", "trade_date", "spot_ltp", "futures_ltp",
                     "futures_contract_expiry", "contract", "assignment"]
                    + [f"s_{c}" for c in OHLC] + [f"f_{c}" for c in OHLC]]
    return merged.sort_values("timestamp").reset_index(drop=True), report


def expand_ohlc(merged: pd.DataFrame, order: str = "adverse-first") -> pd.DataFrame:
    """Turn each 1-minute bar into four ticks so the engine sees the bar's
    extremes and its true opening print, not just the close:

        :00  open
        :15  low  (up bar)  / high (down bar)      -- adverse-first convention
        :30  high (up bar)  / low  (down bar)
        :45  close

    The real order of high and low inside a bar is unknown; assuming the
    adverse move comes first is the usual conservative choice. The spot bar's
    direction decides the order for both instruments so each row stays a
    consistent snapshot. Timestamps get 0/15/30/45-second offsets, which keeps
    them unique and inside the same minute.
    """
    up = merged["s_close"] >= merged["s_open"]
    if order == "favourable-first":
        up = ~up                  # flip the convention: up bars go O-H-L-C
    legs = [
        (0, "open", "open"),
        (15, "low", "high"),      # up bars: low first; down bars: high first
        (30, "high", "low"),
        (45, "close", "close"),
    ]
    parts = []
    for offset, up_field, down_field in legs:
        part = merged[["timestamp", "trade_date", "futures_contract_expiry",
                       "contract", "assignment"]].copy()
        part["timestamp"] = part["timestamp"] + pd.Timedelta(seconds=offset)
        s_field = pd.Series(up_field, index=merged.index).where(up, down_field)
        part["spot_ltp"] = [merged.at[i, f"s_{fld}"] for i, fld in zip(merged.index, s_field)]
        part["futures_ltp"] = [merged.at[i, f"f_{fld}"] for i, fld in zip(merged.index, s_field)]
        parts.append(part)
    out = pd.concat(parts).sort_values("timestamp").reset_index(drop=True)
    return out[["timestamp", "trade_date", "spot_ltp", "futures_ltp",
                "futures_contract_expiry", "contract", "assignment"]]


def write_merged(df: pd.DataFrame, path: str) -> None:
    out = df[["timestamp", "trade_date", "spot_ltp", "futures_ltp", "futures_contract_expiry"]].copy()
    out["timestamp"] = out["timestamp"].dt.strftime(OUT_TS_FMT)
    out["trade_date"] = out["trade_date"].astype(str)
    out["futures_contract_expiry"] = out["futures_contract_expiry"].astype(str)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    out.to_csv(path, index=False, float_format="%.2f")


# ---------------------------------------------------------------------------
def setup_logging(log_file: Optional[str]) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, datefmt="%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    today = dt.date.today()
    ap.add_argument("--start", type=dt.date.fromisoformat,
                    default=today.replace(year=today.year - 3))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=today)
    ap.add_argument("--out", default="data/nifty_3y.csv", help="merged CSV for the engine")
    ap.add_argument("--spot-out", default="data/nifty_spot_1min.csv",
                    help="full spot series (timestamp,ltp) -- kept even where futures are missing")
    ap.add_argument("--fut-out", default="data/nifty_fut_1min.csv",
                    help="stitched per-contract futures (timestamp,ltp,expiry,contract,assignment)")
    ap.add_argument("--report", default="data/fetch_report.json")
    ap.add_argument("--cache-dir", default="data/raw/angel")
    ap.add_argument("--log-file", default="data/raw/fetch.log")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--chunk-days", type=int, default=25,
                    help="calendar days per API call (API max 30 for ONE_MINUTE)")
    ap.add_argument("--min-interval", type=float, default=0.4,
                    help="seconds between API calls")
    ap.add_argument("--price-field", choices=("close", "open"), default="close",
                    help="which bar price stands in for the tick LTP")
    ap.add_argument("--nearest-available", action="store_true",
                    help="for dates whose true near-month contract has expired (unavailable), "
                         "use the nearest still-listed contract instead of dropping the date")
    ap.add_argument("--near-month-from", type=dt.date.fromisoformat, default=None,
                    help="first date on which the earliest live contract was the near month "
                         "(day after the previous expiry); inferred if omitted")
    ap.add_argument("--offline", action="store_true",
                    help="no login, no API calls: rebuild outputs from the cache only")
    ap.add_argument("--expand-ohlc", dest="expand_ohlc", action="store_true", default=True,
                    help="(default) emit 4 ticks per bar: open, low/high, high/low, close; "
                         "see expand_ohlc()")
    ap.add_argument("--no-expand-ohlc", dest="expand_ohlc", action="store_false",
                    help="one tick per bar at --price-field (close-only; misrepresents the "
                         "opening print -- reference use only)")
    ap.add_argument("--ohlc-order", choices=("adverse-first", "favourable-first"),
                    default="adverse-first",
                    help="with --expand-ohlc: which intra-bar extreme comes first")
    ap.add_argument("--refetch-since", type=dt.date.fromisoformat, default=None,
                    help="discard cached chunks ending on/after this date and fetch them again "
                         "(use when the vendor backfills a hole)")
    args = ap.parse_args()

    setup_logging(args.log_file)
    if args.start >= args.end:
        ap.error("--start must be before --end")
    LOG.info("range %s .. %s  (%s)", args.start, args.end,
             "OFFLINE, cache only" if args.offline else "Angel One SmartAPI")

    client: Optional[AngelClient] = None
    if not args.offline:
        creds = Credentials.from_env(args.env)
        client = AngelClient(creds, min_interval=args.min_interval)
        client.login()

    master = ScripMaster.load(args.cache_dir, offline=args.offline)
    cache = ChunkCache(args.cache_dir)
    spot_c = master.nifty_spot()
    live = [c for c in master.nifty_futures() if c.expiry >= args.start]
    live_keys = {(c.token, c.expiry) for c in live}
    recovered = [c for c in cache.contracts_on_disk("NFO")
                 if (c.token, c.expiry) not in live_keys and c.expiry >= args.start]
    fut_cs = sorted(live + recovered, key=lambda c: c.expiry)
    expired_only = {(c.token, c.expiry) for c in recovered}
    if recovered:
        LOG.info("recovered %d expired contract(s) from cache: %s", len(recovered),
                 ", ".join(c.label for c in sorted(recovered, key=lambda c: c.expiry)))
    if args.refetch_since and not args.offline:
        n = cache.invalidate_from(spot_c, args.refetch_since)
        n += sum(cache.invalidate_from(c, args.refetch_since) for c in live)
        LOG.info("--refetch-since %s: discarded %d cached chunk(s)", args.refetch_since, n)
    LOG.info("spot: %s token %s", spot_c.symbol, spot_c.token)
    LOG.info("live NIFTY futures contracts: %s",
             ", ".join(f"{c.symbol}({c.expiry})" for c in fut_cs) or "none")
    if len(fut_cs) < 12:
        LOG.warning("only %d futures contract(s) (%d live, %d from cache) -- Angel One does "
                    "not serve expired contracts; run this regularly so history accumulates",
                    len(fut_cs), len(live), len(recovered))

    LOG.info("=== spot ===")
    spot = fetch_series(client, cache, spot_c, args.start, args.end, args.chunk_days,
                        args.offline, today)
    LOG.info("spot bars: %d", len(spot))

    fut_by_contract: Dict[Contract, pd.DataFrame] = {}
    for c in fut_cs:
        LOG.info("=== futures %s ===", c.label)
        # a monthly contract lists ~3 months before expiry; don't ask for more than that
        c_start = max(args.start, c.expiry - dt.timedelta(days=120))
        c_end = min(args.end, c.expiry)
        if c_start > c_end:
            continue
        # an expired contract can only come from cache -- never ask the API for it
        from_cache_only = args.offline or (c.token, c.expiry) in expired_only
        fut_by_contract[c] = fetch_series(client, cache, c, c_start, c_end, args.chunk_days,
                                          from_cache_only, today)

    fut, stitch = assign_contracts(fut_by_contract, fut_cs, args.nearest_available,
                                   args.near_month_from)
    LOG.info("futures rows after stitching: %d (near-month %d, nearest-available %d, "
             "dropped pre-near-month %d)", len(fut), stitch.get("strict_near_month_rows", 0),
             stitch.get("nearest_available_rows", 0),
             stitch.get("dropped_pre_near_month_rows", 0))

    merged, align_report = align(spot, fut, args.price_field) if len(fut) else (
        pd.DataFrame(columns=["timestamp", "trade_date", "spot_ltp", "futures_ltp",
                              "futures_contract_expiry"]), {"aligned_minutes": 0})

    # outputs ---------------------------------------------------------------
    if len(spot):
        s_out = session_filter(spot)[["timestamp", args.price_field]].rename(
            columns={args.price_field: "ltp"})
        s_out = s_out.assign(timestamp=s_out["timestamp"].dt.strftime(OUT_TS_FMT))
        os.makedirs(os.path.dirname(args.spot_out) or ".", exist_ok=True)
        s_out.to_csv(args.spot_out, index=False, float_format="%.2f")
        LOG.info("wrote %s (%d rows, %s..%s)", args.spot_out, len(s_out),
                 s_out["timestamp"].iloc[0], s_out["timestamp"].iloc[-1])
    if len(fut):
        f_out = session_filter(fut)[["timestamp", args.price_field, "futures_contract_expiry",
                                     "contract", "assignment"]]
        f_out = f_out.rename(columns={args.price_field: "ltp", "futures_contract_expiry": "expiry"})
        f_out = f_out.assign(timestamp=f_out["timestamp"].dt.strftime(OUT_TS_FMT))
        f_out.to_csv(args.fut_out, index=False, float_format="%.2f")
        LOG.info("wrote %s (%d rows)", args.fut_out, len(f_out))
    if len(merged):
        final = expand_ohlc(merged, args.ohlc_order) if args.expand_ohlc else merged
        write_merged(final, args.out)
        LOG.info("wrote %s (%d rows%s, %d sessions, %s..%s)", args.out, len(final),
                 " = 4 ticks/bar" if args.expand_ohlc else "", final["trade_date"].nunique(),
                 final["timestamp"].iloc[0], final["timestamp"].iloc[-1])
    else:
        LOG.error("no aligned rows -- nothing written to %s", args.out)

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "range_requested": [args.start.isoformat(), args.end.isoformat()],
        "source": "Angel One SmartAPI (live contracts only)",
        "price_field": (f"ohlc-expanded (4 ticks/bar, {args.ohlc_order})" if args.expand_ohlc
                        else args.price_field),
        "roll_convention": "expiring contract used through its expiry day; next contract from the following session",
        "spot": {"token": spot_c.token, "bars": int(len(spot)),
                 "first": str(spot["timestamp"].iloc[0]) if len(spot) else None,
                 "last": str(spot["timestamp"].iloc[-1]) if len(spot) else None},
        "futures_contracts_live": [c.label for c in fut_cs],
        "stitching": stitch,
        "alignment": align_report,
        "outputs": {"merged": args.out, "spot": args.spot_out, "futures": args.fut_out},
    }
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    with open(args.report, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    LOG.info("report: %s", args.report)

    a = align_report
    print()
    print("Alignment summary")
    print(f"  spot minutes (session hours)      {a.get('spot_minutes_in_session', 0):>10,}")
    print(f"  futures minutes (session hours)   {a.get('futures_minutes_in_session', 0):>10,}")
    print(f"  aligned minutes written           {a.get('aligned_minutes', 0):>10,}"
          f"   ({a.get('aligned_sessions', 0)} sessions)")
    print(f"  spot sessions with no futures     {a.get('spot_sessions_with_no_futures_at_all', 0):>10,}"
          f"   ({a.get('spot_minutes_in_uncovered_sessions', 0):,} minutes; expected -- "
          "Angel One has no expired contracts)")
    print(f"  dropped inside covered sessions:")
    print(f"    spot minute, no futures bar     {a.get('dropped_spot_only_minutes_in_covered_sessions', 0):>10,}")
    print(f"    futures minute, no spot bar     {a.get('dropped_futures_only_minutes', 0):>10,}")
    return 0 if len(merged) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as e:
        LOG.error("%s", e)
        sys.exit(1)
