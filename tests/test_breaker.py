"""
Spec section 16 validation scenarios 8-10, and the two breaker resolutions.

Shared path: a SHORT early-stops and reverses to LONG at 24380 (first early
stop), then the LONG early-stops at 24320 (second consecutive early stop),
which is where the breaker engages.
"""

import pytest

from strategy.config import Config
from strategy.state import Side
from tests.harness import run_path, closed_trades


CFG = Config(warmup_first_session=False, close_at_dataset_end=False)

#            t1     t2(SHORT) t3(stop1->LONG) t4(stop2->breaker)
BASE_SPOT = [24350, 24320,    24380,          24320]
BASE_FUT = [24370, 24340,    24400,          24340]


def _fut(spot):
    return [p + 20 for p in spot]


# ---------------------------------------------------------------------------
# Scenario 8: Second early stop
#   "Second consecutive early stop -> Do not close/reverse; activate deferred flip"
# ---------------------------------------------------------------------------
def test_scenario_08_second_early_stop_engages_breaker():
    bt, res = run_path(spot=BASE_SPOT, futures=BASE_FUT, cfg=CFG)
    st = bt.state

    # only the FIRST early stop produced a closed trade
    assert len(closed_trades(res)) == 1
    assert closed_trades(res)[0].exit_reason == "EARLY_STOP"

    # the second one kept the position
    assert st.position == Side.LONG
    assert st.qty == 390
    assert st.spot_entry_price == 24380
    assert st.deferred_flip_active is True
    assert st.consecutive_early_stops == 2
    assert st.breaker_triggered_this_trade is True
    assert st.deferred_flip_price == pytest.approx(24380 - 240)   # 24140

    kinds = [e["event"] for e in res.events_log]
    assert kinds.count("EARLY_STOP_REVERSAL") == 1
    assert "BREAKER_ENGAGED" in kinds


def test_breaker_does_not_re_fire_every_tick_while_deferred():
    spot = BASE_SPOT + [24300, 24280, 24260]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    kinds = [e["event"] for e in res.events_log]
    assert kinds.count("BREAKER_ENGAGED") == 1
    assert bt.state.position == Side.LONG          # still held, still unprotected


def test_breaker_can_be_disabled():
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 breaker_enabled=False)
    bt, res = run_path(spot=BASE_SPOT, futures=BASE_FUT, cfg=cfg)
    # without the breaker the second early stop reverses normally
    assert len(closed_trades(res)) == 2
    assert bt.state.position == Side.SHORT
    assert bt.state.deferred_flip_active is False


# ---------------------------------------------------------------------------
# Scenario 9: Deferred flip
#   "LONG 24,380 -> second stop 24,320 -> 24,140 -> Reverse to SHORT at deferred level"
# ---------------------------------------------------------------------------
def test_scenario_09_deferred_flip_executes_at_minus_240():
    spot = BASE_SPOT + [24200, 24140]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    st = bt.state

    done = closed_trades(res)
    assert len(done) == 2
    flip = done[1]
    assert flip.exit_reason == "DEFERRED_FLIP"
    assert flip.direction == 1                      # the retained LONG
    assert flip.entry_spot == 24380
    assert flip.exit_spot == 24140
    assert flip.maximum_adverse_excursion == -240
    assert flip.breaker_triggered is True

    # reversed into a SHORT
    assert st.position == Side.SHORT
    assert st.spot_entry_price == 24140
    assert st.entry_reason == "REVERSE_DEFERRED_FLIP"

    # R3: the counter must reset or the strategy never leaves breaker state
    assert st.consecutive_early_stops == 0
    assert st.deferred_flip_active is False


def test_deferred_flip_does_not_fire_one_point_early():
    spot = BASE_SPOT + [24141]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    assert bt.state.position == Side.LONG           # R = -239
    assert bt.state.deferred_flip_active is True


def test_full_chop_episode_costs_S_plus_S_plus_S_T():
    """
    The tail-risk shape flagged in review: -60, reverse, -60, then held to -240.
    Three legs of one chop episode, measured end to end in points.
    """
    spot = BASE_SPOT + [24140]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    done = closed_trades(res)
    total_points = sum(
        t.direction * (t.exit_futures - t.entry_futures) * t.exit_qty / 390
        for t in done
    )
    assert total_points == pytest.approx(-60 + -240)
    assert sum(t.gross_pnl for t in done) == pytest.approx(390 * -300)


# ---------------------------------------------------------------------------
# Scenario 10: Deferred recovery
#   "Retained position recovers +60 before deferred level
#    -> Cancel deferred flip and resume trail"
# ---------------------------------------------------------------------------
def test_scenario_10_deferred_recovery_cancels_the_flip():
    spot = BASE_SPOT + [24440]                      # LONG 24380 -> R = +60
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    st = bt.state

    assert st.position == Side.LONG
    assert st.deferred_flip_active is False
    assert st.consecutive_early_stops == 0
    assert st.early_stop_active is False            # armed, so permanently off
    assert st.mfe_points == 60
    assert len(closed_trades(res)) == 1             # the retained LONG is still open


def test_recovered_position_resumes_normal_trailing():
    # recover to +60, then give back 180 from the best -> trail exit
    spot = BASE_SPOT + [24440, 24260]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    done = closed_trades(res)
    assert done[-1].exit_reason == "TRAIL_STOP"
    assert done[-1].maximum_favourable_excursion == 60


# ---------------------------------------------------------------------------
# R1: the literal spec 9 reset condition makes the breaker unreachable
# ---------------------------------------------------------------------------
def test_literal_reset_reading_makes_the_breaker_dead_code():
    """
    Spec 9 resets on 'position_becomes_flat'. Spec 8.2 routes every early stop
    through a momentary flat. Taken literally the counter never reaches B.
    """
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 breaker_reset_on_reversal_flat=True)
    bt, res = run_path(spot=BASE_SPOT, futures=BASE_FUT, cfg=cfg)

    assert bt.state.deferred_flip_active is False
    assert "BREAKER_ENGAGED" not in [e["event"] for e in res.events_log]
    assert len(closed_trades(res)) == 2             # both early stops reversed


def test_counter_resets_when_a_trade_arms():
    """Spec 9: 'if MFE >= A ... consecutive_early_stops = 0'."""
    #     t1     t2(SHORT) t3(stop1->LONG) t4(LONG arms +60)
    spot = [24350, 24320, 24380, 24440]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    assert bt.state.consecutive_early_stops == 0
    assert bt.state.mfe_points >= 60


def test_counter_survives_across_the_reversal_flat_by_default():
    spot = BASE_SPOT[:3]                            # one early stop only
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    assert bt.state.consecutive_early_stops == 1


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------
def test_trail_cannot_fire_during_a_deferred_flip():
    """
    Proof in breaker.py: the early stop only fires while unarmed (MFE < A), so
    a deferred flip implies MFE < A, and the spec 8.4 trail requires MFE >= A.
    The unprotected interval is therefore structural, not incidental.
    """
    spot = BASE_SPOT + [24300, 24250, 24200, 24160]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    st = bt.state
    assert st.deferred_flip_active is True
    assert st.mfe_points < CFG.early_arm_distance
    assert "TRAIL_STOP" not in [e["event"] for e in res.events_log]


def test_unprotected_interval_is_measured():
    spot = BASE_SPOT + [24200, 24140]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    flip = [t for t in closed_trades(res) if t.exit_reason == "DEFERRED_FLIP"][0]
    assert flip.deferred_unprotected_seconds == pytest.approx(120.0)   # two 60s ticks
