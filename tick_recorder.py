#!/usr/bin/env python3
"""
tick_recorder.py -- record live ticks from the SmartAPI WebSocket to disk.

    python tick_recorder.py                 # run until 15:31 IST, then exit
    python tick_recorder.py --until 13:05   # shorter, e.g. for a test

Subscribes in LTP mode to the NIFTY 50 index and the near-month and next-month
NIFTY futures, and appends one CSV row per tick:

    data/ticks/<date>/<label>.csv
    recv_time,exchange_time,ltp,seq

recv_time is this machine's clock (ms), exchange_time the exchange's stamp
(ms, IST), ltp in rupees, seq the exchange sequence number. Read-only: the
stream is a market-data feed; nothing here can place an order.

Reconnects on drops until --until. Progress (ticks per instrument per minute)
goes to data/raw/ticks.log. Run under launchd by scripts/record_ticks.sh.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data as fd  # noqa: E402

LOG = fd.LOG


class Writer:
    """Buffered per-instrument CSV appender."""

    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.files = {}
        self.counts = {}
        self.lock = threading.Lock()

    def write(self, label: str, recv_ms: int, exch_ms: int, ltp: float, seq: int) -> None:
        with self.lock:
            fh = self.files.get(label)
            if fh is None:
                path = os.path.join(self.out_dir, f"{label}.csv")
                new = not os.path.exists(path) or os.path.getsize(path) == 0
                fh = open(path, "a", buffering=1 << 16)
                if new:
                    fh.write("recv_time,exchange_time,ltp,seq\n")
                self.files[label] = fh
            r = dt.datetime.fromtimestamp(recv_ms / 1000).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            e = dt.datetime.fromtimestamp(exch_ms / 1000).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if exch_ms else ""
            fh.write(f"{r},{e},{ltp:.2f},{seq}\n")
            self.counts[label] = self.counts.get(label, 0) + 1

    def flush(self) -> None:
        with self.lock:
            for fh in self.files.values():
                fh.flush()

    def close(self) -> None:
        with self.lock:
            for fh in self.files.values():
                fh.close()
            self.files.clear()

    def snapshot(self) -> dict:
        with self.lock:
            c = dict(self.counts)
            self.counts = {k: 0 for k in c}
            return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="15:31", help="HH:MM IST to stop")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "data", "ticks"))
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "data", "raw", "angel"))
    args = ap.parse_args()

    fd.setup_logging(os.path.join(HERE, "data", "raw", "ticks.log"))
    today = dt.date.today()
    h, m = (int(x) for x in args.until.split(":"))
    stop_at = dt.datetime.combine(today, dt.time(h, m))
    if dt.datetime.now() >= stop_at:
        LOG.info("past %s already -- nothing to record", args.until)
        return 0

    creds = fd.Credentials.from_env(os.path.join(HERE, ".env"))
    session_path = os.path.join(HERE, "data", "raw", "angel_session.json")
    client = fd.AngelClient(creds, session_cache=session_path)
    client.login()                                  # ensures today's jwt + feed token on disk
    with open(session_path) as fh:
        sess = json.load(fh)
    jwt, feed = sess["jwt"], sess.get("feed")
    if not feed:
        raise fd.FetchError("no feed token in the session cache")

    master = fd.ScripMaster.load(args.cache_dir, offline=False)
    spot = master.nifty_spot()
    futs = [c for c in master.nifty_futures() if c.expiry >= today][:2]    # near + next month
    labels = {spot.token: "NIFTY50"}
    for c in futs:
        labels[c.token] = c.symbol
    token_list = [{"exchangeType": 1, "tokens": [spot.token]},
                  {"exchangeType": 2, "tokens": [c.token for c in futs]}]
    LOG.info("recording %s until %s -> %s", ", ".join(labels.values()), args.until,
             os.path.join(args.out_dir, today.isoformat()))

    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    writer = Writer(os.path.join(args.out_dir, today.isoformat()))
    state = {"stop": False, "connected": False, "first": None}

    def run_socket():
        while not state["stop"] and dt.datetime.now() < stop_at:
            sws = SmartWebSocketV2("Bearer " + jwt, creds.api_key, creds.client_code, feed,
                                   max_retry_attempt=3, retry_delay=5)

            def on_open(wsapp):
                state["connected"] = True
                LOG.info("websocket open; subscribing")
                sws.subscribe("nifty180", SmartWebSocketV2.LTP_MODE, token_list)

            def on_data(wsapp, msg):
                try:
                    tok = str(msg.get("token", "")).strip('"')
                    label = labels.get(tok, tok)
                    ltp = (msg.get("last_traded_price") or 0) / 100.0
                    writer.write(label, int(time.time() * 1000), int(msg.get("exchange_timestamp") or 0),
                                 ltp, int(msg.get("sequence_number") or 0))
                    if state["first"] is None:
                        state["first"] = time.time()
                        LOG.info("first tick: %s %.2f", label, ltp)
                except Exception as e:  # never let a bad packet kill the stream
                    LOG.warning("tick parse error: %s", e)

            def on_error(wsapp, err=None):
                LOG.warning("websocket error: %s", err)

            def on_close(wsapp, *a):
                state["connected"] = False
                LOG.info("websocket closed")

            sws.on_open, sws.on_data, sws.on_error, sws.on_close = on_open, on_data, on_error, on_close
            state["sws"] = sws
            try:
                sws.connect()          # blocks until the socket closes
            except Exception as e:
                LOG.warning("connect failed: %s", e)
            if not state["stop"] and dt.datetime.now() < stop_at:
                LOG.info("reconnecting in 5 s")
                time.sleep(5)

    t = threading.Thread(target=run_socket, daemon=True)
    t.start()

    last_report = time.time()
    try:
        while dt.datetime.now() < stop_at and t.is_alive():
            time.sleep(2)
            writer.flush()
            if time.time() - last_report >= 60:
                snap = writer.snapshot()
                LOG.info("last minute: %s%s", ", ".join(f"{k} {v}" for k, v in snap.items()) or "no ticks",
                         "" if state["connected"] else "  [disconnected]")
                last_report = time.time()
    finally:
        state["stop"] = True
        try:
            state.get("sws") and state["sws"].close_connection()
        except Exception:
            pass
        writer.close()
        LOG.info("stopped at %s", dt.datetime.now().strftime("%H:%M:%S"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except fd.FetchError as e:
        LOG.error("%s", e)
        sys.exit(1)
