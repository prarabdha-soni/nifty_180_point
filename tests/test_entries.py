"""Spec section 16 validation scenarios 1-3, plus entry-rule edge cases."""

import datetime as dt
import pytest

from strategy.config import Config
from strategy.state import Side, PendingType
from tests.harness import run_path, run, Session, D1, D2, closed_trades


CFG = Config(warmup_first_session=False, close_at_dataset_end=False)


# ---------------------------------------------------------------------------
# Scenario 1: Immediate SHORT
#   "Spot & futures both reach short trigger -> Open SHORT at current spot/futures"
# ---------------------------------------------------------------------------
def test_scenario_01_immediate_short():
    bt, res = run_path(spot=[24350, 24320], futures=[24370, 24340], cfg=CFG)
    st = bt.state

    assert st.position == Side.SHORT
    assert st.spot_entry_price == 24320
    assert st.futures_entry_price == 24340
    assert st.qty == CFG.base_qty == 390
    assert st.early_stop_active is True
    assert st.partial_done is False

    entry = res.executions[0]
    assert entry.side == "SELL"
    assert entry.qty == 390
    assert entry.reason == "ENTRY"
    # spec 12.1: cost on notional, not a fixed points deduction
    assert entry.transaction_cost == pytest.approx(390 * 24340 * 0.00015)


def test_immediate_long_mirror():
    # long_trigger = prev_spot_low + D = 24380; fut long trigger = 24400
    bt, res = run_path(spot=[24350, 24380], futures=[24370, 24400], cfg=CFG)
    assert bt.state.position == Side.LONG
    assert bt.state.spot_entry_price == 24380
    assert res.executions[0].side == "BUY"


def test_short_takes_precedence_when_both_sides_trigger():
    """Spec 5.3: 'If both sides are true on the same tick, SHORT takes precedence.'"""
    # Range wider than 2*D so both triggers are simultaneously live.
    s = Session(
        D1,
        spot=[24300],
        futures=[24320],
        prev_spot_high=24800, prev_spot_low=24000, prev_spot_close=24300,
        prev_fut_high=24820, prev_fut_low=24020,
    )
    bt, res = run([s], CFG)
    # short_trig 24620 (24300 <= 24620 ok); long_trig 24180 (24300 >= 24180 ok)
    assert bt.state.position == Side.SHORT


# ---------------------------------------------------------------------------
# Scenario 2: Delayed futures confirmation
#   "Spot reaches 24,320; futures confirms later when spot = 24,305
#    -> Open SHORT using current 24,305 spot reference"
# ---------------------------------------------------------------------------
def test_scenario_02_delayed_futures_confirmation():
    bt, res = run_path(
        spot=[24350, 24320, 24310, 24305],
        futures=[24370, 24360, 24350, 24340],
        cfg=CFG,
    )
    st = bt.state
    assert st.position == Side.SHORT
    # spec 5.4: entry reference is the CURRENT spot at confirmation, not the trigger
    assert st.spot_entry_price == 24305
    assert st.futures_entry_price == 24340
    assert res.executions[0].reason == "ENTRY_PENDING"

    kinds = [e["event"] for e in res.events_log]
    assert "PENDING_CREATED" in kinds
    assert "ENTRY_PENDING_CONFIRMED" in kinds


def test_pending_freezes_the_trigger_levels():
    """
    The pending slot holds the FROZEN trigger levels, not the moving ones.
    Spec 11 converts an unconfirmed FIRST_ENTRY pending into a carry anchor at
    session close, so the frozen levels are asserted there (R4).
    """
    bt, res = run_path(
        spot=[24350, 24320, 24310],
        futures=[24370, 24360, 24350],
        cfg=CFG,
    )
    st = bt.state
    assert st.position == Side.FLAT
    assert st.carry_anchor_side == Side.SHORT
    assert st.carry_anchor_spot_level == 24320      # frozen, not 24310
    assert st.carry_anchor_futures_level == 24340

    kinds = [e["event"] for e in res.events_log]
    assert kinds.count("PENDING_CREATED") == 1
    assert "CARRY_ANCHOR_STORED" in kinds
    created = next(e for e in res.events_log if e["event"] == "PENDING_CREATED")
    assert created["type"] == PendingType.FIRST_ENTRY


# ---------------------------------------------------------------------------
# Scenario 3: Pending cancelled
#   "Spot triggers then retraces through frozen trigger before futures confirms
#    -> Cancel pending; remain flat"
# ---------------------------------------------------------------------------
def test_scenario_03_pending_cancelled_on_retrace():
    bt, res = run_path(
        spot=[24350, 24320, 24325],
        futures=[24370, 24360, 24365],
        cfg=CFG,
    )
    st = bt.state
    assert st.position == Side.FLAT
    assert not st.has_pending()
    assert len(res.trades) == 0


def test_pending_long_cancels_on_downward_retrace():
    bt, res = run_path(
        spot=[24350, 24380, 24375],
        futures=[24370, 24390, 24385],   # never reaches fut long trigger 24400
        cfg=CFG,
    )
    assert bt.state.position == Side.FLAT
    assert not bt.state.has_pending()


def test_cancellation_is_checked_before_firing():
    """Spec 13 step 3. Same tick: spot retraces AND futures would confirm."""
    bt, res = run_path(
        spot=[24350, 24320, 24325],
        futures=[24370, 24360, 24300],   # futures deeply confirms on the retrace tick
        cfg=CFG,
    )
    assert bt.state.position == Side.FLAT, "cancel must win over confirm on the same tick"


# ---------------------------------------------------------------------------
# Reference extremes
# ---------------------------------------------------------------------------
def test_references_only_widen_within_a_session():
    bt, res = run_path(spot=[24350, 24450, 24400], futures=[24370, 24470, 24420], cfg=CFG)
    st = bt.state
    assert st.running_high == 24500          # prev high still dominates
    assert st.running_low == 24200


def test_new_session_resets_references_to_two_day_window():
    s1 = Session(D1, spot=[24350, 24360], futures=[24370, 24380])
    s2 = Session(D2, spot=[24350], futures=[24370],
                 prev_spot_high=24400, prev_spot_low=24300, prev_spot_close=24350,
                 prev_fut_high=24420, prev_fut_low=24320)
    bt, res = run([s1, s2], CFG)
    assert bt.state.running_high == 24400
    assert bt.state.running_low == 24300


def test_extremes_do_not_self_trigger_on_the_widening_tick():
    """Widening running_high to the current spot sets the trigger 180 below it."""
    bt, res = run_path(spot=[24350, 24700], futures=[24370, 24720], cfg=CFG)
    # 24700 > prev high, so running_high becomes 24700 and short_trig 24520.
    # 24700 <= 24520 is false, so no self-triggered short.
    assert bt.state.position in (Side.FLAT, Side.LONG)
    assert bt.state.position != Side.SHORT
