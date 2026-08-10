"""[2026-08-10] place_entry_order() 단위테스트 — 지정가(메이커) 진입으로 슬리피지 리스크를
없애는 기능. 실제 API를 절대 호출하지 않는다(Exchange를 가짜 객체로 완전히 대체)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import place_entry_order


def cfg(**overrides):
    c = Config()
    c.limit_entry_enabled = True
    c.limit_entry_wait_sec = 2.0
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class PlaceEntryOrderMarketFallbackTests(unittest.TestCase):
    def test_uses_market_order_when_limit_entry_disabled(self):
        c = cfg(limit_entry_enabled=False)
        ex = MagicMock()
        result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)
        ex.open_market_position.assert_called_once_with("BTCUSDT", "LONG", 1.0)
        ex.open_limit_position.assert_not_called()
        self.assertTrue(result)


class PlaceEntryOrderLimitTests(unittest.TestCase):
    def test_long_uses_bid_price_and_returns_true_on_immediate_fill(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)

        ex.open_limit_position.assert_called_once_with("BTCUSDT", "LONG", 1.0, 99.5)
        ex.cancel_regular_order.assert_not_called()
        self.assertTrue(result)

    def test_short_uses_ask_price(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        place_entry_order(ex, c, "BTCUSDT", "SHORT", 1.0)

        ex.open_limit_position.assert_called_once_with("BTCUSDT", "SHORT", 1.0, 100.5)

    def test_polls_until_filled_within_wait_window(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.side_effect = [{"status": "NEW"}, {"status": "NEW"}, {"status": "FILLED"}]

        with unittest.mock.patch("bot.main.time.sleep"):
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)

        self.assertTrue(result)
        ex.cancel_regular_order.assert_not_called()

    def test_cancels_and_returns_false_when_completely_unfilled_at_timeout(self):
        """[2026-08-10 핵심 테스트] 시간 안에 전혀 체결 안 되면 시장가로 쫓아가지 않고
        취소 후 포기해야 한다 — 이게 "리스크 최소화"라는 기능 목적의 핵심이다."""
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        # 시간 경과 시뮬레이션: time.time()이 매번 늘어나 deadline을 넘기게 함
        times = iter([0.0, 0.1, 100.0])  # 세 번째 호출에서 deadline(2.0초) 초과로 루프 탈출
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 100.0)), \
             unittest.mock.patch("bot.main.time.sleep"):
            ex.get_order_status.return_value = {"status": "NEW", "executedQty": "0"}
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)

        ex.cancel_regular_order.assert_called_once_with("BTCUSDT", 1)
        ex.open_market_position.assert_not_called()  # 시장가로 쫓아가면 안 됨
        self.assertFalse(result)

    def test_partial_fill_at_timeout_still_cancels_remainder_but_returns_true(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        times = iter([0.0, 0.1, 100.0])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 100.0)), \
             unittest.mock.patch("bot.main.time.sleep"):
            ex.get_order_status.return_value = {"status": "PARTIALLY_FILLED", "executedQty": "0.4"}
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)

        ex.cancel_regular_order.assert_called_once_with("BTCUSDT", 1)
        self.assertTrue(result)  # 부분체결분은 그대로 추적(True) — 호출부 포지션 폴링이 실제 수량을 잡음


if __name__ == "__main__":
    unittest.main()
