"""
Backtest engine -- spec sections 13 (tick processing order) and 14 (loop).

Spec 13: "Do not rearrange this sequence. The source strategy is
path-dependent and same-tick precedence changes outcomes."

on_spot_tick() below follows the spec's numbered steps in order. The only
departure is step 2, where R2 (see DECISIONS.md) controls whether reference
extremes also widen while a position is open.

Updating the extremes before evaluating an entry is self-consistent and
cannot self-trigger: if running_high is widened to the current spot, then
spot_short_trigger = spot - D and the condition spot <= spot - D is false
for any D > 0. Same for the long side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import datetime as dt
import copy

from .config import Config
from .state import State, Side, PendingType, MarketSnapshot
from .signals import SignalEngine
from .pending_manager import PendingManager
from .position_manager import PositionManager
from .breaker import Breaker
from .gap_manager import GapManager
from .execution import ExecutionSimulator
from .pnl import TradeLedger, Trade
from .data import MarketData, SessionReference


@dataclass
class BacktestResult:
    config: Config
    ledger: TradeLedger
    executions: list
    events_log: List[Dict[str, Any]]
    reconciliation: dict
    sessions_processed: int
    final_state: Optional[State] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def trades(self) -> List[Trade]:
        return self.ledger.trades


class Backtester:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self.cfg.validate()
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.state = State()
        self.sim = ExecutionSimulator(self.cfg)
        self.ledger = TradeLedger()
        self.signals = SignalEngine(self.cfg)
        self.pending = PendingManager(self.cfg)
        self.positions = PositionManager(self.cfg, self.sim, self.ledger)
        self.breaker = Breaker(self.cfg)
        self.gap = GapManager(self.cfg)
        self.events_log: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self._sessions = 0

    # ------------------------------------------------------------------
    def log(self, ts, kind: str, **kw) -> None:
        rec = {"timestamp": ts, "event": kind}
        rec.update(kw)
        self.events_log.append(rec)

    # ==================================================================
    # Session loop -- spec 14
    # ==================================================================
    def run(self, market: MarketData) -> BacktestResult:
        self.reset()
        st = self.state

        last_event: Optional[MarketSnapshot] = None

        for trade_date, events in market.sessions():
            ref = market.references[trade_date]
            self.initialize_session(st, ref)
            self._sessions += 1

            if ref.is_warmup:
                for ev in events:               # build references only
                    self.update_latest_market_state(st, ev)
                    last_event = ev
                self.process_session_close(st, warmup=True)
                continue

            for ev in events:
                self.update_latest_market_state(st, ev)
                last_event = ev

                # expiry square-off (spec 11)
                if self.is_expiry_squareoff_time(st, ev.timestamp):
                    self.force_expiry_exit(st, ev.timestamp)
                    continue

                # morning gap test (spec 10)
                decision = self.gap.should_run_gap_test(
                    st, ev.timestamp, is_spot=(ev.spot_ltp is not None)
                )
                if decision == "SKIP":
                    st.gap_tested = True
                    self.log(ev.timestamp, "GAP_TEST_SKIPPED", reason="no tick in window")
                elif decision == "RUN":
                    self.evaluate_morning_gap(st, ev)

                if ev.spot_ltp is not None:
                    self.on_spot_tick(st, ev.spot_ltp, st.last_futures, ev.timestamp)

            self.process_session_close(st)

        # Dataset end: snapshot the live state BEFORE any forced close, so the
        # terminal position is inspectable (scenario tests rely on this).
        final_state = copy.deepcopy(st)

        if self.cfg.close_at_dataset_end and not st.is_flat() and last_event is not None:
            self.positions.close_position(
                st,
                st.last_spot,
                st.last_futures,
                last_event.timestamp,
                reason="DATASET_END",
            )
            self.warnings.append(
                "A position was open at the end of the dataset and was closed "
                "at the final tick with exit_reason=DATASET_END. Exclude it from "
                "headline statistics if you want realised-only results."
            )

        recon = self.ledger.reconcile(self.sim.executions)
        return BacktestResult(
            config=self.cfg,
            ledger=self.ledger,
            executions=self.sim.executions,
            events_log=self.events_log,
            reconciliation=recon,
            sessions_processed=self._sessions,
            warnings=self.warnings,
            final_state=final_state,
        )

    # ------------------------------------------------------------------
    def initialize_session(self, st: State, ref: SessionReference) -> None:
        # R7: futures contract roll
        rolled = (
            st.current_contract_expiry is not None
            and ref.contract_expiry is not None
            and ref.contract_expiry != st.current_contract_expiry
        )
        if rolled:
            self.log(None, "FUTURES_ROLL",
                     old=st.current_contract_expiry, new=ref.contract_expiry)
            st.futures_running_high = None
            st.futures_running_low = None
            st.prev_fut_high = None
            st.prev_fut_low = None
            if not st.is_flat():
                self.warnings.append(
                    f"Position was open across a futures roll into {ref.contract_expiry}. "
                    "Expiry square-off should have prevented this; check expiry flags."
                )

        st.trade_date = ref.trade_date
        st.current_contract_expiry = ref.contract_expiry
        st.is_expiry_day = ref.is_expiry_day
        st.expiry_squareoff_done = False

        st.prev_spot_high = ref.prev_spot_high
        st.prev_spot_low = ref.prev_spot_low
        st.prev_spot_close = ref.prev_spot_close
        if not rolled:
            st.prev_fut_high = ref.prev_fut_high
            st.prev_fut_low = ref.prev_fut_low

        # references reset to the two-day window (prev session + today)
        st.session_spot_high = None
        st.session_spot_low = None
        st.session_fut_high = None
        st.session_fut_low = None
        st.running_high = ref.prev_spot_high
        st.running_low = ref.prev_spot_low
        st.futures_running_high = None if rolled else ref.prev_fut_high
        st.futures_running_low = None if rolled else ref.prev_fut_low

        # gap / suppression flags reset daily
        st.gap_regime = False
        st.gap_fill_price = None
        st.gap_hold_only_partial = False
        st.gap_tested = False
        st.suppress_first_entries = False
        st.suppress_reversals = False
        st.trading_disabled_today = False

        if not st.is_flat():
            st.overnight_flag = True

        # carry anchor (spec 11)
        if self.pending.restore_carry_anchor(st, ref.contract_expiry):
            self.log(None, "CARRY_ANCHOR_RESTORED",
                     side=st.pending_side.label, spot_level=st.pending_spot_level)

    # ------------------------------------------------------------------
    def update_latest_market_state(self, st: State, ev: MarketSnapshot) -> None:
        st.last_timestamp = ev.timestamp
        if ev.futures_ltp is not None:
            st.last_futures = ev.futures_ltp
            if self.signals.should_update_extremes(st):
                self.signals.update_futures_reference_extremes(st, ev.futures_ltp)
        if ev.spot_ltp is not None:
            st.last_spot = ev.spot_ltp
            st.session_spot_close = ev.spot_ltp
            if st.session_spot_open is None:
                st.session_spot_open = ev.spot_ltp

    # ------------------------------------------------------------------
    def is_expiry_squareoff_time(self, st: State, ts: dt.datetime) -> bool:
        return st.is_expiry_day and ts.time() >= self.cfg.t_expiry_squareoff

    def force_expiry_exit(self, st: State, ts: dt.datetime) -> None:
        if not st.expiry_squareoff_done:
            if not st.is_flat():
                self.positions.close_position(
                    st, st.last_spot, st.last_futures, ts, reason="EXPIRY_SQUAREOFF"
                )
                self.breaker.on_flat_without_reversal(st)
                self.log(ts, "EXPIRY_SQUAREOFF", spot=st.last_spot)
            st.clear_pending()
            self.pending.clear_carry_anchor(st)
            st.trading_disabled_today = True
            st.suppress_first_entries = True
            st.suppress_reversals = True
            st.expiry_squareoff_done = True

    # ------------------------------------------------------------------
    def evaluate_morning_gap(self, st: State, ev: MarketSnapshot) -> None:
        outcome = self.gap.evaluate(st, ev.spot_ltp, st.last_futures)
        if outcome is None:
            return
        self.log(ev.timestamp, f"GAP_{outcome}", spot=ev.spot_ltp,
                 prev_close=st.prev_spot_close)
        if outcome == "ADVERSE" and not st.is_flat():
            self.positions.close_position(
                st, ev.spot_ltp, st.last_futures, ev.timestamp,
                reason="ADVERSE_GAP_ASSUMED_MANUAL_EXIT",
            )
            self.breaker.on_flat_without_reversal(st)

    # ==================================================================
    # Tick processing -- spec 13
    # ==================================================================
    def on_spot_tick(
        self, st: State, spot_ltp: float, futures_ltp: Optional[float], ts: dt.datetime
    ) -> None:

        # 1. Gap fill first
        self.gap.check_gap_fill(st, spot_ltp)

        # 2. Reference extremes (R2 controls in-position widening)
        if self.signals.should_update_extremes(st):
            self.signals.update_spot_reference_extremes(st, spot_ltp)

        # 3. Resolve pending slot
        if st.is_flat() and st.has_pending():
            side = self.pending.process(st, spot_ltp, futures_ltp)
            if side is not None:
                st.clear_pending()
                if self._entries_allowed(st) and futures_ltp is not None:
                    self.positions.open_position(
                        st, side, spot_ltp, futures_ltp, ts, reason="ENTRY_PENDING"
                    )
                    self.log(ts, "ENTRY_PENDING_CONFIRMED",
                             side=side.label, spot=spot_ltp, futures=futures_ltp)

        # 4. Still flat with no pending -> evaluate a new entry
        if st.is_flat() and not st.has_pending() and self._entries_allowed(st):
            self._evaluate_new_entry(st, spot_ltp, futures_ltp, ts)

        # 5. Manage an open position on the same tick
        if not st.is_flat():
            self._accrue_unprotected_time(st, ts)
            self.positions.update_best_price_and_mfe(st, spot_ltp)
            self._check_early_stop_disarm(st)
            self.breaker.check_deferred_flip_recovery(st, spot_ltp)

            early_fired = self._check_early_stop(st, spot_ltp, futures_ltp, ts)
            if not early_fired and not st.is_flat():
                self._check_partial_profit(st, spot_ltp, futures_ltp, ts)
            if not st.is_flat():
                self._check_trailing_stop_or_flip(st, spot_ltp, futures_ltp, ts)

    # ------------------------------------------------------------------
    def _entries_allowed(self, st: State) -> bool:
        return not (st.suppress_first_entries or st.trading_disabled_today)

    # ------------------------------------------------------------------
    def _evaluate_new_entry(
        self, st: State, spot_ltp: float, futures_ltp: Optional[float], ts: dt.datetime
    ) -> None:
        side, confirmed = self.signals.evaluate_entry(st, spot_ltp, futures_ltp)
        if side is None:
            return

        if confirmed:
            self.positions.open_position(st, side, spot_ltp, futures_ltp, ts)
            self.log(ts, "ENTRY_IMMEDIATE", side=side.label,
                     spot=spot_ltp, futures=futures_ltp)
            return

        # Spec 5.4: freeze the current spot trigger and required futures trigger
        s_short, s_long = self.signals.spot_triggers(st)
        f_short, f_long = self.signals.futures_triggers(st)
        spot_level = s_short if side == Side.SHORT else s_long
        fut_level = f_short if side == Side.SHORT else f_long

        self.pending.create(st, side, spot_level, fut_level, PendingType.FIRST_ENTRY)
        self.log(ts, "PENDING_CREATED", side=side.label,
                 spot_level=spot_level, futures_level=fut_level, type="FIRST_ENTRY")

    # ------------------------------------------------------------------
    def _check_early_stop_disarm(self, st: State) -> None:
        """Spec 8.1: once MFE reaches A the early stop is permanently off."""
        if st.mfe_points >= self.cfg.early_arm_distance:
            if st.early_stop_active:
                st.early_stop_active = False
                self.log(st.last_timestamp, "EARLY_STOP_DISARMED",
                         trade_id=st.trade_id, mfe=st.mfe_points)
            self.breaker.on_arm(st)

    # ------------------------------------------------------------------
    def _check_early_stop(
        self, st: State, spot_ltp: float, futures_ltp: float, ts: dt.datetime
    ) -> bool:
        if not st.early_stop_active:
            return False
        if not self.gap.early_stop_allowed(st):     # spec 10.1: DISALLOW early stop
            return False
        if st.deferred_flip_active:                 # already suppressed for this trade
            return False
        if st.r_points(spot_ltp) > -self.cfg.early_stop_distance:
            return False

        suppressed = self.breaker.register_early_stop(st)

        if suppressed:
            # Spec 9: DO_NOT_CLOSE / DO_NOT_REVERSE / KEEP_CURRENT_POSITION
            self.breaker.engage_deferred_flip(st)
            st.deferred_started_at = ts
            self.log(ts, "BREAKER_ENGAGED", trade_id=st.trade_id,
                     consecutive=st.consecutive_early_stops,
                     deferred_price=st.deferred_flip_price,
                     r=st.r_points(spot_ltp))
            return True

        if st.suppress_reversals or st.trading_disabled_today:
            self.positions.close_position(st, spot_ltp, futures_ltp, ts, "EARLY_STOP")
            self.breaker.on_flat_without_reversal(st)
            self.log(ts, "EARLY_STOP_CLOSE_NO_REVERSE", spot=spot_ltp)
            return True

        # Spec 8.2: normal early-stop reversal
        old = st.position
        self.breaker.on_reversal_flat(st)           # R1: no-op by default
        self.positions.reverse(st, spot_ltp, futures_ltp, ts, "EARLY_STOP")
        self.log(ts, "EARLY_STOP_REVERSAL", from_side=old.label,
                 to_side=st.position.label, spot=spot_ltp,
                 consecutive=st.consecutive_early_stops)
        return True

    # ------------------------------------------------------------------
    def _check_partial_profit(
        self, st: State, spot_ltp: float, futures_ltp: float, ts: dt.datetime
    ) -> None:
        if st.partial_done:
            return
        if st.r_points(spot_ltp) < self.cfg.partial_profit_distance:
            return
        self.positions.execute_partial(st, spot_ltp, futures_ltp, ts)
        self.log(ts, "PARTIAL", trade_id=st.trade_id, spot=spot_ltp,
                 qty=self.cfg.partial_qty, r=st.r_points(spot_ltp))

    # ------------------------------------------------------------------
    def _check_trailing_stop_or_flip(
        self, st: State, spot_ltp: float, futures_ltp: float, ts: dt.datetime
    ) -> None:
        # deferred flip takes precedence (spec 9)
        if self.breaker.deferred_flip_due(st, spot_ltp):
            self._accrue_unprotected_time(st, ts, final=True)
            unprotected = st.deferred_unprotected_seconds   # cleared by close_position
            old = st.position
            closed, _ = self.positions.reverse(
                st, spot_ltp, futures_ltp, ts, "DEFERRED_FLIP"
            )
            closed.deferred_unprotected_seconds = unprotected
            self.breaker.on_deferred_flip_executed(st)       # R3
            self.log(ts, "DEFERRED_FLIP_EXECUTED", from_side=old.label,
                     to_side=st.position.label, spot=spot_ltp)
            return

        if self.gap.trailing_suspended(st):        # spec 10.1: DISALLOW trailing stop
            return

        if st.mfe_points < self.cfg.early_arm_distance:
            return
        if st.drawback(spot_ltp) < self.cfg.trail_distance:
            return

        direction = st.position
        self.positions.close_position(st, spot_ltp, futures_ltp, ts, "TRAIL_STOP")
        self.breaker.on_trail_exit(st)
        self.log(ts, "TRAIL_STOP", side=direction.label, spot=spot_ltp,
                 mfe=0.0, drawback=self.cfg.trail_distance)

        self.signals.seed_references_from_trade(
            st, st.last_trade_best, st.last_trade_worst
        )
        self._arm_opposite_reentry(st, direction.opposite, spot_ltp, futures_ltp, ts)

    # ------------------------------------------------------------------
    def _arm_opposite_reentry(
        self,
        st: State,
        want: Side,
        spot_ltp: float,
        futures_ltp: Optional[float],
        ts: dt.datetime,
    ) -> None:
        """
        Spec 8.4: "A trail exit closes the remaining position but does not
        automatically reverse. An opposite re-entry must satisfy the futures
        confirmation gate."

        Only the opposite side is armed here. If it is not triggered, normal
        flat-state logic resumes on the next tick.
        """
        if not self._entries_allowed(st):
            return

        side, confirmed = self.signals.evaluate_entry(st, spot_ltp, futures_ltp)
        if side != want:
            s_short, s_long = self.signals.spot_triggers(st)
            level = s_short if want == Side.SHORT else s_long
            if level is None:
                return
            triggered = spot_ltp <= level if want == Side.SHORT else spot_ltp >= level
            if not triggered:
                return
            f_short, f_long = self.signals.futures_triggers(st)
            flevel = f_short if want == Side.SHORT else f_long
            confirmed = (
                futures_ltp is not None and flevel is not None
                and (futures_ltp <= flevel if want == Side.SHORT else futures_ltp >= flevel)
            )
            side = want

        if confirmed and self.cfg.reentry_same_tick:
            self.positions.open_position(
                st, want, spot_ltp, futures_ltp, ts, reason="REENTRY"
            )
            self.log(ts, "REENTRY_IMMEDIATE", side=want.label, spot=spot_ltp)
            return

        s_short, s_long = self.signals.spot_triggers(st)
        f_short, f_long = self.signals.futures_triggers(st)
        self.pending.create(
            st,
            want,
            s_short if want == Side.SHORT else s_long,
            f_short if want == Side.SHORT else f_long,
            PendingType.REENTRY,
        )
        self.log(ts, "PENDING_CREATED", side=want.label, type="REENTRY")

    # ------------------------------------------------------------------
    def _accrue_unprotected_time(
        self, st: State, ts: dt.datetime, final: bool = False
    ) -> None:
        if st.deferred_flip_active and st.deferred_started_at is not None:
            st.deferred_unprotected_seconds = (ts - st.deferred_started_at).total_seconds()

    # ------------------------------------------------------------------
    def process_session_close(self, st: State, warmup: bool = False) -> None:
        """Spec 11: carry overnight on non-expiry days; store a first-entry anchor."""
        if warmup:
            st.clear_pending()
            return

        stored = self.pending.store_carry_anchor(st)
        if stored:
            self.log(st.last_timestamp, "CARRY_ANCHOR_STORED",
                     side=st.carry_anchor_side.label,
                     spot_level=st.carry_anchor_spot_level)

        if not st.is_flat():
            self.log(st.last_timestamp, "OVERNIGHT_CARRY",
                     trade_id=st.trade_id, side=st.position.label,
                     spot=st.last_spot)
            trade = self.ledger.get(st.trade_id)
            trade.overnight_flag = True
            trade.sessions_spanned += 1
