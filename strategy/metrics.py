"""
Strategy metrics -- spec section 15.2, plus the attribution blocks section 18
requires ("separate attribution for early stops, partial exits, trails,
breaker events and gap regimes").

Sharpe and Sortino: the spec asks for them but does not define the return
series. V1 uses daily NET P&L in rupees, annualised by sqrt(trading_days_per_year).
This is a P&L-Sharpe, not a return-on-capital Sharpe, because the strategy
has no defined capital base -- position size is fixed at num_lots.
Documented here so the number is never mistaken for a return-based figure.

The diagnostics block is not in the spec. It exists because the review
flagged three things worth measuring before any optimisation:
  * trail exits split into winners and losers (a trail can exit at R = -120)
  * time spent in the unprotected deferred-flip interval
  * how often the futures confirmation gate actually changed the outcome
"""

from __future__ import annotations

from collections import defaultdict
from typing import List, Dict, Any, Optional
import datetime as dt
import math

from .pnl import Trade
from .config import Config


def _safe_div(a: float, b: float) -> float:
    return a / b if b else float("nan")


def _stdev(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mu = sum(xs) / n
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))


def compute_metrics(
    trades: List[Trade],
    cfg: Config,
    executions: Optional[list] = None,
    exclude_reasons: tuple = ("DATASET_END",),
) -> Dict[str, Any]:
    closed = [
        t for t in trades
        if t.is_closed and t.exit_reason not in exclude_reasons
    ]

    if not closed:
        return {"trades": 0, "note": "no closed trades"}

    nets = [t.net_pnl for t in closed]
    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl < 0]

    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)

    # --- equity curve by trade -------------------------------------------
    equity, peak, max_dd, dd_series = 0.0, 0.0, 0.0, []
    for t in sorted(closed, key=lambda x: x.exit_datetime):
        equity += t.net_pnl
        peak = max(peak, equity)
        dd = peak - equity
        dd_series.append(dd)
        max_dd = max(max_dd, dd)

    # --- daily P&L --------------------------------------------------------
    daily: Dict[dt.date, float] = defaultdict(float)
    for t in closed:
        daily[t.exit_trade_date or t.exit_datetime.date()] += t.net_pnl
    daily_pnl = [daily[d] for d in sorted(daily)]

    mu_d = sum(daily_pnl) / len(daily_pnl)
    sd_d = _stdev(daily_pnl)
    downside = [min(x, 0.0) for x in daily_pnl]
    sd_dn = math.sqrt(sum(x * x for x in downside) / len(downside)) if downside else 0.0
    ann = math.sqrt(cfg.trading_days_per_year)

    # --- consecutive losses ----------------------------------------------
    run = best_run = 0
    for t in sorted(closed, key=lambda x: x.exit_datetime):
        if t.net_pnl < 0:
            run += 1
            best_run = max(best_run, run)
        else:
            run = 0

    def by_reason(reason: str) -> List[Trade]:
        return [t for t in closed if t.exit_reason == reason]

    def net_of(ts: List[Trade]) -> float:
        return sum(t.net_pnl for t in ts)

    early = [t for t in closed if t.exit_reason == "EARLY_STOP"]
    trail = by_reason("TRAIL_STOP")
    deferred = by_reason("DEFERRED_FLIP")
    adverse_gap = by_reason("ADVERSE_GAP_ASSUMED_MANUAL_EXIT")
    expiry = by_reason("EXPIRY_SQUAREOFF")

    partial_contrib = sum(
        t.direction * t.partial_qty * (t.partial_futures - t.entry_futures)
        for t in closed if t.partial_qty
    )

    m: Dict[str, Any] = {
        # ---- spec 15.2 -------------------------------------------------
        "total_gross_pnl": sum(t.gross_pnl for t in closed),
        "total_net_pnl": sum(nets),
        "total_transaction_costs": sum(t.transaction_cost for t in closed),
        "completed_trades": len(closed),
        "win_rate": _safe_div(len(wins), len(closed)),
        "average_winner": _safe_div(gross_win, len(wins)),
        "average_loser": _safe_div(-gross_loss, len(losses)),
        "profit_factor": _safe_div(gross_win, gross_loss),
        "expectancy_per_trade": _safe_div(sum(nets), len(closed)),
        "maximum_drawdown": max_dd,
        "average_drawdown": _safe_div(sum(dd_series), len(dd_series)),
        "sharpe_ratio_daily_pnl": _safe_div(mu_d, sd_d) * ann if sd_d else float("nan"),
        "sortino_ratio_daily_pnl": _safe_div(mu_d, sd_dn) * ann if sd_dn else float("nan"),
        "maximum_consecutive_losses": best_run,
        "average_holding_time_seconds": _safe_div(
            sum(t.holding_time or 0 for t in closed), len(closed)
        ),
        "overnight_pnl": net_of([t for t in closed if t.overnight_flag]),
        "long_pnl": net_of([t for t in closed if t.direction == 1]),
        "short_pnl": net_of([t for t in closed if t.direction == -1]),
        "early_stop_pnl": net_of(early),
        "trailing_stop_pnl": net_of(trail),
        "partial_profit_contribution": partial_contrib,
        "breaker_contribution": net_of([t for t in closed if t.breaker_triggered]),
        "gap_regime_contribution": net_of([t for t in closed if t.gap_regime_flag]),

        # ---- attribution counts (spec 18) ------------------------------
        "counts_by_exit_reason": {
            r: len([t for t in closed if t.exit_reason == r])
            for r in sorted({t.exit_reason for t in closed})
        },
        "net_by_exit_reason": {
            r: net_of([t for t in closed if t.exit_reason == r])
            for r in sorted({t.exit_reason for t in closed})
        },

        # ---- diagnostics (not in spec; see module docstring) ------------
        "diagnostics": {
            "trail_exits_total": len(trail),
            "trail_exits_winning": len([t for t in trail if t.net_pnl > 0]),
            "trail_exits_losing": len([t for t in trail if t.net_pnl <= 0]),
            "trail_worst_net": min((t.net_pnl for t in trail), default=0.0),
            "breaker_episodes": len([t for t in closed if t.breaker_triggered]),
            "breaker_worst_net": min(
                (t.net_pnl for t in closed if t.breaker_triggered), default=0.0
            ),
            "breaker_worst_mae_points": min(
                (t.maximum_adverse_excursion for t in closed if t.breaker_triggered),
                default=0.0,
            ),
            "breaker_unprotected_seconds_total": sum(
                t.deferred_unprotected_seconds for t in closed
            ),
            "breaker_held_overnight": len(
                [t for t in closed if t.breaker_triggered and t.overnight_flag]
            ),
            "deferred_flips_executed": len(deferred),
            "adverse_gap_exits": len(adverse_gap),
            "expiry_squareoffs": len(expiry),
            "trades_with_partial": len([t for t in closed if t.partial_qty]),
            "overnight_trades": len([t for t in closed if t.overnight_flag]),
            "top5_net_concentration": _safe_div(
                sum(sorted(nets, reverse=True)[:5]), sum(nets)
            ),
        },
    }
    return m


def format_metrics(m: Dict[str, Any]) -> str:
    if m.get("trades") == 0:
        return "No closed trades."
    lines = ["=" * 62, "STRATEGY METRICS (spec 15.2)", "=" * 62]

    def row(k, v, unit=""):
        if isinstance(v, float):
            v = f"{v:,.2f}"
        lines.append(f"  {k:<38} {v}{unit}")

    for k in (
        "completed_trades", "total_gross_pnl", "total_transaction_costs",
        "total_net_pnl", "win_rate", "average_winner", "average_loser",
        "profit_factor", "expectancy_per_trade", "maximum_drawdown",
        "average_drawdown", "sharpe_ratio_daily_pnl", "sortino_ratio_daily_pnl",
        "maximum_consecutive_losses", "average_holding_time_seconds",
        "overnight_pnl", "long_pnl", "short_pnl", "early_stop_pnl",
        "trailing_stop_pnl", "partial_profit_contribution",
        "breaker_contribution", "gap_regime_contribution",
    ):
        row(k, m.get(k, float("nan")))

    lines += ["", "-" * 62, "ATTRIBUTION BY EXIT REASON", "-" * 62]
    for r, c in m["counts_by_exit_reason"].items():
        lines.append(f"  {r:<38} {c:>6}   net {m['net_by_exit_reason'][r]:>14,.2f}")

    lines += ["", "-" * 62, "DIAGNOSTICS", "-" * 62]
    for k, v in m["diagnostics"].items():
        row(k, v)
    lines.append("=" * 62)
    return "\n".join(lines)
