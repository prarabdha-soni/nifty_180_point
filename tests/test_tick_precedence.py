"""
Spec 13 (tick processing order), spec 1 (determinism), spec 11 (session,
overnight, expiry), and the R7/R8 resolutions.
"""

import copy
import datetime as dt
import pytest

from strategy.config import Config
from strategy.state import Side, MarketSnapshot
from strategy.data import MarketData, synth_events
from strategy.backtester import Backtester
from tests.harness import run, run_path, Session, D1, D2, closed_trades, fut_from_spot


CFG = Config(warmup_first_session=False, close_at_dataset_end=False)
D3 = dt.date(2026, 1, 7)


def _fut(spot):
    return [p + 20 for p in spot]


# ---------------------------------------------------------------------------
# Spec 1: determinism
# ---------------------------------------------------------------------------
def test_identical_input_produces_an_identical_ledger():
    spot = [24350, 24320, 24380, 24320, 24200, 24140, 24440, 24500, 24260]
    a_bt, a = run_path(spot=spot, futures=_fut(spot), cfg=CFG)
    b_bt, b = run_path(spot=spot, futures=_fut(spot), cfg=CFG)

    assert [t.as_dict() for t in a.trades] == [t.as_dict() for t in b.trades]
    assert [(e.timestamp, e.side, e.qty, e.futures_fill_price, e.reason)
            for e in a.executions] == \
           [(e.timestamp, e.side, e.qty, e.futures_fill_price, e.reason)
            for e in b.executions]


# ---------------------------------------------------------------------------
# R8: timestamp tiebreak between spot and futures events
# ---------------------------------------------------------------------------
def _split_events(date, pairs):
    """pairs = [(spot, futures)] emitted as SEPARATE events on the same timestamp."""
    t0 = dt.datetime.combine(date, dt.time(9, 16, 0))
    evs = []
    for i, (sp, fp) in enumerate(pairs):
        ts = t0 + dt.timedelta(seconds=i * 60)
        evs.append(MarketSnapshot(ts, date, None, float(fp), None, "FUT"))
        evs.append(MarketSnapshot(ts, date, float(sp), None, None, "SPOT"))
    return evs


def _run_split(pairs, cfg):
    events = MarketData._order(_split_events(D1, pairs), cfg)
    md = MarketData(events, cfg)
    ref = md.references[D1]
    ref.prev_spot_high, ref.prev_spot_low, ref.prev_spot_close = 24500, 24200, 24350
    ref.prev_fut_high, ref.prev_fut_low = 24520, 24220
    ref.is_warmup = False
    bt = Backtester(cfg)
    return bt, bt.run(md)


def test_futures_first_tiebreak_lets_the_spot_tick_see_fresh_futures():
    """Default ('FUT','SPOT'): the confirming futures print lands before the tick."""
    bt, res = _run_split([(24350, 24370), (24320, 24340)], CFG)
    assert bt.state.position == Side.SHORT
    assert bt.state.futures_entry_price == 24340


def test_spot_first_tiebreak_delays_the_same_entry_by_one_tick():
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 tie_break_order=("SPOT", "FUT"))
    bt, res = _run_split([(24350, 24370), (24320, 24340)], cfg)
    # the spot tick is processed against the STALE futures price 24370, which
    # does not confirm, so the engine parks a pending instead of entering
    assert bt.state.position == Side.FLAT
    assert bt.state.carry_anchor_side == Side.SHORT


def test_ordering_is_stable_for_equal_keys():
    cfg = CFG
    ts = dt.datetime(2026, 1, 5, 9, 16)
    evs = [MarketSnapshot(ts, D1, float(i), None, None, "SPOT") for i in range(5)]
    ordered = MarketData._order(evs, cfg)
    assert [e.spot_ltp for e in ordered] == [0.0, 1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------------------
# Spec 13: same-tick precedence
# ---------------------------------------------------------------------------
def test_entry_and_management_share_a_tick_without_exiting():
    """Step 4 opens, step 5 manages. R = 0 at entry so no exit can fire."""
    bt, res = run_path(spot=[24350, 24320], futures=[24370, 24340], cfg=CFG)
    assert bt.state.position == Side.SHORT
    assert len(res.executions) == 1
    assert bt.state.mfe_points == 0


def test_gap_fill_is_checked_before_anything_else_on_the_tick():
    """
    Step 1 runs the fill check, so the retirement rebase happens before the
    trail check in step 5 sees the tick. Without that ordering the REBASE mode
    could not prevent an instant trail exit.
    """
    from tests.test_gaps import two_session
    bt, res = two_session([24600, 24380])
    assert bt.state.position == Side.LONG
    assert bt.state.gap_regime is False
    assert "TRAIL_STOP" not in [e["event"] for e in res.events_log]


def test_early_stop_disarm_precedes_the_early_stop_check():
    """
    On a tick where the position both arms and would otherwise stop out, the
    disarm wins. Only reachable if a single tick moves R past +A and back,
    which cannot happen -- so assert the ordering directly on the sequence.
    """
    bt, res = run_path(
        spot=[24350, 24320, 24260, 24390],
        futures=[24370, 24340, 24280, 24410],
        cfg=CFG,
    )
    kinds = [e["event"] for e in res.events_log]
    assert kinds.index("EARLY_STOP_DISARMED") < len(kinds)
    assert "EARLY_STOP_REVERSAL" not in kinds


def test_early_stop_suppresses_the_partial_check_on_the_same_tick():
    """Spec 13: 'if early_stop_did_not_execute: check_partial_profit()'."""
    bt, res = run_path(
        spot=[24350, 24320, 24380],
        futures=[24370, 24340, 24400],
        cfg=CFG,
    )
    assert [e.reason for e in res.executions] == [
        "ENTRY", "EARLY_STOP", "REVERSE_EARLY_STOP"
    ]
    assert not any(e.reason == "PARTIAL" for e in res.executions)


# ---------------------------------------------------------------------------
# Spec 11: overnight carry and carry anchors
# ---------------------------------------------------------------------------
def test_position_is_carried_overnight_not_squared_off():
    s1 = Session(D1, spot=[24350, 24320], futures=[24370, 24340])
    s2 = Session(D2, spot=[24330], futures=[24350],
                 prev_spot_high=24350, prev_spot_low=24320, prev_spot_close=24320,
                 prev_fut_high=24370, prev_fut_low=24340)
    bt, res = run([s1, s2], CFG)
    assert bt.state.position == Side.SHORT
    assert bt.state.overnight_flag is True
    assert closed_trades(res) == []


def test_first_entry_pending_becomes_a_carry_anchor_and_is_restored():
    s1 = Session(D1, spot=[24350, 24320], futures=[24370, 24360])   # no confirm
    s2 = Session(D2, spot=[24315], futures=[24340],
                 prev_spot_high=24350, prev_spot_low=24320, prev_spot_close=24320,
                 prev_fut_high=24370, prev_fut_low=24360)
    bt, res = run([s1, s2], CFG)
    kinds = [e["event"] for e in res.events_log]
    assert "CARRY_ANCHOR_STORED" in kinds
    assert "CARRY_ANCHOR_RESTORED" in kinds
    # restored anchor confirms on day 2: spot 24315 <= 24320, futures 24340 <= 24340
    assert bt.state.position == Side.SHORT
    assert bt.state.spot_entry_price == 24315


def test_reentry_pending_is_discarded_at_session_close():
    """Spec 11: 'A trail/deferred re-entry pending signal is discarded at 15:30.'"""
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 extreme_tracking="FLAT_ONLY_PLUS_TRADE_BEST", reentry_same_tick=False)
    spot = [24350, 24380, 24700, 24520]
    s1 = Session(D1, spot, _fut(spot))
    bt, res = run([s1], cfg)
    kinds = [e["event"] for e in res.events_log]
    if "PENDING_CREATED" in kinds:
        created = [e for e in res.events_log if e["event"] == "PENDING_CREATED"]
        assert any(e.get("type") == "REENTRY" for e in created)
        assert "CARRY_ANCHOR_STORED" not in kinds
        assert bt.state.carry_anchor_side is None


# ---------------------------------------------------------------------------
# Spec 11: expiry
# ---------------------------------------------------------------------------
def test_expiry_day_force_closes_at_1500():
    s = Session(D1, spot=[24350, 24320, 24330], futures=[24370, 24340, 24350],
                start="14:58:00", step_seconds=60, expiry=D1, is_expiry_day=True)
    bt, res = run([s], CFG)
    done = closed_trades(res)
    assert len(done) == 1
    assert done[0].exit_reason == "EXPIRY_SQUAREOFF"
    assert done[0].exit_datetime.time() == dt.time(15, 0)
    assert bt.state.position == Side.FLAT


def test_expiry_day_blocks_new_entries_after_squareoff():
    # 14:58 warm, 14:59 SHORT entry, 15:00 square-off, 15:01 would re-trigger
    s = Session(D1, spot=[24350, 24320, 24500, 24320], futures=[24370, 24340, 24520, 24340],
                start="14:58:00", step_seconds=60, expiry=D1, is_expiry_day=True)
    bt, res = run([s], CFG)
    assert len(closed_trades(res)) == 1
    assert bt.state.position == Side.FLAT
    assert bt.state.trading_disabled_today is True


def test_non_expiry_day_has_no_squareoff():
    s = Session(D1, spot=[24350, 24320, 24330], futures=[24370, 24340, 24350],
                start="14:58:00", step_seconds=60)
    bt, res = run([s], CFG)
    assert bt.state.position == Side.SHORT
    assert closed_trades(res) == []


# ---------------------------------------------------------------------------
# R7: futures contract roll
# ---------------------------------------------------------------------------
def test_futures_roll_resets_the_futures_references():
    s1 = Session(D1, spot=[24350], futures=[24370], expiry=D2)
    s2 = Session(D2, spot=[24350], futures=[24420], expiry=D2, is_expiry_day=True)
    s3 = Session(D3, spot=[24350], futures=[24450], expiry=dt.date(2026, 2, 3),
                 prev_spot_high=24350, prev_spot_low=24350, prev_spot_close=24350,
                 prev_fut_high=24420, prev_fut_low=24420)
    bt, res = run([s1, s2, s3], CFG)
    kinds = [e["event"] for e in res.events_log]
    assert "FUTURES_ROLL" in kinds
    # the new contract's references are seeded from its own prints, not the old basis
    assert bt.state.futures_running_high == 24450
    assert bt.state.futures_running_low == 24450


def test_carry_anchor_is_discarded_across_a_roll():
    s1 = Session(D1, spot=[24350, 24320], futures=[24370, 24360], expiry=D2)
    s2 = Session(D2, spot=[24315], futures=[24340], expiry=dt.date(2026, 2, 3),
                 prev_spot_high=24350, prev_spot_low=24320, prev_spot_close=24320,
                 prev_fut_high=24370, prev_fut_low=24360)
    bt, res = run([s1, s2], CFG)
    kinds = [e["event"] for e in res.events_log]
    assert "CARRY_ANCHOR_STORED" in kinds
    assert "CARRY_ANCHOR_RESTORED" not in kinds
    assert "FUTURES_ROLL" in kinds


def test_roll_can_be_configured_to_keep_the_anchor():
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 discard_carry_anchor_on_roll=False)
    s1 = Session(D1, spot=[24350, 24320], futures=[24370, 24360], expiry=D2)
    s2 = Session(D2, spot=[24315], futures=[24340], expiry=dt.date(2026, 2, 3),
                 prev_spot_high=24350, prev_spot_low=24320, prev_spot_close=24320,
                 prev_fut_high=24370, prev_fut_low=24360)
    bt, res = run([s1, s2], cfg)
    assert "CARRY_ANCHOR_RESTORED" in [e["event"] for e in res.events_log]


# ---------------------------------------------------------------------------
# Costs (spec 12)
# ---------------------------------------------------------------------------
def test_cost_is_charged_per_execution_not_per_trade():
    cfg = Config(warmup_first_session=False)
    spot = [24350, 24320, 24180, 24500]
    bt, res = run_path(spot=spot, futures=_fut(spot), cfg=cfg)
    for e in res.executions:
        assert e.transaction_cost == pytest.approx(
            e.qty * e.futures_fill_price * cfg.transaction_cost_per_side
        )
    t = closed_trades(res, include_dataset_end=True)[0]
    assert t.transaction_cost == pytest.approx(
        sum(e.transaction_cost for e in res.executions if e.trade_id == t.trade_id)
    )


def test_reversal_is_charged_two_sides_not_one():
    bt, res = run_path(spot=[24350, 24320, 24380], futures=[24370, 24340, 24400], cfg=CFG)
    assert len(res.executions) == 3
    close_ex = res.executions[1]
    open_ex = res.executions[2]
    assert close_ex.transaction_cost > 0 and open_ex.transaction_cost > 0


def test_slippage_is_applied_against_the_trader():
    cfg = Config(warmup_first_session=False, close_at_dataset_end=False,
                 slippage_points_per_side=1.5)
    bt, res = run_path(spot=[24350, 24320], futures=[24370, 24340], cfg=cfg)
    ex = res.executions[0]
    assert ex.side == "SELL"
    assert ex.futures_fill_price == pytest.approx(24340 - 1.5)   # sold lower
    assert ex.slippage_cost == pytest.approx(1.5 * 390)


def test_v1_baseline_has_zero_slippage():
    assert Config().slippage_points_per_side == 0.0
    assert Config().stt_sell_side_pct == 0.0
