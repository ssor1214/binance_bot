import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import scalp_bot_e3_portfolio


class PortfolioHelpersTests(unittest.TestCase):
    def test_build_strategy_profiles_fast_mode_has_multiple_profiles(self):
        profiles = scalp_bot_e3_portfolio.build_strategy_profiles("fast")
        self.assertGreaterEqual(len(profiles), 3)
        self.assertEqual(profiles[0]["id"], "speed_6x4")

    def test_filter_profiles_for_small_wallet_prefers_6x4_and_8x4(self):
        profiles = scalp_bot_e3_portfolio.build_strategy_profiles("fast")
        active = scalp_bot_e3_portfolio.filter_profiles_for_wallet(28.0, profiles)
        ids = [p["id"] for p in active]
        self.assertIn("speed_6x4", ids)
        self.assertIn("fast_8x4", ids)
        self.assertNotIn("turbo_8x3", ids)
        self.assertNotIn("fast_8x5", ids)

    def test_filter_profiles_for_large_wallet_allows_8x4_priority_band(self):
        profiles = scalp_bot_e3_portfolio.build_strategy_profiles("fast")
        active = scalp_bot_e3_portfolio.filter_profiles_for_wallet(320.0, profiles)
        ids = [p["id"] for p in active]
        self.assertIn("fast_8x4", ids)
        self.assertIn("speed_6x4", ids)
        self.assertIn("fast_8x5", ids)

    def test_score_profile_candidate_prefers_faster_turnover_profile(self):
        row = {
            "symbol": "DOGEUSDT",
            "score": 50.0,
            "price": 1.0,
            "mean_abs_1m_pct": 0.6,
            "ret_24h_pct": 2.0,
            "ret_60_pct": 1.0,
            "ret_180_pct": 2.0,
            "qty_per_rung": 10.0,
        }
        fast = scalp_bot_e3_portfolio.score_profile_candidate(row, width_pct=8.0, grid_count=5)
        slow = scalp_bot_e3_portfolio.score_profile_candidate(row, width_pct=12.0, grid_count=5)
        self.assertGreater(fast["profile_score"], slow["profile_score"])

    def test_score_profile_candidate_penalizes_overheated_symbol(self):
        calm = {
            "symbol": "DOGEUSDT",
            "score": 50.0,
            "price": 1.0,
            "mean_abs_1m_pct": 0.6,
            "ret_24h_pct": 4.0,
            "ret_60_pct": 2.0,
            "ret_180_pct": 5.0,
            "qty_per_rung": 10.0,
        }
        hot = dict(calm)
        hot["ret_24h_pct"] = 25.0
        hot["ret_60_pct"] = 14.0
        hot["ret_180_pct"] = 35.0
        calm_scored = scalp_bot_e3_portfolio.score_profile_candidate(calm, width_pct=8.0, grid_count=4)
        hot_scored = scalp_bot_e3_portfolio.score_profile_candidate(hot, width_pct=8.0, grid_count=4)
        self.assertGreater(calm_scored["profile_score"], hot_scored["profile_score"])

    @patch("scripts.scalp_bot_e3_portfolio.rank_candidate_symbols")
    def test_select_profile_candidates_filters_out_pumped_symbols(self, rank_candidate_symbols):
        rank_candidate_symbols.return_value = [
            {
                "symbol": "HOTUSDT",
                "score": 60.0,
                "price": 1.0,
                "width_pct": 12.0,
                "mean_abs_1m_pct": 0.7,
                "ret_24h_pct": 31.88,
                "ret_60_pct": 12.0,
                "ret_180_pct": 25.0,
                "spread_pct": 0.05,
                "qty_per_rung": 10.0,
            },
            {
                "symbol": "CALMUSDT",
                "score": 55.0,
                "price": 1.0,
                "width_pct": 10.0,
                "mean_abs_1m_pct": 0.6,
                "ret_24h_pct": 6.0,
                "ret_60_pct": 3.0,
                "ret_180_pct": 10.0,
                "spread_pct": 0.04,
                "qty_per_rung": 10.0,
            },
        ]
        profiles = [{"id": "speed_6x4", "width_pct": 6.0, "grid_count": 4}]
        out = scalp_bot_e3_portfolio.select_profile_candidates(
            ex=SimpleNamespace(),
            wallet_balance=28.0,
            leverage=3,
            capital_usage=0.33,
            candidate_limit=60,
            top_n=12,
            exclude_symbols=set(),
            min_width_pct=4.0,
            min_mean_abs_1m_pct=0.08,
            max_spread_pct=0.12,
            max_abs_ret_24h_pct=15.0,
            max_abs_ret_60_pct=8.0,
            max_abs_ret_180_pct=20.0,
            profiles=profiles,
        )
        self.assertEqual([row["symbol"] for row in out], ["CALMUSDT"])

    def test_parse_symbol_csv_uppercases_and_trims(self):
        parsed = scalp_bot_e3_portfolio.parse_symbol_csv(" xrpusdt, DOGEusdt ,, adausdt ")
        self.assertEqual(parsed, {"XRPUSDT", "DOGEUSDT", "ADAUSDT"})

    def test_pick_symbols_returns_unique_symbols(self):
        rows = [
            {"symbol": "XRPUSDT", "score": 10},
            {"symbol": "XRPUSDT", "score": 9},
            {"symbol": "DOGEUSDT", "score": 8},
            {"symbol": "ADAUSDT", "score": 7},
        ]
        picked = scalp_bot_e3_portfolio.pick_symbols(rows, 3)
        self.assertEqual([r["symbol"] for r in picked], ["XRPUSDT", "DOGEUSDT", "ADAUSDT"])

    def test_slot_summary_handles_empty_slot(self):
        slot = scalp_bot_e3_portfolio.SlotState(slot_id=1, capital_ratio=0.34)
        self.assertIn("비어 있음", scalp_bot_e3_portfolio.slot_summary(slot))

    def test_slot_summary_shows_strategy_id(self):
        slot = scalp_bot_e3_portfolio.SlotState(
            slot_id=1,
            capital_ratio=0.34,
            strategy_id="8x5",
            state={"symbol": "DOGEUSDT", "reset_count": 0},
            last_score=12.3,
        )
        text = scalp_bot_e3_portfolio.slot_summary(slot)
        self.assertIn("배정", text)
        self.assertIn("8x5", text)
        self.assertIn("DOGEUSDT", text)

    def test_assert_clean_account_or_raise_accepts_empty_account(self):
        ex = SimpleNamespace(
            client=SimpleNamespace(
                futures_get_open_orders=lambda: [],
                futures_position_information=lambda: [],
            )
        )
        scalp_bot_e3_portfolio.assert_clean_account_or_raise(ex)

    def test_assert_clean_account_or_raise_blocks_dirty_account(self):
        ex = SimpleNamespace(
            client=SimpleNamespace(
                futures_get_open_orders=lambda: [{"symbol": "DOGEUSDT"}],
                futures_position_information=lambda: [{"symbol": "ADAUSDT", "positionAmt": "1"}],
            )
        )
        with self.assertRaisesRegex(RuntimeError, "fresh start 차단"):
            scalp_bot_e3_portfolio.assert_clean_account_or_raise(ex)

    def test_adopt_legacy_positions_cancels_orders_and_snapshots_positions(self):
        cancelled = []
        ex = SimpleNamespace(
            cancel_regular_order=lambda symbol, order_id: cancelled.append((symbol, order_id)),
            client=SimpleNamespace(
                futures_get_open_orders=lambda: [{"symbol": "DOGEUSDT", "orderId": 11}],
                futures_position_information=lambda: [
                    {
                        "symbol": "ADAUSDT",
                        "positionAmt": "2",
                        "entryPrice": "1.2",
                        "breakEvenPrice": "1.21",
                    }
                ],
            ),
        )
        legacy = scalp_bot_e3_portfolio.adopt_legacy_positions(ex)
        self.assertEqual(cancelled, [("DOGEUSDT", 11)])
        self.assertEqual(legacy[0]["symbol"], "ADAUSDT")
        self.assertEqual(legacy[0]["break_even_price"], 1.21)

    def test_reconcile_portfolio_with_exchange_clears_stale_slot(self):
        portfolio = scalp_bot_e3_portfolio.PortfolioState(
            wallet_balance_start=28.0,
            slots=[
                scalp_bot_e3_portfolio.SlotState(
                    slot_id=1,
                    capital_ratio=0.5,
                    strategy_id="6x4",
                    state={"symbol": "DOGEUSDT"},
                    last_score=10.0,
                )
            ],
        )
        ex = SimpleNamespace(
            client=SimpleNamespace(
                futures_get_open_orders=lambda: [],
                futures_position_information=lambda: [],
            )
        )
        out = scalp_bot_e3_portfolio.reconcile_portfolio_with_exchange(ex, portfolio)
        self.assertIsNone(out.slots[0].state)
        self.assertEqual(out.slots[0].strategy_id, "")

    def test_reconcile_portfolio_with_exchange_keeps_live_slot(self):
        portfolio = scalp_bot_e3_portfolio.PortfolioState(
            wallet_balance_start=28.0,
            slots=[
                scalp_bot_e3_portfolio.SlotState(
                    slot_id=1,
                    capital_ratio=0.5,
                    strategy_id="6x4",
                    state={"symbol": "DOGEUSDT"},
                    last_score=10.0,
                )
            ],
        )
        ex = SimpleNamespace(
            client=SimpleNamespace(
                futures_get_open_orders=lambda: [{"symbol": "DOGEUSDT"}],
                futures_position_information=lambda: [],
            )
        )
        out = scalp_bot_e3_portfolio.reconcile_portfolio_with_exchange(ex, portfolio)
        self.assertEqual(out.slots[0].state["symbol"], "DOGEUSDT")

    def test_close_legacy_positions_on_profit_flattens_and_removes(self):
        closed = []
        ex = SimpleNamespace(
            get_all_positions=lambda: [{"symbol": "DOGEUSDT", "amount": 3.0, "mark_price": 1.05}],
            close_market_position=lambda symbol, side, quantity: closed.append((symbol, side, quantity)),
        )
        remaining, alerts = scalp_bot_e3_portfolio.close_legacy_positions_on_profit(
            ex,
            [{
                "symbol": "DOGEUSDT",
                "side": "LONG",
                "amount": 3.0,
                "entry_price": 1.0,
                "break_even_price": 1.01,
            }],
        )
        self.assertEqual(closed, [("DOGEUSDT", "LONG", 3.0)])
        self.assertEqual(remaining, [])
        self.assertEqual(alerts[0]["symbol"], "DOGEUSDT")

    def test_close_legacy_positions_on_profit_keeps_unprofitable_position(self):
        closed = []
        ex = SimpleNamespace(
            get_all_positions=lambda: [{"symbol": "DOGEUSDT", "amount": 3.0, "mark_price": 0.99}],
            close_market_position=lambda symbol, side, quantity: closed.append((symbol, side, quantity)),
        )
        legacy = [{
            "symbol": "DOGEUSDT",
            "side": "LONG",
            "amount": 3.0,
            "entry_price": 1.0,
            "break_even_price": 1.01,
        }]
        remaining, alerts = scalp_bot_e3_portfolio.close_legacy_positions_on_profit(ex, legacy)
        self.assertEqual(closed, [])
        self.assertEqual(remaining, legacy)
        self.assertEqual(alerts, [])

    @patch("scripts.scalp_bot_e3_portfolio.log_event")
    @patch("scripts.scalp_bot_e3_portfolio.expand_range_cycle")
    def test_maybe_rotate_slot_expands_range_before_switching(self, expand_range_cycle, _log_event):
        from scripts.scalp_bot_e3 import CycleState

        slot = scalp_bot_e3_portfolio.SlotState(
            slot_id=1,
            capital_ratio=0.5,
            strategy_id="6x4",
            width_pct=6.0,
            grid_count=4,
            state={
                "symbol": "DOGEUSDT",
                "center_price": 1.0,
                "started_at": 1.0,
                "wallet_balance_start": 28.0,
                "levels": [0.94, 0.98, 1.02, 1.06],
                "qty_per_rung": 10.0,
                "held_buy_rungs": [],
                "buy_orders": {},
                "sell_orders": {},
                "realized_grid_profit_est": 0.0,
                "reset_count": 0,
            },
            last_score=10.0,
        )
        expanded = dict(slot.state)
        expanded["levels"] = [0.90, 0.966, 1.032, 1.098]
        expand_range_cycle.return_value = CycleState(**expanded)
        ex = SimpleNamespace(get_mark_price=lambda symbol: 1.2)
        candidates = [{"symbol": "ACEUSDT", "strategy_id": "8x4", "profile_score": 999}]
        scalp_bot_e3_portfolio.maybe_rotate_slot(
            ex,
            slot,
            candidates,
            wallet_balance=28.0,
            leverage=3,
            min_switch_interval_sec=1200,
            switch_score_delta=15,
            idle_switch_sec=900,
            live=True,
        )
        expand_range_cycle.assert_called_once()
        self.assertEqual(slot.state["levels"], [0.90, 0.966, 1.032, 1.098])
        self.assertEqual(slot.strategy_id, "6x4")

    @patch("scripts.scalp_bot_e3_portfolio.start_slot")
    @patch("scripts.scalp_bot_e3_portfolio.flatten_position")
    @patch("scripts.scalp_bot_e3_portfolio.time.time", return_value=2000.0)
    def test_maybe_rotate_slot_switches_idle_unfilled_slot(self, _time_now, flatten_position, start_slot):
        from scripts.scalp_bot_e3 import CycleState

        start_slot.return_value = CycleState(
            symbol="ACEUSDT",
            center_price=1.1,
            started_at=2000.0,
            wallet_balance_start=28.0,
            levels=[1.0, 1.1, 1.2, 1.3],
            qty_per_rung=9.0,
            held_buy_rungs=[],
            buy_orders={"0": {"order_id": 501, "rung": 0, "price": 1.0, "quantity": 9.0}},
            sell_orders={},
            realized_grid_profit_est=0.0,
            reset_count=0,
            last_fill_at=2000.0,
        )
        slot = scalp_bot_e3_portfolio.SlotState(
            slot_id=1,
            capital_ratio=0.5,
            strategy_id="6x4",
            width_pct=6.0,
            grid_count=4,
            last_switch_at=0.0,
            last_score=40.0,
            state={
                "symbol": "DOGEUSDT",
                "center_price": 1.0,
                "started_at": 1000.0,
                "wallet_balance_start": 28.0,
                "levels": [0.94, 0.98, 1.02, 1.06],
                "qty_per_rung": 10.0,
                "held_buy_rungs": [],
                "buy_orders": {"0": {"order_id": 11, "rung": 0, "price": 0.94, "quantity": 10.0}},
                "sell_orders": {},
                "realized_grid_profit_est": 0.0,
                "reset_count": 0,
                "last_fill_at": 1000.0,
            },
        )
        cancelled = []
        ex = SimpleNamespace(
            get_mark_price=lambda symbol: 1.0,
            cancel_regular_order=lambda symbol, order_id: cancelled.append((symbol, order_id)),
        )
        candidates = [
            {"symbol": "ACEUSDT", "strategy_id": "8x4", "profile_width_pct": 8.0, "profile_grid_count": 4, "profile_score": 70.0},
        ]
        scalp_bot_e3_portfolio.maybe_rotate_slot(
            ex,
            slot,
            candidates,
            wallet_balance=28.0,
            leverage=3,
            min_switch_interval_sec=1200,
            switch_score_delta=15,
            idle_switch_sec=900,
            live=True,
        )
        self.assertEqual(cancelled, [("DOGEUSDT", 11)])
        flatten_position.assert_called_once_with(ex, "DOGEUSDT")
        self.assertEqual(slot.strategy_id, "8x4")
        self.assertEqual(slot.state["symbol"], "ACEUSDT")

# ===================================== 슬롯 간 심볼 중복 방지 (실사고 회귀)
class SlotSymbolCollisionTests(unittest.TestCase):
    """[2026-08-21 실사고] 두 슬롯이 동시에 BEATUSDT 로 교체됐다.

    결과: 같은 칸을 두 슬롯이 각자 매수해 재고가 의도의 2배가 됐고,
    한 슬롯이 교체하며 flatten_position 을 부르자 다른 슬롯 포지션까지 닫혔다.
    40분 만에 -2.91 USDT, 손실컷(7%) 발동으로 봇이 정지했다.
    """

    def _cands(self, *syms):
        return [{"symbol": s, "profile_score": 90.0 - i}
                for i, s in enumerate(syms)]

    def test_taken_symbols_are_filtered_from_candidates(self):
        cands = self._cands("BEATUSDT", "TUTUSDT", "HEMIUSDT")
        taken = {"BEATUSDT"}
        left = [c for c in cands if c["symbol"] not in taken]
        self.assertEqual([c["symbol"] for c in left], ["TUTUSDT", "HEMIUSDT"])

    def test_pick_symbols_dedupes_within_one_call(self):
        picked = scalp_bot_e3_portfolio.pick_symbols(
            self._cands("AAA", "AAA", "BBB"), 2)
        self.assertEqual([p["symbol"] for p in picked], ["AAA", "BBB"])

    def test_empty_slot_does_not_take_symbol_held_by_other_slot(self):
        """빈 슬롯이 이미 쓰이는 심볼을 받으면 안 된다.

        pick_symbols 는 한 번의 선택 안에서만 중복을 막으므로,
        후보 목록에서 보유 심볼을 먼저 빼야 한다.
        """
        slots = [
            scalp_bot_e3_portfolio.SlotState(
                slot_id=1, capital_ratio=0.5, state={"symbol": "BEATUSDT"}),
            scalp_bot_e3_portfolio.SlotState(slot_id=2, capital_ratio=0.5),
        ]
        held = {str((s.state or {}).get("symbol") or "") for s in slots if s.state}
        held.discard("")
        pool = [c for c in self._cands("BEATUSDT", "TUTUSDT")
                if c["symbol"] not in held]
        picked = scalp_bot_e3_portfolio.pick_symbols(pool, 2)
        self.assertEqual([p["symbol"] for p in picked], ["TUTUSDT"])

    def test_rotate_accepts_taken_symbols_argument(self):
        import inspect
        sig = inspect.signature(scalp_bot_e3_portfolio.maybe_rotate_slot)
        self.assertIn("taken_symbols", sig.parameters)

    def test_rotate_skips_when_only_candidate_is_taken(self):
        """유일한 후보가 다른 슬롯 것이면 교체 후보가 비어야 한다."""
        cands = self._cands("BEATUSDT")
        left = [c for c in cands if c["symbol"] not in {"BEATUSDT"}]
        self.assertEqual(left, [], "교체 후보가 남으면 같은 심볼로 몰린다")


class LossGuardAndStaleGridTests(unittest.TestCase):
    def _slot(self):
        return scalp_bot_e3_portfolio.SlotState(
            slot_id=1,
            capital_ratio=0.5,
            strategy_id="6x4",
            state={
                "symbol": "DOGEUSDT",
                "center_price": 1.0,
                "started_at": 1000.0,
                "wallet_balance_start": 28.0,
                "levels": [0.94, 0.98, 1.02, 1.06],
                "qty_per_rung": 10.0,
                "held_buy_rungs": [1],
                "buy_orders": {"0": {"order_id": 11, "rung": 0, "price": 0.94, "quantity": 10.0}},
                "sell_orders": {"2": {"order_id": 22, "rung": 2, "price": 1.02, "quantity": 10.0}},
                "realized_grid_profit_est": 0.0,
                "reset_count": 0,
                "last_fill_at": 1000.0,
            },
        )

    def test_evaluate_loss_guard_triggers_on_30m_net_loss(self):
        now = 2000.0
        events = [
            {"ts": now - 100.0, "income": -0.21},
            {"ts": now - 200.0, "income": -0.11},
        ]
        trigger = scalp_bot_e3_portfolio.evaluate_loss_guard(events, now, 0.30, 0.60)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["window"], "30m")
        self.assertLessEqual(trigger["net"], -0.30)

    def test_evaluate_loss_guard_ignores_old_loss_outside_window(self):
        now = 4000.0
        events = [{"ts": now - 4000.0, "income": -9.0}]
        self.assertIsNone(
            scalp_bot_e3_portfolio.evaluate_loss_guard(events, now, 0.30, 0.60)
        )

    @patch("scripts.scalp_bot_e3_portfolio.STATE")
    def test_loss_pause_is_persisted_across_resume(self, state_path):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            state_path.exists.side_effect = path.exists
            state_path.write_text.side_effect = path.write_text
            state_path.read_text.side_effect = path.read_text
            p = scalp_bot_e3_portfolio.PortfolioState(
                wallet_balance_start=28.0,
                slots=[self._slot()],
                loss_pause_until=5000.0,
                loss_guard_last_trigger_at=4500.0,
            )
            scalp_bot_e3_portfolio.save_portfolio_state(p)
            loaded = scalp_bot_e3_portfolio.load_portfolio_state()
            self.assertEqual(loaded.loss_pause_until, 5000.0)
            self.assertEqual(loaded.loss_guard_last_trigger_at, 4500.0)

    @patch("scripts.scalp_bot_e3_portfolio.log_event")
    def test_apply_loss_pause_cancels_buys_but_keeps_sells_and_held(self, _log_event):
        cancelled = []
        ex = SimpleNamespace(cancel_regular_order=lambda sym, oid: cancelled.append((sym, oid)))
        slot = self._slot()
        p = scalp_bot_e3_portfolio.PortfolioState(wallet_balance_start=28.0, slots=[slot])
        scalp_bot_e3_portfolio.apply_loss_pause(
            ex,
            p,
            {"window": "30m", "net": -0.31, "limit": -0.30},
            pause_sec=3600,
            now=2000.0,
            live=True,
        )
        self.assertEqual(cancelled, [("DOGEUSDT", 11)])
        self.assertEqual(slot.state["buy_orders"], {})
        self.assertEqual(slot.state["sell_orders"]["2"]["order_id"], 22)
        self.assertEqual(slot.state["held_buy_rungs"], [1])
        self.assertEqual(p.loss_pause_until, 5600.0)

    @patch("scripts.scalp_bot_e3_portfolio.reconcile_cycle_state_with_exchange")
    @patch("scripts.scalp_bot_e3_portfolio.log_event")
    def test_monitor_stale_slot_grid_clears_old_unfilled_buys_only(self, _log_event, reconcile):
        slot = self._slot()
        slot.state["held_buy_rungs"] = []
        slot.state["sell_orders"] = {}
        cancelled = []
        ex = SimpleNamespace(cancel_regular_order=lambda sym, oid: cancelled.append((sym, oid)))
        from scripts.scalp_bot_e3 import CycleState
        reconcile.return_value = CycleState(**slot.state)
        result = scalp_bot_e3_portfolio.monitor_stale_slot_grid(
            ex, slot, now=4000.0, stale_buy_sec=1800.0, live=True
        )
        self.assertEqual(result["cancelled_stale_buys"], 1)
        self.assertEqual(cancelled, [("DOGEUSDT", 11)])
        self.assertEqual(slot.state["buy_orders"], {})

    def test_legacy_filter_excludes_symbols_already_managed_by_slots(self):
        portfolio = scalp_bot_e3_portfolio.PortfolioState(
            wallet_balance_start=28.0,
            slots=[self._slot()],
        )
        legacy = [
            {"symbol": "DOGEUSDT", "side": "LONG"},
            {"symbol": "ADAUSDT", "side": "LONG"},
        ]
        out = scalp_bot_e3_portfolio.filter_legacy_positions(
            legacy,
            scalp_bot_e3_portfolio.active_slot_symbols(portfolio),
        )
        self.assertEqual(out, [{"symbol": "ADAUSDT", "side": "LONG"}])

    @patch("scripts.scalp_bot_e3_portfolio.log_event")
    def test_repair_orphan_position_restores_nearest_buy_rung(self, _log_event):
        from scripts.scalp_bot_e3 import CycleState
        state = CycleState(**self._slot().state)
        state.held_buy_rungs = []
        state.sell_orders = {}
        ex = SimpleNamespace(
            get_position=lambda symbol: {
                "symbol": symbol,
                "amount": 14.0,
                "entry_price": 0.981,
                "side": "LONG",
            }
        )
        repaired = scalp_bot_e3_portfolio.repair_orphan_position_held_rung(ex, state)
        self.assertTrue(repaired)
        self.assertEqual(state.held_buy_rungs, [1])

if __name__ == "__main__":
    unittest.main()


class ResumeDefaultTests(unittest.TestCase):
    """[2026-08-21 실사고] --resume 을 빼먹으면 adopt_legacy_positions() 가
    계좌의 미체결 주문을 전부 취소한다. 그 주문들이 곧 격자다.
    하룻밤에 LEGACY_ADOPT 가 22번 찍혔고 매번 격자가 지워졌다.
    매수 39건 대 매도 3건(완결률 7.7%)이 그 결과다.
    """

    def _parse(self, argv):
        import contextlib, io, sys
        from unittest.mock import patch as _p
        src = inspect.getsource(scalp_bot_e3_portfolio.main)
        self.assertIn('"--resume"', src)
        return src

    def test_resume_defaults_to_true(self):
        src = inspect.getsource(scalp_bot_e3_portfolio.main)
        self.assertIn('"--resume", action="store_true", default=True', src,
                      "--resume 이 기본값이 아니면 재시작마다 격자가 지워진다")

    def test_fresh_start_flag_exists(self):
        src = inspect.getsource(scalp_bot_e3_portfolio.main)
        self.assertIn('"--fresh-start"', src,
                      "새로 시작하는 경로가 명시적으로 있어야 한다")
        self.assertIn('dest="resume", action="store_false"', src)


class ConservativePortfolioDefaultsTests(unittest.TestCase):
    def test_defaults_are_two_slot_two_x_idle_30m(self):
        src = inspect.getsource(scalp_bot_e3_portfolio.main)
        self.assertIn('"--slots", type=int, default=2', src)
        self.assertIn('"--leverage", type=int, default=2', src)
        self.assertIn('"--idle-switch-sec", type=float, default=1800.0', src)

    def test_portfolio_rejects_non_two_x_leverage(self):
        src = inspect.getsource(scalp_bot_e3_portfolio.main)
        self.assertIn("if args.leverage != 2:", src)
        self.assertIn("격리 2배 고정", src)
