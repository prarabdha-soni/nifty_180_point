"""V2 research switches (V2_RESEARCH.md). Each must reproduce V1 exactly at its
default, and do what it says when switched on.

H1 distance_scale_ref: D, S, A, P, T and the futures-confirm distance are
multiplied by prev_session_close / ref at each session start.
Harness references: prev_spot_low 24200, prev_spot_close 24350, so the V1
long trigger is 24200 + 180 = 24380 (futures 24220 + 180 = 24400).
"""

from strategy.config import Config
from strategy.state import Side
from tests.harness import run_path, closed_trades, PREV_SPOT_CLOSE

BASE = dict(warmup_first_session=False, close_at_dataset_end=False)

# the R2 path from test_exits: long at 24380, runs to 24700, trails out at 24520
PATH_SPOT = [24350, 24380, 24700, 24520]
PATH_FUT = [24370, 24400, 24720, 24540]


def _ledger(res):
    return [(t.direction, t.entry_spot, t.exit_spot, t.exit_reason, round(t.net_pnl, 6))
            for t in res.trades]


def test_scale_default_is_v1():
    assert Config().distance_scale_ref is None


def test_scale_at_prev_close_is_identical_to_v1():
    """k = prev_close / ref = 1 -> every distance unchanged -> same ledger."""
    _, v1 = run_path(PATH_SPOT, PATH_FUT, cfg=Config(**BASE))
    _, h1 = run_path(PATH_SPOT, PATH_FUT, cfg=Config(**BASE, distance_scale_ref=PREV_SPOT_CLOSE))
    assert _ledger(h1) == _ledger(v1)
    assert [e["event"] for e in h1.events_log] == [e["event"] for e in v1.events_log]


def test_scale_doubles_the_entry_distance():
    """ref = prev_close / 2 -> k = 2 -> D = 360: 24380 no longer triggers a long,
    24560 (= 24200 + 360) does."""
    cfg = Config(**BASE, distance_scale_ref=PREV_SPOT_CLOSE / 2)
    bt, _ = run_path([24350, 24380], [24370, 24400], cfg=cfg)
    assert bt.state.position == Side.FLAT

    bt, _ = run_path([24350, 24380, 24560], [24370, 24400, 24580], cfg=cfg)
    assert bt.state.position == Side.LONG
    assert bt.state.spot_entry_price == 24560


def test_scale_scales_the_early_stop_too():
    """k = 2 -> S = 120: a 60-point adverse move that stops V1 out does not stop H1."""
    cfg = Config(**BASE, distance_scale_ref=PREV_SPOT_CLOSE / 2)
    # enter long at 24560 (see above), then fall 100 points: V1's S=60 would fire
    bt, res = run_path([24350, 24560, 24460], [24370, 24580, 24480], cfg=cfg)
    assert bt.state.position == Side.LONG
    assert not closed_trades(res)


def test_result_config_is_the_unscaled_base():
    cfg = Config(**BASE, distance_scale_ref=PREV_SPOT_CLOSE / 2)
    _, res = run_path(PATH_SPOT, PATH_FUT, cfg=cfg)
    assert res.config is cfg
    assert res.config.swing_distance == 180.0


def test_invalid_scale_ref_rejected():
    import pytest
    with pytest.raises(ValueError):
        Config(distance_scale_ref=0).validate()
