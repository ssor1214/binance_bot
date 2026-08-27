import json
import tempfile
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.grid_e3 import GridState
from scripts import scalp_bot_e3


class FakeExchange:
    def __init__(self, qty=1.25, wallet=30.0, position=None):
        self.qty = qty
        self.wallet = wallet
        self.position = position
        self.cancelled = []
        self.closed = []

    def round_quantity(self, symbol, qty, price=None, max_notional=None):
        return self.qty

    def round_price(self, symbol, price):
        return round(float(price), 4)

    def get_total_margin_balance(self):
        return self.wallet

    def cancel_regular_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))

    def get_position(self, symbol):
        return self.position

    def close_market_position(self, symbol, side, quantity):
        self.closed.append((symbol, side, quantity))

    def cancel_regular_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))


class E3ScriptTests(unittest.TestCase):
    def test_cm_ultimate_defaults_match_sma_output_on_current_resolution(self):
        rows = []
        for i in range(1, 80):
            rows.append({
                "open_time": pd.Timestamp("2026-08-25 00:00:00") + pd.Timedelta(minutes=3 * i),
                "open": float(i),
                "high": float(i) + 0.5,
                "low": float(i) - 0.5,
                "close": float(i),
                "volume": 100.0 + i,
            })
        df = pd.DataFrame(rows)
        out = scalp_bot_e3.cm_ultimate_ma_mtf_v2(df)
        self.assertIsNotNone(out)
        expected = sum(range(60, 80)) / 20.0
        self.assertAlmostEqual(out["out1"], expected)
        self.assertTrue(out["ma_up"])
        self.assertFalse(out["cr_up"])
        self.assertFalse(out["cr_down"])

    def test_cm_ultimate_custom_resolution_aligns_latest_htf_value(self):
        base_rows = []
        for i in range(12):
            base_rows.append({
                "open_time": pd.Timestamp("2026-08-25 00:00:00") + pd.Timedelta(minutes=3 * i),
                "open": 10.0 + i,
                "high": 10.4 + i,
                "low": 9.6 + i,
                "close": 10.0 + i,
                "volume": 50.0 + i,
            })
        htf_rows = []
        for i in range(4):
            htf_rows.append({
                "open_time": pd.Timestamp("2026-08-25 00:00:00") + pd.Timedelta(minutes=9 * i),
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.0 + i,
                "volume": 200.0 + i,
            })
        settings = scalp_bot_e3.CMUltimateMASettings(use_current_resolution=False, len=2)
        out = scalp_bot_e3.cm_ultimate_ma_mtf_v2(pd.DataFrame(base_rows), pd.DataFrame(htf_rows), settings)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["out1"], (102.0 + 103.0) / 2.0)

    def test_cm_ultimate_hull_and_cross_flags_are_available(self):
        rows = []
        closes = [10, 9, 8, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        for i, close in enumerate(closes):
            rows.append({
                "open_time": pd.Timestamp("2026-08-25 00:00:00") + pd.Timedelta(minutes=3 * i),
                "open": close - 0.6,
                "high": close + 0.5,
                "low": close - 0.8,
                "close": float(close),
                "volume": 100.0,
            })
        settings = scalp_bot_e3.CMUltimateMASettings(
            len=4,
            atype=4,
            doma2=True,
            len2=3,
            atype2=1,
            spc=True,
            spc2=True,
            smoothe=1,
        )
        out = scalp_bot_e3.cm_ultimate_ma_mtf_v2(pd.DataFrame(rows), settings=settings)
        self.assertIsNotNone(out)
        self.assertIn("out2", out)
        self.assertIn("crossed", out)
        self.assertIsInstance(out["ma_up"], bool)

    def test_load_state_restores_saved_cycle_state(self):
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            payload = {
                "symbol": "BTCUSDT",
                "center_price": 100.0,
                "started_at": 1.0,
                "wallet_balance_start": 28.0,
                "levels": [90.0, 95.0, 100.0, 105.0, 110.0],
                "qty_per_rung": 1.25,
                "held_buy_rungs": [1],
                "buy_orders": {"0": {"order_id": 10, "rung": 0, "price": 90.0, "quantity": 1.25}},
                "sell_orders": {"2": {"order_id": 20, "rung": 2, "price": 100.0, "quantity": 1.25}},
                "realized_grid_profit_est": 3.5,
                "reset_count": 2,
            }
            state_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            with patch.object(scalp_bot_e3, "STATE", state_path):
                state = scalp_bot_e3.load_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.symbol, "BTCUSDT")
        self.assertEqual(state.held_buy_rungs, [1])
        self.assertIn("0", state.buy_orders)
        self.assertIn("2", state.sell_orders)
        self.assertEqual(state.reset_count, 2)

    def test_make_cycle_state_raises_when_min_notional_not_met(self):
        ex = FakeExchange(qty=0.0, wallet=28.0)
        with self.assertRaisesRegex(RuntimeError, "최소주문금액"):
            scalp_bot_e3.make_cycle_state(
                ex, "BTCUSDT", 100.0, 28.0, 10.0, 16, 3, 0.65
            )

    @patch("scripts.scalp_bot_e3.log_event")
    def test_process_dry_fills_updates_inventory_and_realized_profit(self, _log_event):
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=2.0,
            held_buy_rungs=[],
            buy_orders={
                "1": {"order_id": -2, "rung": 1, "price": 95.0, "quantity": 2.0}
            },
            sell_orders={},
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_dry_fills(state, grid, 94.0)
        self.assertEqual(state.held_buy_rungs, [1])
        self.assertFalse(state.buy_orders)

        state.sell_orders = {
            "2": {"order_id": -1002, "rung": 2, "price": 100.0, "quantity": 2.0}
        }
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_dry_fills(state, grid, 101.0)
        self.assertEqual(state.held_buy_rungs, [])
        self.assertFalse(state.sell_orders)
        self.assertAlmostEqual(state.realized_grid_profit_est, 10.0)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_recenter_cycle_cancels_orders_and_resets_state(self, _log_event):
        ex = FakeExchange(qty=1.0, wallet=27.5)
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=1.0,
            held_buy_rungs=[1],
            buy_orders={
                "0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 1.0}
            },
            sell_orders={
                "2": {"order_id": 22, "rung": 2, "price": 100.0, "quantity": 1.0}
            },
            reset_count=3,
        )
        new_state = scalp_bot_e3.recenter_cycle(
            ex, state, 120.0, 10.0, 3, 0.65, live=False
        )
        self.assertEqual(ex.cancelled, [("BTCUSDT", 11), ("BTCUSDT", 22)])
        self.assertEqual(new_state.reset_count, 4)
        self.assertEqual(new_state.center_price, 120.0)
        self.assertEqual(new_state.wallet_balance_start, 27.5)
        self.assertEqual(new_state.held_buy_rungs, [])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_recenter_cycle_flattens_live_position_when_requested(self, _log_event):
        ex = FakeExchange(qty=1.0, wallet=27.5, position={"side": "LONG", "amount": 3.5})
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=1.0,
        )
        scalp_bot_e3.recenter_cycle(ex, state, 120.0, 10.0, 3, 0.65, live=True)
        self.assertEqual(ex.closed, [("BTCUSDT", "LONG", 3.5)])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_expand_range_cycle_widens_range_without_flatten(self, _log_event):
        ex = FakeExchange(qty=1.0, wallet=27.5, position={"side": "LONG", "amount": 3.5})
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=1.0,
            held_buy_rungs=[1],
            buy_orders={
                "0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 1.0}
            },
            sell_orders={
                "2": {"order_id": 22, "rung": 2, "price": 100.0, "quantity": 1.0}
            },
            realized_grid_profit_est=1.25,
            reset_count=3,
        )
        new_state = scalp_bot_e3.expand_range_cycle(ex, state, 130.0, width_mult=1.5)
        self.assertEqual(ex.cancelled, [("BTCUSDT", 11), ("BTCUSDT", 22)])
        self.assertEqual(ex.closed, [])
        self.assertTrue(new_state.levels[0] <= 130.0 <= new_state.levels[-1])
        self.assertEqual(new_state.center_price, 100.0)
        self.assertEqual(new_state.wallet_balance_start, 28.0)
        self.assertEqual(new_state.qty_per_rung, 1.0)
        self.assertEqual(new_state.held_buy_rungs, [1])
        self.assertEqual(new_state.buy_orders, {})
        self.assertEqual(new_state.sell_orders, {})
        self.assertEqual(new_state.realized_grid_profit_est, 1.25)
        self.assertEqual(new_state.reset_count, 4)

    @patch("scripts.scalp_bot_e3.save_state")
    @patch("scripts.scalp_bot_e3.log_event")
    def test_handle_global_stop_cancels_orders_without_live_flatten_in_dry_mode(self, _log_event, save_state):
        ex = FakeExchange()
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=1.0,
            buy_orders={"0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 1.0}},
            sell_orders={"2": {"order_id": 22, "rung": 2, "price": 100.0, "quantity": 1.0}},
        )
        stopped = scalp_bot_e3.handle_global_stop(
            ex, state, "BTCUSDT", wallet_balance=25.0, max_loss_pct=7.0, live=False
        )
        self.assertTrue(stopped)
        self.assertEqual(ex.cancelled, [("BTCUSDT", 11), ("BTCUSDT", 22)])
        self.assertEqual(ex.closed, [])
        save_state.assert_called_once_with(state)

    @patch("scripts.scalp_bot_e3.save_state")
    @patch("scripts.scalp_bot_e3.log_event")
    def test_handle_global_stop_flattens_live_position_when_live(self, _log_event, save_state):
        ex = FakeExchange(position={"side": "LONG", "amount": 2.0})
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=1.0,
        )
        stopped = scalp_bot_e3.handle_global_stop(
            ex, state, "BTCUSDT", wallet_balance=25.0, max_loss_pct=7.0, live=True
        )
        self.assertTrue(stopped)
        self.assertEqual(ex.closed, [("BTCUSDT", "LONG", 2.0)])
        save_state.assert_called_once_with(state)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_process_live_fills_ignores_none_and_partial_status(self, _log_event):
        ex = FakeExchange()
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=2.0,
            buy_orders={"1": {"order_id": 101, "rung": 1, "price": 95.0, "quantity": 2.0}},
            sell_orders={"2": {"order_id": 202, "rung": 2, "price": 100.0, "quantity": 2.0}},
            held_buy_rungs=[1],
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        with patch("scripts.scalp_bot_e3.regular_order_status", side_effect=[None, {"status": "PARTIALLY_FILLED"}]):
            scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertIn("1", state.buy_orders)
        self.assertIn("2", state.sell_orders)
        self.assertEqual(state.held_buy_rungs, [1])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_process_live_fills_applies_filled_status_to_buy_and_sell(self, _log_event):
        ex = FakeExchange()
        state = scalp_bot_e3.CycleState(
            symbol="BTCUSDT",
            center_price=100.0,
            started_at=1.0,
            wallet_balance_start=28.0,
            levels=[90.0, 95.0, 100.0, 105.0, 110.0],
            qty_per_rung=2.0,
            buy_orders={"1": {"order_id": 101, "rung": 1, "price": 95.0, "quantity": 2.0}},
            sell_orders={},
            held_buy_rungs=[],
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        with patch("scripts.scalp_bot_e3.regular_order_status", return_value={"status": "FILLED"}):
            scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertEqual(state.held_buy_rungs, [1])
        self.assertFalse(state.buy_orders)
        self.assertGreater(state.last_fill_at, 0.0)

        state.sell_orders = {"2": {"order_id": 202, "rung": 2, "price": 100.0, "quantity": 2.0}}
        grid = GridState(state.levels, set(state.held_buy_rungs))
        with patch("scripts.scalp_bot_e3.regular_order_status", return_value={"status": "FILLED"}):
            scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertEqual(state.held_buy_rungs, [])
        self.assertFalse(state.sell_orders)
        self.assertAlmostEqual(state.realized_grid_profit_est, 10.0)
        self.assertGreater(state.last_fill_at, 0.0)

class StatusExchange(FakeExchange):
    """주문 상태를 시나리오대로 돌려주는 거래소.

    status_map: order_id -> 반환값. 등록되지 않은 id 는 조회 실패(예외)로 처리한다.
    """

    def __init__(self, status_map, **kwargs):
        super().__init__(**kwargs)
        self.status_map = status_map
        self.asked = []

    def get_order_status(self, symbol, order_id):
        self.asked.append((symbol, order_id))
        oid = int(order_id)
        if oid not in self.status_map:
            raise RuntimeError("주문을 찾을 수 없습니다")
        return self.status_map[oid]


def _state(**over):
    base = dict(
        symbol="BTCUSDT",
        center_price=100.0,
        started_at=1.0,
        wallet_balance_start=28.0,
        levels=[90.0, 95.0, 100.0, 105.0, 110.0],
        qty_per_rung=2.0,
    )
    base.update(over)
    return scalp_bot_e3.CycleState(**base)


# ------------------------------------------------------ 1) --resume 상태 복구
class E3ResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "state.json"
        patcher = patch.object(scalp_bot_e3, "STATE", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_state_restores_basic_fields(self):
        original = _state(
            held_buy_rungs=[1, 3],
            realized_grid_profit_est=4.25,
            reset_count=2,
        )
        scalp_bot_e3.save_state(original)
        restored = scalp_bot_e3.load_state()

        self.assertIsInstance(restored, scalp_bot_e3.CycleState)
        self.assertEqual(restored.symbol, "BTCUSDT")
        self.assertEqual(restored.center_price, 100.0)
        self.assertEqual(restored.levels, original.levels)
        self.assertEqual(restored.qty_per_rung, 2.0)
        self.assertEqual(restored.held_buy_rungs, [1, 3])
        self.assertAlmostEqual(restored.realized_grid_profit_est, 4.25)
        self.assertEqual(restored.reset_count, 2)
        # 기준선이 유지돼야 손실 컷이 원래 기준으로 계속 판정된다
        self.assertEqual(restored.wallet_balance_start, 28.0)

    def test_load_state_keeps_open_orders(self):
        original = _state(
            buy_orders={"0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 2.0}},
            sell_orders={"2": {"order_id": 22, "rung": 2, "price": 100.0, "quantity": 2.0}},
            held_buy_rungs=[1],
        )
        scalp_bot_e3.save_state(original)
        restored = scalp_bot_e3.load_state()

        self.assertEqual(restored.buy_orders, original.buy_orders)
        self.assertEqual(restored.sell_orders, original.sell_orders)
        # 복구본이 그대로 다음 사이클 입력이 될 수 있어야 한다
        grid = GridState(restored.levels, set(restored.held_buy_rungs))
        self.assertTrue(grid.in_range(restored.center_price))

    def test_load_state_returns_none_when_missing_or_corrupt(self):
        self.assertIsNone(scalp_bot_e3.load_state())
        self.state_path.write_text("깨진 내용", encoding="utf-8")
        self.assertIsNone(scalp_bot_e3.load_state())

    def test_resume_does_not_reconcile_orders_with_exchange(self):
        """현재 한계 기록: load_state 는 저장된 주문을 그대로 믿는다.

        봇이 죽어 있는 동안 주문이 체결·취소됐어도 상태 파일은 그대로다.
        재개하면 이미 없는 주문을 살아 있다고 보고 그 칸을 건너뛴다.
        반대로 거래소에 남아 있는데 상태 파일에 없으면 같은 칸에 또 낸다.
        즉 --resume 에는 거래소 대조가 없다. 이 테스트는 그 한계를 고정해 둔다.
        대조를 넣으면 이 테스트가 깨지고, 그때 갱신하면 된다.
        """
        original = _state(
            buy_orders={"0": {"order_id": 999, "rung": 0, "price": 90.0, "quantity": 2.0}}
        )
        scalp_bot_e3.save_state(original)
        ex = StatusExchange({})          # 거래소에는 그 주문이 없다
        restored = scalp_bot_e3.load_state()
        self.assertIn("0", restored.buy_orders)
        self.assertEqual(ex.asked, [], "load_state 는 거래소를 조회하지 않는다")

    def test_reconcile_resume_adopts_exchange_orders(self):
        ex = StatusExchange({}, position={"side": "LONG", "amount": 2.0})
        ex.client = type("C", (), {
            "futures_get_open_orders": lambda _self, symbol=None: [
                {"orderId": 11, "price": "90.0", "origQty": "2.0", "side": "BUY", "reduceOnly": False},
                {"orderId": 22, "price": "100.0", "origQty": "2.0", "side": "SELL", "reduceOnly": True},
            ]
        })()
        state = _state(
            buy_orders={"9": {"order_id": 999, "rung": 9, "price": 0, "quantity": 2.0}},
            sell_orders={},
            held_buy_rungs=[],
        )
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state, cancel_duplicates=False)
        self.assertEqual(sorted(out.buy_orders), ["0"])
        self.assertEqual(sorted(out.sell_orders), ["2"])
        self.assertEqual(out.held_buy_rungs, [1])

    def test_reconcile_resume_cancels_duplicate_orders(self):
        ex = StatusExchange({}, position=None)
        ex.client = type("C", (), {
            "futures_get_open_orders": lambda _self, symbol=None: [
                {"orderId": 11, "price": "90.0", "origQty": "2.0", "side": "BUY", "reduceOnly": False},
                {"orderId": 12, "price": "90.0", "origQty": "2.0", "side": "BUY", "reduceOnly": False},
            ]
        })()
        state = _state()
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state, cancel_duplicates=True)
        self.assertEqual(sorted(out.buy_orders), ["0"])
        self.assertEqual(ex.cancelled, [("BTCUSDT", 12)])

    def test_reconcile_resume_cancels_stale_orders_outside_current_grid(self):
        ex = StatusExchange({}, position=None)
        ex.client = type("C", (), {
            "futures_get_open_orders": lambda _self, symbol=None: [
                {"orderId": 11, "price": "90.0", "origQty": "2.0", "side": "BUY", "reduceOnly": False},
                {"orderId": 99, "price": "89.0", "origQty": "2.0", "side": "BUY", "reduceOnly": False},
            ]
        })()
        state = _state()
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(
            ex, state, cancel_duplicates=False, cancel_stale=True
        )
        self.assertEqual(sorted(out.buy_orders), ["0"])
        self.assertEqual(ex.cancelled, [("BTCUSDT", 99)])


# ------------------------------------------------- 2) GLOBAL_STOP 손실 컷
class E3GlobalStopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "state.json"
        patcher = patch.object(scalp_bot_e3, "STATE", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _orders_state(self):
        return _state(
            buy_orders={"0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 2.0}},
            sell_orders={"2": {"order_id": 22, "rung": 2, "price": 100.0, "quantity": 2.0}},
            held_buy_rungs=[1],
        )

    @patch("scripts.scalp_bot_e3.log_event")
    def test_no_stop_above_threshold(self, _log_event):
        ex = StatusExchange({}, position={"side": "LONG", "amount": 3.0})
        state = self._orders_state()
        # 기준 28.0 에 손실 15% -> 컷 선은 23.8. 24.0 은 아직 위다.
        self.assertFalse(
            scalp_bot_e3.handle_global_stop(ex, state, "BTCUSDT", 24.0, 15.0, live=True)
        )
        self.assertEqual(ex.cancelled, [])
        self.assertEqual(ex.closed, [])
        self.assertFalse(self.state_path.exists())

    @patch("scripts.scalp_bot_e3.log_event")
    def test_stop_cancels_orders_but_does_not_flatten_when_not_live(self, _log_event):
        ex = StatusExchange({}, position={"side": "LONG", "amount": 3.0})
        state = self._orders_state()
        self.assertTrue(
            scalp_bot_e3.handle_global_stop(ex, state, "BTCUSDT", 20.0, 15.0, live=False)
        )
        self.assertEqual(ex.cancelled, [("BTCUSDT", 11), ("BTCUSDT", 22)])
        self.assertEqual(ex.closed, [], "드라이런에서 시장가 청산이 나가면 안 된다")

    @patch("scripts.scalp_bot_e3.log_event")
    def test_stop_flattens_position_when_live(self, _log_event):
        ex = StatusExchange({}, position={"side": "LONG", "amount": 3.0})
        state = self._orders_state()
        self.assertTrue(
            scalp_bot_e3.handle_global_stop(ex, state, "BTCUSDT", 20.0, 15.0, live=True)
        )
        self.assertEqual(ex.cancelled, [("BTCUSDT", 11), ("BTCUSDT", 22)])
        self.assertEqual(ex.closed, [("BTCUSDT", "LONG", 3.0)])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_stop_saves_state_before_exit(self, _log_event):
        """종료 직전 저장이 빠지면 재개 시 마지막 사이클이 통째로 사라진다."""
        ex = StatusExchange({})
        state = self._orders_state()
        scalp_bot_e3.handle_global_stop(ex, state, "BTCUSDT", 20.0, 15.0, live=False)
        self.assertTrue(self.state_path.exists())
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["symbol"], "BTCUSDT")
        self.assertEqual(saved["held_buy_rungs"], [1])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_stop_survives_cancel_failure(self, _log_event):
        """취소가 실패해도 청산과 저장까지는 반드시 도달해야 한다."""

        class BrokenCancel(StatusExchange):
            def cancel_regular_order(self, symbol, order_id):
                raise RuntimeError("취소 거부")

        ex = BrokenCancel({}, position={"side": "LONG", "amount": 1.0})
        state = self._orders_state()
        self.assertTrue(
            scalp_bot_e3.handle_global_stop(ex, state, "BTCUSDT", 20.0, 15.0, live=True)
        )
        self.assertEqual(ex.closed, [("BTCUSDT", "LONG", 1.0)])
        self.assertTrue(self.state_path.exists())


# --------------------------------- 3) process_live_fills 부분체결/미응답
class E3LiveFillTests(unittest.TestCase):
    @patch("scripts.scalp_bot_e3.log_event")
    def test_missing_status_keeps_order_and_inventory(self, _log_event):
        """조회 실패를 체결로 오인하면 없는 재고를 만든다."""
        ex = StatusExchange({})          # 조회 시 예외 -> regular_order_status 가 None
        state = _state(
            buy_orders={"1": {"order_id": 11, "rung": 1, "price": 95.0, "quantity": 2.0}}
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertIn("1", state.buy_orders)
        self.assertEqual(state.held_buy_rungs, [])
        self.assertEqual(ex.asked, [("BTCUSDT", 11)])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_none_payload_keeps_order(self, _log_event):
        """응답은 왔는데 내용이 없는 경우도 체결이 아니다."""
        ex = StatusExchange({11: None})
        state = _state(
            buy_orders={"1": {"order_id": 11, "rung": 1, "price": 95.0, "quantity": 2.0}}
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertIn("1", state.buy_orders)
        self.assertEqual(state.held_buy_rungs, [])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_partially_filled_keeps_order_and_inventory(self, _log_event):
        """부분체결은 한 칸이 아직 다 차지 않은 상태다. 재고로 잡으면 안 된다."""
        ex = StatusExchange({11: {"status": "PARTIALLY_FILLED", "executedQty": "1.0"}})
        state = _state(
            buy_orders={"1": {"order_id": 11, "rung": 1, "price": 95.0, "quantity": 2.0}}
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertIn("1", state.buy_orders)
        self.assertEqual(state.held_buy_rungs, [])
        self.assertAlmostEqual(state.realized_grid_profit_est, 0.0)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_filled_buy_moves_rung_into_inventory(self, _log_event):
        ex = StatusExchange({11: {"status": "FILLED"}})
        state = _state(
            buy_orders={"1": {"order_id": 11, "rung": 1, "price": 95.0, "quantity": 2.0}}
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertFalse(state.buy_orders)
        self.assertEqual(state.held_buy_rungs, [1])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_filled_sell_releases_rung_and_books_profit(self, _log_event):
        ex = StatusExchange({22: {"status": "FILLED"}})
        state = _state(
            held_buy_rungs=[1],
            sell_orders={"2": {"order_id": 22, "rung": 2, "price": 100.0, "quantity": 2.0}},
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertFalse(state.sell_orders)
        self.assertEqual(state.held_buy_rungs, [])
        self.assertAlmostEqual(state.realized_grid_profit_est, 10.0)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_mixed_statuses_do_not_raise(self, _log_event):
        """한 주문의 상태가 이상해도 나머지 주문 처리가 멈추면 안 된다."""
        ex = StatusExchange({
            11: None,
            12: {"status": "PARTIALLY_FILLED"},
            13: {"status": "FILLED"},
        })
        state = _state(
            buy_orders={
                "0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 2.0},
                "1": {"order_id": 12, "rung": 1, "price": 95.0, "quantity": 2.0},
                "3": {"order_id": 13, "rung": 3, "price": 105.0, "quantity": 2.0},
            }
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        scalp_bot_e3.process_live_fills(ex, state, grid)
        self.assertEqual(sorted(state.buy_orders), ["0", "1"])
        self.assertEqual(state.held_buy_rungs, [3])
        self.assertEqual(len(ex.asked), 3, "모든 주문을 조회해야 한다")

# ------------------------------------------------- 격자 사다리 뷰
class E3GridViewTests(unittest.TestCase):
    def _view_state(self, **over):
        base = dict(
            levels=[88.0, 91.0, 94.0, 97.0, 100.0, 103.0, 106.0],
            held_buy_rungs=[2, 3],
            buy_orders={
                "1": {"order_id": 1, "rung": 1, "price": 91.0, "quantity": 0.05},
            },
            sell_orders={
                "4": {"order_id": 3, "rung": 4, "price": 100.0, "quantity": 0.05},
            },
            realized_grid_profit_est=0.1234,
        )
        base.update(over)
        return _state(**base)

    def test_view_marks_each_rung_state(self):
        text = scalp_bot_e3.render_grid_view(self._view_state(), 98.6, 28.31)
        lines = {int(l.split()[0]): l for l in text.splitlines()
                 if l.strip() and l.split()[0].isdigit()}
        self.assertIn("매도대기", lines[4])
        self.assertIn("보유", lines[3])
        self.assertIn("보유", lines[2])
        self.assertIn("매수대기", lines[1])
        self.assertIn("·", lines[6])

    def test_view_is_ordered_high_to_low(self):
        """호가창처럼 위가 고가여야 읽기 쉽다."""
        text = scalp_bot_e3.render_grid_view(self._view_state(), 98.6, 28.31)
        rungs = [int(l.split()[0]) for l in text.splitlines()
                 if l.strip() and l.split()[0].isdigit()]
        self.assertEqual(rungs, sorted(rungs, reverse=True))

    def test_cursor_sits_on_rung_below_price(self):
        """현재가 98.6 은 rung3(97.0) 과 rung4(100.0) 사이다."""
        text = scalp_bot_e3.render_grid_view(self._view_state(), 98.6, 28.31)
        cursor_line = [l for l in text.splitlines() if "◀ 현재가" in l]
        self.assertEqual(len(cursor_line), 1)
        self.assertTrue(cursor_line[0].strip().startswith("3"))

    def test_view_flags_out_of_range(self):
        text = scalp_bot_e3.render_grid_view(self._view_state(), 200.0, 28.0)
        self.assertIn("범위 이탈", text)
        inside = scalp_bot_e3.render_grid_view(self._view_state(), 98.6, 28.0)
        self.assertNotIn("범위 이탈", inside)

    def test_view_windows_around_price_when_many_levels(self):
        """격자 31개를 전부 찍으면 읽을 수 없다. 현재가 주변만 보여준다."""
        levels = [100.0 + i * 0.5 for i in range(31)]
        state = self._view_state(levels=levels, held_buy_rungs=[14],
                                 buy_orders={}, sell_orders={})
        text = scalp_bot_e3.render_grid_view(state, 107.2, 28.1, max_rows=21)
        rows = [l for l in text.splitlines()
                if l.strip() and l.split()[0].isdigit()]
        self.assertEqual(len(rows), 21)
        self.assertIn("생략", text)
        # 현재가 칸이 창 안에 있어야 의미가 있다
        self.assertIn("◀ 현재가", text)

    def test_view_reports_totals(self):
        state = self._view_state()
        text = scalp_bot_e3.render_grid_view(state, 98.6, 28.31)
        self.assertIn("보유 2/7칸", text)
        self.assertIn("대기 매수1 매도1", text)
        self.assertIn("+0.123400", text)
        self.assertIn("+0.3100", text)          # 28.31 - 28.00

    def test_view_warns_realized_excludes_inventory(self):
        """격자 실현만 보면 하락장에서도 계속 플러스로 보인다.
        그리드의 대표적 착시라 뷰에 경고가 반드시 남아 있어야 한다."""
        text = scalp_bot_e3.render_grid_view(self._view_state(), 98.6, 28.31)
        self.assertIn("맞물린 짝만", text)

    def test_view_handles_empty_levels(self):
        state = _state(levels=[], held_buy_rungs=[], buy_orders={}, sell_orders={})
        self.assertIn("표시할 격자가 없습니다",
                      scalp_bot_e3.render_grid_view(state, 100.0, 28.0))

    def test_view_shows_paused_flag(self):
        state = self._view_state()
        self.assertIn("일시정지",
                      scalp_bot_e3.render_grid_view(state, 98.6, 28.0, paused=True))
        self.assertIn("가동중",
                      scalp_bot_e3.render_grid_view(state, 98.6, 28.0, paused=False))

    def test_gridview_button_is_wired(self):
        self.assertIn("gridview", scalp_bot_e3.Tg.BUTTONS.values())

class OrderClient:
    """futures_create_order 를 기록하는 가짜 클라이언트.

    fail_on: 이 가격에서 주문이 거부된다(거래소 거절 재현).
    """

    def __init__(self, fail_on=None):
        self.orders = []
        self.fail_on = fail_on
        self._next = 1000

    def futures_create_order(self, **kw):
        if self.fail_on is not None and float(kw["price"]) == self.fail_on:
            raise RuntimeError("APIError(code=-2022): ReduceOnly Order is rejected")
        self._next += 1
        self.orders.append(kw)
        return {"orderId": self._next}


class OrderExchange(FakeExchange):
    def __init__(self, fail_on=None, **kwargs):
        super().__init__(**kwargs)
        self.client = OrderClient(fail_on=fail_on)

    def round_price(self, symbol, price):
        return round(float(price), 6)


class RankingExchange(FakeExchange):
    def __init__(self, frames=None, books=None, symbols=None, tickers24=None, qty=1.25, wallet=30.0):
        super().__init__(qty=qty, wallet=wallet)
        self.frames = frames or {}
        self.books = books or {}
        self.symbols = symbols or []
        self.tickers24 = tickers24 or {}

    def get_klines(self, symbol, limit=240, interval="1m"):
        return self.frames[symbol]

    def get_book_ticker(self, symbol):
        return self.books[symbol]

    def get_24h_ticker(self, symbol):
        return self.tickers24.get(symbol, {"price_change_pct": 0.0, "quote_volume": 100000000.0})

    def get_active_usdt_perpetual_symbols(self, limit=None):
        if limit is None:
            return list(self.symbols)
        return list(self.symbols)[:limit]


# ------------------------------------------- ensure_grid_orders (주문 배치)
class E3EnsureGridOrdersTests(unittest.TestCase):
    LEVELS = [88.0, 91.0, 94.0, 97.0, 100.0, 103.0, 106.0]

    def _fixture(self, held=(), buy_orders=None, sell_orders=None):
        state = _state(
            levels=list(self.LEVELS),
            held_buy_rungs=list(held),
            buy_orders=dict(buy_orders or {}),
            sell_orders=dict(sell_orders or {}),
        )
        grid = GridState(state.levels, set(state.held_buy_rungs))
        return state, grid

    @patch("scripts.scalp_bot_e3.log_event")
    def test_dry_run_places_no_real_orders(self, _log_event):
        ex = OrderExchange()
        state, grid = self._fixture()
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=False)
        self.assertEqual(ex.client.orders, [], "드라이런에서 실주문이 나갔다")
        self.assertTrue(state.buy_orders)
        # 가짜 id 는 음수라 실제 주문 id 와 섞이지 않아야 한다
        for payload in state.buy_orders.values():
            self.assertLess(payload["order_id"], 0)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_buy_is_not_reduce_only_and_sell_is(self, _log_event):
        """방향이 뒤바뀌면 재고 없이 숏이 열리거나 매도가 전부 거부된다."""
        ex = OrderExchange()
        state, grid = self._fixture(held=[2])
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)
        buys = [o for o in ex.client.orders if o["side"] == "BUY"]
        sells = [o for o in ex.client.orders if o["side"] == "SELL"]
        self.assertTrue(buys)
        self.assertTrue(sells)
        for o in buys:
            self.assertFalse(o["reduceOnly"])
        for o in sells:
            self.assertTrue(o["reduceOnly"])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_sell_only_on_rung_above_inventory(self, _log_event):
        """보유가 없는 칸에 매도를 걸면 거래소가 -2022 로 거부한다."""
        ex = OrderExchange()
        state, grid = self._fixture(held=[2])
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)
        self.assertEqual(sorted(state.sell_orders), ["3"])
        self.assertAlmostEqual(state.sell_orders["3"]["price"], self.LEVELS[3])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_no_sell_when_no_inventory(self, _log_event):
        ex = OrderExchange()
        state, grid = self._fixture(held=[])
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)
        self.assertFalse(state.sell_orders)
        self.assertFalse([o for o in ex.client.orders if o["side"] == "SELL"])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_does_not_duplicate_existing_orders(self, _log_event):
        """두 번 호출해도 같은 칸에 주문이 또 나가면 안 된다.
        e2 에서 손절주문이 REUSDT 3건·XLMUSDT 2건 중복으로 쌓인 전례가 있다."""
        ex = OrderExchange()
        state, grid = self._fixture(held=[2])
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)
        first = len(ex.client.orders)
        snapshot_buy = dict(state.buy_orders)
        snapshot_sell = dict(state.sell_orders)

        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)
        self.assertEqual(len(ex.client.orders), first, "같은 칸에 중복 주문이 나갔다")
        self.assertEqual(state.buy_orders, snapshot_buy)
        self.assertEqual(state.sell_orders, snapshot_sell)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_held_rung_gets_no_new_buy(self, _log_event):
        """이미 보유한 칸에 또 사면 한 칸에 재고가 두 번 쌓인다."""
        ex = OrderExchange()
        state, grid = self._fixture(held=[1, 2])
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)
        self.assertNotIn("1", state.buy_orders)
        self.assertNotIn("2", state.buy_orders)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_only_rungs_below_price_get_buys(self, _log_event):
        ex = OrderExchange()
        state, grid = self._fixture()
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 95.0, live=True)
        # 95.0 미만인 칸: 88/91/94 -> rung 0,1,2
        self.assertEqual(sorted(state.buy_orders, key=int), ["0", "1", "2"])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_recorded_order_id_matches_exchange_response(self, _log_event):
        ex = OrderExchange()
        state, grid = self._fixture()
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 92.0, live=True)
        returned = [o for o in ex.client.orders]
        self.assertEqual(len(returned), len(state.buy_orders))
        for payload in state.buy_orders.values():
            self.assertGreater(payload["order_id"], 0)

    @patch("scripts.scalp_bot_e3.log_event")
    def test_order_failure_does_not_record_ghost_order(self, _log_event):
        """한 칸 거부가 나도 나머지 칸 주문과 상태 기록은 계속 진행돼야 한다."""
        ex = OrderExchange(fail_on=94.0)      # rung2 에서 거부
        state, grid = self._fixture()
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 98.6, live=True)

        # 실패한 칸은 기록되면 안 된다
        self.assertNotIn("2", state.buy_orders)
        # 실패 전/후 성공분은 거래소에 실제로 나갔으므로 state 에도 있어야 한다
        placed_prices = {float(o["price"]) for o in ex.client.orders}
        recorded_prices = {p["price"] for p in state.buy_orders.values()}
        self.assertEqual(placed_prices, recorded_prices,
                         "거래소에 나간 주문과 state 기록이 어긋난다")
        self.assertEqual(sorted(state.buy_orders, key=int), ["0", "1", "3"])

    def test_place_limit_order_rounds_price_and_maps_side(self):
        ex = OrderExchange()
        order_id = scalp_bot_e3.place_limit_order(
            ex, "BTCUSDT", "LONG", quantity=0.37, price=100.1234567, reduce_only=False
        )
        self.assertGreater(order_id, 0)
        self.assertEqual(len(ex.client.orders), 1)
        placed = ex.client.orders[0]
        self.assertEqual(placed["side"], "BUY")
        self.assertEqual(float(placed["price"]), 100.123457)
        self.assertFalse(placed["reduceOnly"])

    def test_place_limit_order_sell_sets_reduce_only(self):
        ex = OrderExchange()
        scalp_bot_e3.place_limit_order(
            ex, "BTCUSDT", "SHORT", quantity=0.5, price=99.8765432, reduce_only=True
        )
        placed = ex.client.orders[0]
        self.assertEqual(placed["side"], "SELL")
        self.assertTrue(placed["reduceOnly"])

    @patch("scripts.scalp_bot_e3.log_event")
    def test_try_place_limit_order_returns_none_on_failure(self, log_event):
        ex = OrderExchange(fail_on=94.0)
        order_id = scalp_bot_e3.try_place_limit_order(
            ex, "BTCUSDT", "LONG", quantity=1.0, price=94.0, reduce_only=False
        )
        self.assertIsNone(order_id)
        log_event.assert_called_once()

    @patch("scripts.scalp_bot_e3.log_event")
    def test_quantity_and_price_come_from_state(self, _log_event):
        ex = OrderExchange()
        state, grid = self._fixture()
        state.qty_per_rung = 0.037
        scalp_bot_e3.ensure_grid_orders(ex, state, grid, 92.0, live=True)
        for o in ex.client.orders:
            self.assertAlmostEqual(float(o["quantity"]), 0.037)
        for payload in state.buy_orders.values():
            self.assertAlmostEqual(payload["quantity"], 0.037)
            self.assertIn(payload["price"], self.LEVELS)

    def test_quantity_for_rung_passes_budget_as_max_notional(self):
        class QtyExchange(FakeExchange):
            def __init__(self):
                super().__init__(qty=0.42)
                self.calls = []

            def round_quantity(self, symbol, qty, price=None, max_notional=None):
                self.calls.append({
                    "symbol": symbol,
                    "qty": qty,
                    "price": price,
                    "max_notional": max_notional,
                })
                return 0.42

        ex = QtyExchange()
        out = scalp_bot_e3.quantity_for_rung(
            ex, "BTCUSDT", budget_usdt=28.0, leverage=3, grid_count=6, mark_price=100.0
        )
        self.assertEqual(out, 0.42)
        self.assertEqual(len(ex.calls), 1)
        self.assertAlmostEqual(ex.calls[0]["qty"], 0.14)
        self.assertAlmostEqual(ex.calls[0]["max_notional"], 14.0)
        self.assertAlmostEqual(ex.calls[0]["price"], 100.0)


class E3RankingTests(unittest.TestCase):
    def _frame(self, closes, wick_pct=0.01):
        rows = []
        for c in closes:
            rows.append(
                {
                    "open": c,
                    "high": c * (1.0 + wick_pct),
                    "low": c * (1.0 - wick_pct),
                    "close": c,
                    "volume": 1000.0,
                    "taker_buy_base": 500.0,
                }
            )
        return pd.DataFrame(rows)

    def test_score_candidate_symbol_filters_too_still_symbol(self):
        closes = [1.0 + (0.0002 if i % 2 else 0.0) for i in range(240)]
        ex = RankingExchange(
            frames={"SLOWUSDT": self._frame(closes)},
            books={"SLOWUSDT": {"bid": 0.9999, "ask": 1.0001}},
            tickers24={"SLOWUSDT": {"price_change_pct": 0.5, "quote_volume": 100000000.0}},
            symbols=["SLOWUSDT"],
        )
        row = scalp_bot_e3.score_candidate_symbol(
            ex, "SLOWUSDT", wallet_balance=28.0, leverage=3, grid_count=5, capital_usage=1.0
        )
        self.assertIsNone(row)

    def test_rank_candidate_symbols_keeps_active_liquid_names(self):
        fast = [1.0 + (0.004 * ((i % 6) - 3)) for i in range(240)]
        slow = [1.0 + (0.0002 if i % 2 else 0.0) for i in range(240)]
        wide_spread = [1.0 + (0.003 * ((i % 4) - 2)) for i in range(240)]
        ex = RankingExchange(
            frames={
                "DOGEUSDT": self._frame(fast, wick_pct=0.03),
                "XRPUSDT": self._frame(fast, wick_pct=0.03),
                "STILLUSDT": self._frame(slow),
                "WIDEUSDT": self._frame(wide_spread, wick_pct=0.03),
            },
            books={
                "DOGEUSDT": {"bid": 0.9995, "ask": 1.0005},
                "XRPUSDT": {"bid": 0.9995, "ask": 1.0005},
                "STILLUSDT": {"bid": 0.9999, "ask": 1.0001},
                "WIDEUSDT": {"bid": 0.99, "ask": 1.01},
            },
            tickers24={
                "DOGEUSDT": {"price_change_pct": 4.0, "quote_volume": 200000000.0},
                "XRPUSDT": {"price_change_pct": 3.0, "quote_volume": 180000000.0},
                "STILLUSDT": {"price_change_pct": 0.2, "quote_volume": 150000000.0},
                "WIDEUSDT": {"price_change_pct": 1.0, "quote_volume": 170000000.0},
            },
            symbols=["DOGEUSDT", "XRPUSDT", "STILLUSDT", "WIDEUSDT"],
        )
        ranked = scalp_bot_e3.rank_candidate_symbols(
            ex,
            wallet_balance=28.0,
            leverage=3,
            grid_count=5,
            capital_usage=1.0,
            candidate_limit=4,
            top_n=4,
            exclude_symbols={"XRPUSDT"},
        )
        self.assertEqual([row["symbol"] for row in ranked], ["DOGEUSDT"])

# ============================= 사용자 수동 개입 대응
class ManualClient:
    """거래소에 실제로 남아 있는 미체결만 돌려준다."""

    def __init__(self, open_orders, position=None):
        self._oo = open_orders
        self._pos = position

    def futures_get_open_orders(self, symbol=None):
        return [o for o in self._oo if not symbol or o["symbol"] == symbol]


class ManualExchange(FakeExchange):
    def __init__(self, open_orders, position=None, **kw):
        super().__init__(**kw)
        self.client = ManualClient(open_orders, position)
        self._pos = position

    def round_price(self, symbol, price):
        return round(float(price), 6)

    def get_position(self, symbol):
        return self._pos


def _order(oid, side, price, qty, reduce_only=False, sym="BTCUSDT"):
    return {"orderId": oid, "symbol": sym, "side": side, "price": str(price),
            "origQty": str(qty), "reduceOnly": reduce_only}


LEVELS = [90.0, 95.0, 100.0, 105.0, 110.0]


def _manual_state(**over):
    base = dict(
        symbol="BTCUSDT", center_price=100.0, started_at=1.0,
        wallet_balance_start=28.0, levels=list(LEVELS), qty_per_rung=2.0,
    )
    base.update(over)
    return scalp_bot_e3.CycleState(**base)


class E3ManualInterventionTests(unittest.TestCase):
    """사용자가 수동으로 주문을 취소하거나 포지션을 정리하는 상황.

    이 대조가 없으면 봇은 이미 없는 주문을 '살아 있다'고 믿고
    그 칸을 영영 다시 채우지 않는다(격자에 구멍이 남는다).
    """

    def test_user_cancelled_buy_is_dropped_from_state(self):
        state = _manual_state(buy_orders={
            "0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 2.0},
            "1": {"order_id": 12, "rung": 1, "price": 95.0, "quantity": 2.0},
        })
        # 사용자가 rung0 주문을 취소해 거래소에는 rung1 만 남았다
        ex = ManualExchange([_order(12, "BUY", 95.0, 2.0)])
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertEqual(sorted(out.buy_orders), ["1"])
        self.assertNotIn("0", out.buy_orders)

    def test_refill_is_possible_after_user_cancel(self):
        """대조 후에는 ensure_grid_orders 가 그 칸을 다시 채울 수 있어야 한다."""
        state = _manual_state(buy_orders={
            "0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 2.0},
        })
        ex = ManualExchange([])          # 사용자가 전부 취소
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertFalse(out.buy_orders, "취소된 주문이 상태에 남아 재주문을 막는다")

    def test_user_cancelled_sell_clears_inventory_mapping(self):
        """매도가 사라지면 보유 칸도 함께 정리돼야 한다.

        안 그러면 없는 재고에 매도를 걸어 -2022 ReduceOnly 거부가 난다.
        """
        state = _manual_state(
            held_buy_rungs=[1],
            sell_orders={"2": {"order_id": 22, "rung": 2, "price": 100.0,
                               "quantity": 2.0}},
        )
        ex = ManualExchange([])          # 매도 취소 + 포지션도 정리됨
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertFalse(out.sell_orders)
        self.assertEqual(out.held_buy_rungs, [])

    def test_position_still_open_keeps_inventory(self):
        """매도만 취소하고 포지션은 남겨둔 경우 재고를 잃어버리면 안 된다."""
        state = _manual_state(
            held_buy_rungs=[1],
            sell_orders={"2": {"order_id": 22, "rung": 2, "price": 100.0,
                               "quantity": 2.0}},
        )
        ex = ManualExchange([], position={"side": "LONG", "amount": 2.0})
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertEqual(out.held_buy_rungs, [1], "포지션이 남았는데 재고를 지웠다")

    def test_inventory_is_derived_from_sell_orders(self):
        """매도 주문이 rung2 에 있으면 보유는 rung1 이다."""
        state = _manual_state(held_buy_rungs=[])
        ex = ManualExchange([_order(22, "SELL", 100.0, 2.0, reduce_only=True)])
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertEqual(out.held_buy_rungs, [1])

    def test_user_manual_order_is_not_cancelled(self):
        """격자에 없는 가격의 주문은 사용자의 수동 주문일 수 있다.

        기본값(cancel_stale=False)에서는 건드리지 않아야 한다.
        """
        state = _manual_state()
        manual = _order(99, "BUY", 77.7, 2.0)      # 격자에 없는 가격
        ex = ManualExchange([manual])
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertEqual(ex.cancelled, [], "사용자 수동 주문을 취소했다")
        self.assertFalse(out.buy_orders, "격자 밖 주문을 격자로 잘못 인식했다")

    def test_quantity_mismatch_is_ignored(self):
        """수량이 다른 주문은 이 격자의 것이 아니다(사용자 수동 주문 등)."""
        state = _manual_state()
        ex = ManualExchange([_order(98, "BUY", 90.0, 50.0)])   # 수량 2.0 이 아님
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(ex, state)
        self.assertFalse(out.buy_orders)
        self.assertEqual(ex.cancelled, [])

    def test_reconcile_survives_exchange_failure(self):
        """조회가 실패해도 봇이 죽으면 안 된다."""
        class Broken(ManualExchange):
            def __init__(self):
                super().__init__([])
                self.client = type("C", (), {
                    "futures_get_open_orders": lambda *a, **k: (_ for _ in ()).throw(
                        RuntimeError("API"))})()

        state = _manual_state(buy_orders={
            "0": {"order_id": 11, "rung": 0, "price": 90.0, "quantity": 2.0}})
        out = scalp_bot_e3.reconcile_cycle_state_with_exchange(Broken(), state)
        # 조회 실패를 '주문 없음' 으로 오인하면 살아 있는 주문을 상태에서 지우고
        # 다음 사이클에 중복 주문을 낸다(증거금 이중 점유 -> -2019).
        # 실패 시에는 상태를 그대로 둬야 한다.
        self.assertEqual(sorted(out.buy_orders), ["0"],
                         "조회 실패인데 주문을 지웠다")

# ============================== 주문 실패 백오프 (실사고 회귀)
class OrderBackoffTests(unittest.TestCase):
    """[2026-08-21 실사고] -2019(증거금 부족)로 같은 주문을 1시간 동안
    8초마다 재시도해 PLACE_ORDER_FAILED 가 461건 쌓였다.
    그동안 격자에 구멍이 뚫린 채 돌았고, 매도가 걸릴 자리가 없어
    완결률이 7.7%(매수 39 / 매도 3)로 떨어졌다.
    """

    def setUp(self):
        scalp_bot_e3._ORDER_BACKOFF.clear()
        self.addCleanup(scalp_bot_e3._ORDER_BACKOFF.clear)

    def test_no_backoff_by_default(self):
        self.assertFalse(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0))

    @patch("scripts.scalp_bot_e3.log_event")
    def test_margin_error_triggers_backoff(self, _log):
        class Fail:
            def round_price(self, s, p):
                return p

            class client:
                @staticmethod
                def futures_create_order(**kw):
                    raise RuntimeError("APIError(code=-2019): Margin is insufficient.")

        oid = scalp_bot_e3.try_place_limit_order(
            Fail(), "BTCUSDT", "LONG", 1.0, 100.0, False)
        self.assertIsNone(oid)
        self.assertTrue(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0),
            "증거금 부족인데 백오프가 안 걸렸다")

    @patch("scripts.scalp_bot_e3.log_event")
    def test_backed_off_order_is_not_retried(self, _log):
        """백오프 중에는 거래소를 아예 호출하지 않아야 한다."""
        calls = []

        class Counting:
            def round_price(self, s, p):
                return p

            class client:
                @staticmethod
                def futures_create_order(**kw):
                    calls.append(kw)
                    raise RuntimeError("APIError(code=-2019): Margin is insufficient.")

        ex = Counting()
        scalp_bot_e3.try_place_limit_order(ex, "BTCUSDT", "LONG", 1.0, 100.0, False)
        first = len(calls)
        for _ in range(50):
            scalp_bot_e3.try_place_limit_order(ex, "BTCUSDT", "LONG", 1.0, 100.0, False)
        self.assertEqual(len(calls), first,
                         "백오프 중인데 재시도했다 (461건 폭주의 원인)")

    @patch("scripts.scalp_bot_e3.log_event")
    def test_other_errors_do_not_backoff(self, _log):
        """일시적 오류까지 5분 쉬면 격자가 불필요하게 비어 있게 된다."""
        class Fail:
            def round_price(self, s, p):
                return p

            class client:
                @staticmethod
                def futures_create_order(**kw):
                    raise RuntimeError("APIError(code=-1021): Timestamp expired.")

        scalp_bot_e3.try_place_limit_order(Fail(), "BTCUSDT", "LONG", 1.0, 100.0, False)
        self.assertFalse(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0))

    @patch("scripts.scalp_bot_e3.log_event")
    def test_success_clears_backoff(self, _log):
        class OK:
            def round_price(self, s, p):
                return p

            class client:
                @staticmethod
                def futures_create_order(**kw):
                    return {"orderId": 777}

        scalp_bot_e3._ORDER_BACKOFF[
            scalp_bot_e3._backoff_key("BTCUSDT", "LONG", 100.0)] = 0.0
        oid = scalp_bot_e3.try_place_limit_order(
            OK(), "BTCUSDT", "LONG", 1.0, 100.0, False)
        self.assertEqual(oid, 777)
        self.assertFalse(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0))

    def test_backoff_is_per_rung(self):
        """한 칸이 막혔다고 다른 칸까지 멈추면 안 된다."""
        scalp_bot_e3._ORDER_BACKOFF[
            scalp_bot_e3._backoff_key("BTCUSDT", "LONG", 100.0)] = 1e18
        self.assertTrue(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0))
        self.assertFalse(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 95.0))
        self.assertFalse(
            scalp_bot_e3.order_is_backed_off("ETHUSDT", "LONG", 100.0))

    def test_backoff_expires(self):
        key = scalp_bot_e3._backoff_key("BTCUSDT", "LONG", 100.0)
        scalp_bot_e3._ORDER_BACKOFF[key] = 1000.0
        self.assertTrue(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0, now=999.0))
        self.assertFalse(
            scalp_bot_e3.order_is_backed_off("BTCUSDT", "LONG", 100.0, now=1001.0))

if __name__ == "__main__":
    unittest.main()
