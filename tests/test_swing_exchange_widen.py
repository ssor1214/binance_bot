"""[2026-08-10 사용자요청] "스윙은 포지션의 최대일 때뿐" — widen_exchange_trailing_for_swing()
단위테스트. armed 상태에서만 거래소 TRAILING_STOP_MARKET을 넓은 폭으로 재등록해야 하고,
한 번 넓히면 다시 반복 재등록하지 않아야 한다. 실 API를 절대 호출하지 않는다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import widen_exchange_trailing_for_swing
from bot.position_manager import TrackedPosition


def cfg():
    c = Config()
    c.trail_drawdown_pct = 1.0
    c.swing_exchange_trailing_multiplier = 6.0
    return c


def make_pos(armed=True, widened=False, side="LONG", trailing_order_id=111):
    pos = TrackedPosition(symbol="BTCUSDT", side=side, entry_price=100.0, quantity=10.0, leverage=4.0)
    pos.armed = armed
    pos.exchange_trailing_widened = widened
    pos.trailing_order_id = trailing_order_id
    return pos


class WidenExchangeTrailingForSwingTests(unittest.TestCase):
    def test_does_nothing_when_not_armed(self):
        """[핵심] "스윙은 포지션의 최대일 때뿐" — armed(이미 최소 익절선 도달)가 아니면
        거래소 주문에 절대 손대면 안 된다."""
        ex = MagicMock()
        pos = make_pos(armed=False)
        widen_exchange_trailing_for_swing(ex, pos, cfg(), mark_price=105.0)
        ex.place_trailing_stop_market.assert_not_called()
        self.assertFalse(pos.exchange_trailing_widened)

    def test_does_nothing_when_already_widened(self):
        """한 번 넓혔으면 이 포지션이 살아있는 동안 다시 재등록하지 않는다 — 매 5초 주기마다
        불필요하게 거래소 주문을 다시 걸지 않기 위함."""
        ex = MagicMock()
        pos = make_pos(armed=True, widened=True)
        widen_exchange_trailing_for_swing(ex, pos, cfg(), mark_price=105.0)
        ex.place_trailing_stop_market.assert_not_called()

    def test_widens_and_cancels_old_order_when_armed_and_not_yet_widened(self):
        ex = MagicMock()
        ex.place_trailing_stop_market.return_value = {"algoId": 999}
        ex.get_open_algo_order_ids.return_value = {999}
        pos = make_pos(armed=True, widened=False, trailing_order_id=111)

        widen_exchange_trailing_for_swing(ex, pos, cfg(), mark_price=105.0)

        ex.place_trailing_stop_market.assert_called_once()
        call_kwargs = ex.place_trailing_stop_market.call_args
        # callback_rate = trail_drawdown_pct(1.0) * swing_exchange_trailing_multiplier(6.0) / leverage(4.0) = 1.5
        self.assertAlmostEqual(call_kwargs[0][4], 1.5)
        self.assertEqual(pos.trailing_order_id, 999)
        self.assertTrue(pos.exchange_trailing_widened)
        ex.cancel_order.assert_called_once_with("BTCUSDT", 111)

    def test_activation_price_below_mark_for_long_above_for_short(self):
        ex = MagicMock()
        ex.place_trailing_stop_market.return_value = {"algoId": 999}
        ex.get_open_algo_order_ids.return_value = {999}

        pos_long = make_pos(armed=True, side="LONG")
        widen_exchange_trailing_for_swing(ex, pos_long, cfg(), mark_price=100.0)
        activation_long = ex.place_trailing_stop_market.call_args[0][3]
        self.assertLess(activation_long, 100.0)

        ex.reset_mock()
        ex.place_trailing_stop_market.return_value = {"algoId": 998}
        ex.get_open_algo_order_ids.return_value = {998}
        pos_short = make_pos(armed=True, side="SHORT")
        widen_exchange_trailing_for_swing(ex, pos_short, cfg(), mark_price=100.0)
        activation_short = ex.place_trailing_stop_market.call_args[0][3]
        self.assertGreater(activation_short, 100.0)

    def test_does_not_raise_and_leaves_old_order_intact_when_placement_fails(self):
        """[회귀] 재등록 자체가 실패해도 기존(좁은 폭) 주문을 취소하면 안 되고, 예외를
        위로 던지면 안 된다(호출부의 포지션 처리 전체가 죽으면 안 됨)."""
        ex = MagicMock()
        ex.place_trailing_stop_market.side_effect = RuntimeError("API 실패(테스트 모의)")
        pos = make_pos(armed=True, widened=False, trailing_order_id=111)

        try:
            widen_exchange_trailing_for_swing(ex, pos, cfg(), mark_price=105.0)
        except Exception as e:
            self.fail(f"예외를 던지면 안 됨: {e}")

        ex.cancel_order.assert_not_called()
        self.assertEqual(pos.trailing_order_id, 111)  # 그대로 유지
        self.assertFalse(pos.exchange_trailing_widened)

    def test_does_not_raise_when_new_order_missing_from_open_orders(self):
        """응답은 성공했지만 실제 활성 주문 목록에 없는 경우(레이스 등)에도 안전하게
        처리해야 한다 — 기존 주문은 그대로 두고 widened 플래그도 안 켠다."""
        ex = MagicMock()
        ex.place_trailing_stop_market.return_value = {"algoId": 999}
        ex.get_open_algo_order_ids.return_value = set()  # 999가 없음
        pos = make_pos(armed=True, widened=False, trailing_order_id=111)

        widen_exchange_trailing_for_swing(ex, pos, cfg(), mark_price=105.0)

        ex.cancel_order.assert_not_called()
        self.assertEqual(pos.trailing_order_id, 111)
        self.assertFalse(pos.exchange_trailing_widened)


if __name__ == "__main__":
    unittest.main()
