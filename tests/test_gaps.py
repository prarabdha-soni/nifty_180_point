"""
Morning gap logic -- spec section 10.

Every gap scenario needs a position carried overnight, so each test runs two
sessions: session 1 opens a LONG at 24380 and carries it; session 2 opens
with a gap against the previous close of 24380.

    G = 0.30%, so a qualifying gap is at least 73.1 points from 24380.
"""

import pytest

from strategy.config import Config
from strategy.state import Side
from tests.harness import run, Session, D1, D2, closed_trades, fut_from_spot


CFG = Config(warmup_first_session=False, close_at_dataset_end=False)

# Session 1: long_trigger = prev_spot_low + D = 24380, fut long trigger = 24400
S1_SPOT = [24350, 24380]
S1_FUT = [24370, 24400]


def session_two(spot, start="09:16:00"):
    """Session 2 references are session 1's own extremes and close."""
    return Session(
        D2, spot=spot, futures=fut_from_spot(spot), start=start,
        prev_spot_high=24380, prev_spot_low=24350, prev_spot_close=24380,
        prev_fut_high=24400, prev_fut_low=24370,
    )


def two_session(spot2, cfg=CFG, start="09:16:00"):
    return run([Session(D1, S1_SPOT, S1_FUT), session_two(spot2, start)], cfg)


# ---------------------------------------------------------------------------
# Setup sanity
# ---------------------------------------------------------------------------
def test_session_one_carries_a_long_overnight():
    bt, res = run([Session(D1, S1_SPOT, S1_FUT)], CFG)
    assert bt.state.position == Side.LONG
    assert bt.state.spot_entry_price == 24380
    assert "OVERNIGHT_CARRY" in [e["event"] for e in res.events_log]


def test_overnight_flag_is_set_on_the_carried_trade():
    bt, res = two_session([24600, 24380])
    carried = res.trades[0]
    assert carried.overnight_flag is True


# ---------------------------------------------------------------------------
# 10.1 Favourable gap
# ---------------------------------------------------------------------------
def test_favourable_gap_engages_the_regime():
    # +220 points on a LONG = +0.90%, favourable
    bt, res = two_session([24600])
    st = bt.state
    assert st.gap_regime is True
    assert st.gap_fill_price == 24380
    assert st.gap_hold_only_partial is True
    assert st.early_stop_active is False
    assert st.suppress_first_entries is True
    assert st.suppress_reversals is True
    assert "GAP_FAVOURABLE" in [e["event"] for e in res.events_log]


def test_favourable_gap_allows_partial_only():
    """Spec 10.1: ALLOW partial profit, DISALLOW early stop and trailing stop."""
    bt, res = two_session([24600])
    st = bt.state
    assert st.partial_done is True          # R = +220 >= P
    assert st.qty == 195
    assert st.position == Side.LONG         # trail suspended despite MFE 220


def test_favourable_gap_disallows_new_entries():
    bt, res = two_session([24600, 24700, 24800])
    entries = [e for e in res.events_log if e["event"].startswith("ENTRY")]
    assert len(entries) == 1                # only session 1's entry


def test_gap_fill_retires_the_regime():
    bt, res = two_session([24600, 24500, 24380])
    st = bt.state
    assert st.gap_regime is False
    assert st.gap_fill_price is None
    assert st.gap_hold_only_partial is False
    assert st.suppress_first_entries is False


# ---------------------------------------------------------------------------
# R6: MFE handling at gap retirement
# ---------------------------------------------------------------------------
def test_rebase_mode_keeps_the_position_alive_after_the_fill():
    """Default. MFE is rebased to current R, so the trail does not fire."""
    bt, res = two_session([24600, 24380])
    st = bt.state
    assert st.position == Side.LONG
    assert st.mfe_points == 0
    assert "TRAIL_STOP" not in [e["event"] for e in res.events_log]


def test_carry_mode_fires_the_trail_on_the_very_next_tick():
    """
    The reading the spec leaves open: a +220 MFE carried into the fill means
    drawback 220 >= T the instant the regime retires.
    """
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 gap_retirement_mfe_mode="CARRY")
    bt, res = two_session([24600, 24380], cfg=cfg)

    done = closed_trades(res)
    assert done[-1].exit_reason == "TRAIL_STOP"
    assert done[-1].direction == 1
    # and because the trail exit arms the opposite side (R2), the fill that
    # closes the gap also flips the book short
    assert bt.state.position == Side.SHORT


def test_early_stop_stays_disarmed_after_retirement_by_default():
    bt, res = two_session([24600, 24380])
    assert bt.state.early_stop_active is False


def test_early_stop_can_be_rearmed_on_retirement():
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 gap_retirement_rearms_early_stop=True)
    bt, res = two_session([24600, 24380], cfg=cfg)
    assert bt.state.early_stop_active is True


# ---------------------------------------------------------------------------
# 10.2 Adverse gap (backtest convention)
# ---------------------------------------------------------------------------
def test_adverse_gap_exits_at_the_first_eligible_futures_price():
    # -180 points on a LONG = -0.74%, adverse
    bt, res = two_session([24200])
    st = bt.state
    done = closed_trades(res)

    assert len(done) == 1
    assert done[0].exit_reason == "ADVERSE_GAP_ASSUMED_MANUAL_EXIT"
    assert done[0].exit_futures == 24220          # 24200 + 20 basis
    assert st.position == Side.FLAT
    assert st.trading_disabled_today is True


def test_adverse_gap_blocks_trading_for_the_rest_of_the_session():
    bt, res = two_session([24200, 24000, 24600, 24100])
    assert len(closed_trades(res)) == 1
    assert bt.state.position == Side.FLAT
    assert not bt.state.has_pending()


def test_adverse_gap_exit_is_tagged_for_separate_reporting():
    """Spec 10.2: 'Report adverse-gap results separately'."""
    from strategy.metrics import compute_metrics
    bt, res = two_session([24200])
    m = compute_metrics(closed_trades(res), CFG)
    assert m["diagnostics"]["adverse_gap_exits"] == 1
    assert "ADVERSE_GAP_ASSUMED_MANUAL_EXIT" in m["net_by_exit_reason"]


# ---------------------------------------------------------------------------
# Threshold and window
# ---------------------------------------------------------------------------
def test_sub_threshold_gap_does_nothing():
    # +50 points = 0.21% < 0.30%
    bt, res = two_session([24430])
    st = bt.state
    assert st.gap_regime is False
    assert st.trading_disabled_today is False
    assert st.position == Side.LONG


def test_no_gap_test_if_no_tick_arrives_in_the_window():
    """Spec 10: 'do not perform a late gap test'."""
    bt, res = two_session([24600], start="09:21:00")
    st = bt.state
    assert st.gap_regime is False
    assert "GAP_TEST_SKIPPED" in [e["event"] for e in res.events_log]


def test_gap_test_runs_once_per_session():
    bt, res = two_session([24600, 24610, 24620])
    kinds = [e["event"] for e in res.events_log]
    assert kinds.count("GAP_FAVOURABLE") == 1


# ---------------------------------------------------------------------------
# R5: directional_gap is identically zero while flat
# ---------------------------------------------------------------------------
def test_qualifying_gap_while_flat_does_nothing_by_default():
    s1 = Session(D1, spot=[24350], futures=[24370])      # no entry, stays flat
    s2 = session_two([24600])
    bt, res = run([s1, s2], CFG)
    assert bt.state.gap_regime is False
    assert bt.state.suppress_first_entries is False


def test_flat_gap_can_be_made_to_suppress_entries():
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 gap_applies_when_flat=True)
    s1 = Session(D1, spot=[24350], futures=[24370])
    s2 = session_two([24600])
    bt, res = run([s1, s2], cfg)
    assert bt.state.suppress_first_entries is True
    assert bt.state.position == Side.FLAT


# ---------------------------------------------------------------------------
# Flags reset daily
# ---------------------------------------------------------------------------
def test_gap_flags_reset_at_the_next_session():
    s1 = Session(D1, S1_SPOT, S1_FUT)
    s2 = session_two([24200])                     # adverse gap, trading disabled
    s3 = Session(
        __import__("datetime").date(2026, 1, 7), spot=[24250], futures=[24270],
        prev_spot_high=24200, prev_spot_low=24200, prev_spot_close=24200,
        prev_fut_high=24220, prev_fut_low=24220,
    )
    bt, res = run([s1, s2, s3], CFG)
    assert bt.state.trading_disabled_today is False
    assert bt.state.suppress_first_entries is False
    assert bt.state.gap_tested is True            # tested and found nothing on day 3
