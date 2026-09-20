"""Spec section 16 validation scenarios 4-7: early stop, arm, partial, trail."""

import pytest

from strategy.config import Config
from strategy.state import Side
from tests.harness import run_path, closed_trades


CFG = Config(warmup_first_session=False, close_at_dataset_end=False)


# ---------------------------------------------------------------------------
# Scenario 4: Early stop reversal
#   "SHORT 24,320 -> spot 24,380 before +60 favourable -> Close SHORT and open LONG"
# ---------------------------------------------------------------------------
def test_scenario_04_early_stop_reversal():
    bt, res = run_path(
        spot=[24350, 24320, 24350, 24380],
        futures=[24370, 24340, 24370, 24400],
        cfg=CFG,
    )
    st = bt.state
    done = closed_trades(res)

    assert len(done) == 1
    t = done[0]
    assert t.direction == -1
    assert t.exit_reason == "EARLY_STOP"
    assert t.entry_spot == 24320 and t.exit_spot == 24380

    # spec 8.2: modelled as a close plus a new opposite entry at the same price
    assert st.position == Side.LONG
    assert st.spot_entry_price == 24380
    assert st.futures_entry_price == 24400
    assert st.qty == 390
    assert st.early_stop_active is True      # fresh trade re-arms the early stop

    # gross: SHORT 390 lots, futures 24340 -> 24400 = -60 points
    assert t.gross_pnl == pytest.approx(-1 * 390 * (24400 - 24340))
    assert t.net_pnl < t.gross_pnl           # costs deducted

    # a reversal is TWO executions, not one net order
    assert len(res.executions) == 3          # entry, close, re-entry
    assert [e.reason for e in res.executions] == ["ENTRY", "EARLY_STOP", "REVERSE_EARLY_STOP"]


def test_early_stop_does_not_fire_one_point_early():
    bt, res = run_path(
        spot=[24350, 24320, 24379],
        futures=[24370, 24340, 24399],
        cfg=CFG,
    )
    assert bt.state.position == Side.SHORT   # R = -59, stop is at -60
    assert closed_trades(res) == []


# ---------------------------------------------------------------------------
# Scenario 5: Arm disables stop
#   "SHORT 24,320 -> spot 24,260 -> Early stop permanently off"
# ---------------------------------------------------------------------------
def test_scenario_05_arm_disables_early_stop_permanently():
    bt, res = run_path(
        spot=[24350, 24320, 24290, 24260],
        futures=[24370, 24340, 24310, 24280],
        cfg=CFG,
    )
    st = bt.state
    assert st.position == Side.SHORT
    assert st.mfe_points == 60
    assert st.early_stop_active is False
    assert "EARLY_STOP_DISARMED" in [e["event"] for e in res.events_log]


def test_armed_position_survives_an_adverse_move_past_the_old_stop():
    """Once armed, R = -70 no longer closes the trade (drawback 130 < T)."""
    bt, res = run_path(
        spot=[24350, 24320, 24260, 24390],
        futures=[24370, 24340, 24280, 24410],
        cfg=CFG,
    )
    st = bt.state
    assert st.position == Side.SHORT
    assert st.early_stop_active is False
    assert st.r_points(24390) == -70
    assert closed_trades(res) == []


# ---------------------------------------------------------------------------
# Scenario 6: Partial
#   "SHORT 24,320 -> spot 24,180 -> Close 50% once"
# ---------------------------------------------------------------------------
def test_scenario_06_partial_profit():
    bt, res = run_path(
        spot=[24350, 24320, 24250, 24180],
        futures=[24370, 24340, 24270, 24200],
        cfg=CFG,
    )
    st = bt.state
    assert st.partial_done is True
    assert st.qty == 195                       # 390 - 195
    assert st.position == Side.SHORT

    partials = [e for e in res.executions if e.reason == "PARTIAL"]
    assert len(partials) == 1
    assert partials[0].qty == 195
    assert partials[0].side == "BUY"           # closing a short buys
    assert partials[0].futures_fill_price == 24200


def test_partial_executes_only_once_per_trade():
    bt, res = run_path(
        spot=[24350, 24320, 24180, 24200, 24150],
        futures=[24370, 24340, 24200, 24220, 24170],
        cfg=CFG,
    )
    assert len([e for e in res.executions if e.reason == "PARTIAL"]) == 1
    assert bt.state.qty == 195


def test_partial_does_not_fire_below_threshold():
    bt, res = run_path(
        spot=[24350, 24320, 24181],
        futures=[24370, 24340, 24201],
        cfg=CFG,
    )
    assert bt.state.partial_done is False      # R = 139
    assert bt.state.qty == 390


# ---------------------------------------------------------------------------
# Scenario 7: Trail
#   "SHORT best 24,260 -> spot 24,440 -> Trail closes remaining position"
# ---------------------------------------------------------------------------
def test_scenario_07_trailing_stop():
    bt, res = run_path(
        spot=[24350, 24320, 24260, 24380, 24440],
        futures=[24370, 24340, 24280, 24400, 24460],
        cfg=CFG,
    )
    done = closed_trades(res)
    trail = [t for t in done if t.exit_reason == "TRAIL_STOP"]

    assert len(trail) == 1
    t = trail[0]
    assert t.direction == -1
    assert t.maximum_favourable_excursion == 60
    assert t.exit_spot == 24440
    assert t.partial_qty == 0                  # never reached +140
    assert t.exit_qty == 390

    # A trail exit at MFE 60 with T 180 closes at R = -120. Gross is a LOSS.
    assert t.gross_pnl == pytest.approx(-1 * 390 * (24460 - 24340))
    assert t.gross_pnl < 0


def test_trail_needs_the_position_armed_first():
    """MFE < A means no trail, however large the giveback."""
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 breaker_enabled=False)
    bt, res = run_path(
        spot=[24350, 24320, 24300, 24600],
        futures=[24370, 24340, 24320, 24620],
        cfg=cfg,
    )
    # MFE peaks at 20, so the early stop (not the trail) governs the exit
    done = closed_trades(res)
    assert done and done[0].exit_reason == "EARLY_STOP"


# ---------------------------------------------------------------------------
# R2: the single largest behavioural fork. The two modes only diverge when the
# in-trade extreme EXCEEDS the previous session extreme, so the path below
# runs a LONG up through prev_spot_high (24500) to 24700 before trailing out.
#   ALWAYS     running_high 24700 -> short_trigger 24520 == the trail exit
#              price, so an opposite re-entry is armed immediately.
#   FLAT_ONLY  running_high stays 24500 -> short_trigger 24320, far below the
#              exit, so arm_opposite_reentry_if_applicable() has nothing to arm.
# ---------------------------------------------------------------------------
_R2_SPOT = [24350, 24380, 24700, 24520]
_R2_FUT = [24370, 24400, 24720, 24540]


def test_trail_exit_arms_an_opposite_reentry_under_always_tracking():
    bt, res = run_path(spot=_R2_SPOT, futures=_R2_FUT, cfg=CFG)
    kinds = [e["event"] for e in res.events_log]

    assert "TRAIL_STOP" in kinds
    assert "REENTRY_IMMEDIATE" in kinds
    assert bt.state.position == Side.SHORT
    assert bt.state.entry_reason == "REENTRY"
    assert bt.state.spot_entry_price == 24520


def test_flat_only_extreme_tracking_leaves_reentry_unarmed():
    """The literal spec 13 step 2 reading, for comparison."""
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 extreme_tracking="FLAT_ONLY")
    bt, res = run_path(spot=_R2_SPOT, futures=_R2_FUT, cfg=cfg)
    kinds = [e["event"] for e in res.events_log]

    assert "TRAIL_STOP" in kinds
    assert "REENTRY_IMMEDIATE" not in kinds
    assert bt.state.position == Side.FLAT


def test_flat_only_plus_trade_best_restores_the_arm():
    """Middle option: flat-only tracking, but seed references from the trade."""
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 extreme_tracking="FLAT_ONLY_PLUS_TRADE_BEST")
    bt, res = run_path(spot=_R2_SPOT, futures=_R2_FUT, cfg=cfg)
    kinds = [e["event"] for e in res.events_log]

    assert "TRAIL_STOP" in kinds
    assert "PENDING_CREATED" in kinds or "REENTRY_IMMEDIATE" in kinds


# ---------------------------------------------------------------------------
# P&L reconciliation (spec 18)
# ---------------------------------------------------------------------------
def test_ledger_reconciles():
    cfg = Config(warmup_first_session=False)     # force-close at dataset end
    bt, res = run_path(
        spot=[24350, 24320, 24350, 24380, 24320, 24380, 24250, 24180, 24400],
        futures=[p + 20 for p in
                 [24350, 24320, 24350, 24380, 24320, 24380, 24250, 24180, 24400]],
        cfg=cfg,
    )
    r = res.reconciliation
    assert r["all_ok"], r
    assert r["cost_reconciles"] and r["net_reconciles"] and r["quantity_reconciles"]


def test_partial_and_final_quantities_sum_to_entry():
    cfg = Config(warmup_first_session=False)
    bt, res = run_path(
        spot=[24350, 24320, 24180, 24500],
        futures=[24370, 24340, 24200, 24520],
        cfg=cfg,
    )
    for t in res.trades:
        if t.is_closed:
            assert t.partial_qty + t.exit_qty == t.entry_qty
