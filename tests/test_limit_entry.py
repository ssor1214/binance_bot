"""[2026-08-10] place_entry_order() 단위테스트 — 지정가(메이커) 진입으로 슬리피지 리스크를
없애는 기능. 실제 API를 절대 호출하지 않는다(Exchange를 가짜 객체로 완전히 대체)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import place_entry_order, should_allow_aggressive_limit_fallback


def cfg(**overrides):
    c = Config()
    c.limit_entry_enabled = True
    c.limit_entry_wait_sec = 2.0
    c.limit_entry_pullback_pct = 0.0
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


class AggressiveLimitFallbackGateTests(unittest.TestCase):
    def test_allows_mid_high_probability_candidate_with_clean_risk_flags(self):
        c = cfg(
            limit_entry_fallback_min_probability=0.78,
            limit_entry_fallback_min_score=0.72,
            low_balance_recovery_min_probability=0.72,
            low_balance_recovery_min_score=0.68,
        )
        candidate = {
            "probability": 0.78,
            "score": 0.74,
            "signal": "SHORT",
            "negative_ev_symbol": False,
            "short_reversal_risk": False,
        }
        self.assertTrue(should_allow_aggressive_limit_fallback(candidate, c))

    def test_blocks_candidate_below_dedicated_probability_floor(self):
        c = cfg(
            limit_entry_fallback_min_probability=0.78,
            limit_entry_fallback_min_score=0.72,
            low_balance_recovery_min_probability=0.72,
            low_balance_recovery_min_score=0.68,
        )
        candidate = {"probability": 0.77, "score": 0.90, "signal": "LONG"}
        self.assertFalse(should_allow_aggressive_limit_fallback(candidate, c))

    def test_blocks_short_candidate_with_reversal_risk_even_if_probability_is_high(self):
        c = cfg(
            limit_entry_fallback_min_probability=0.78,
            limit_entry_fallback_min_score=0.72,
            low_balance_recovery_min_probability=0.72,
            low_balance_recovery_min_score=0.68,
        )
        candidate = {
            "probability": 0.93,
            "score": 0.87,
            "signal": "SHORT",
            "short_reversal_risk": True,
        }
        self.assertFalse(should_allow_aggressive_limit_fallback(candidate, c))


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

    def test_pullback_offsets_limit_price_without_chasing(self):
        c = cfg(limit_entry_pullback_pct=0.03)
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 100.0, "ask": 101.0}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)
        args = ex.open_limit_position.call_args.args
        self.assertEqual(args[:3], ("BTCUSDT", "LONG", 1.0))
        self.assertAlmostEqual(args[3], 99.97)

        ex.reset_mock()
        ex.get_book_ticker.return_value = {"bid": 100.0, "ask": 101.0}
        ex.open_limit_position.return_value = {"orderId": 2}
        ex.get_order_status.return_value = {"status": "FILLED"}

        place_entry_order(ex, c, "BTCUSDT", "SHORT", 1.0)
        args = ex.open_limit_position.call_args.args
        self.assertEqual(args[:3], ("BTCUSDT", "SHORT", 1.0))
        self.assertAlmostEqual(args[3], 101.0303)

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

    def test_wait_sec_override_uses_shorter_timeout(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        times = iter([0.0, 0.1, 1.6])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 1.6)), \
             unittest.mock.patch("bot.main.time.sleep"):
            ex.get_order_status.return_value = {"status": "NEW", "executedQty": "0"}
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0, wait_sec=1.5)

        ex.cancel_regular_order.assert_called_once_with("BTCUSDT", 1)
        self.assertFalse(result)


class PlaceEntryOrderAggressiveSpikeTests(unittest.TestCase):
    """[2026-08-16] early_entry_spike 후보 전용 공격적 체결 — aggressive=True일 때만
    반대편 호가(스프레드 교차)로 주문한다. aggressive 기본값 False라 기존 테스트/호출부는
    전부 영향받지 않는다(회귀 없음)."""

    def test_aggressive_long_uses_ask_price_not_bid(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0, aggressive=True)

        ex.open_limit_position.assert_called_once_with("BTCUSDT", "LONG", 1.0, 100.5)
        self.assertTrue(result)

    def test_aggressive_short_uses_bid_price_not_ask(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        place_entry_order(ex, c, "BTCUSDT", "SHORT", 1.0, aggressive=True)

        ex.open_limit_position.assert_called_once_with("BTCUSDT", "SHORT", 1.0, 99.5)

    def test_aggressive_ignores_pullback_pct(self):
        """pullback 설정이 켜져 있어도 aggressive=True면 무시하고 반대편 호가 그대로 쓴다."""
        c = cfg(limit_entry_pullback_pct=0.03)
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 100.0, "ask": 101.0}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0, aggressive=True)
        args = ex.open_limit_position.call_args.args
        self.assertEqual(args[3], 101.0)

    def test_aggressive_default_false_preserves_existing_behavior(self):
        """aggressive 인자를 아예 안 넘기면(기존 호출부와 동일) 기존 pullback 로직 그대로."""
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "FILLED"}

        place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)

        ex.open_limit_position.assert_called_once_with("BTCUSDT", "LONG", 1.0, 99.5)

    def test_market_fallback_ignores_aggressive_flag(self):
        c = cfg(limit_entry_enabled=False)
        ex = MagicMock()
        result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0, aggressive=True)
        ex.open_market_position.assert_called_once_with("BTCUSDT", "LONG", 1.0)
        ex.open_limit_position.assert_not_called()
        self.assertTrue(result)


class PlaceEntryOrderAggressiveFallbackTests(unittest.TestCase):
    def test_fallback_gate_uses_dedicated_floor_and_blocks_risky_short(self):
        c = cfg()
        safe_long = {"signal": "LONG", "probability": 0.90, "score": 0.72, "negative_ev_symbol": False}
        low_conf = {"signal": "LONG", "probability": 0.70, "score": 0.72, "negative_ev_symbol": False}
        risky_short = {
            "signal": "SHORT",
            "probability": 0.91,
            "score": 0.75,
            "negative_ev_symbol": False,
            "short_reversal_risk": True,
        }
        self.assertTrue(should_allow_aggressive_limit_fallback(safe_long, c))
        self.assertFalse(should_allow_aggressive_limit_fallback(low_conf, c))
        self.assertFalse(should_allow_aggressive_limit_fallback(risky_short, c))

    def test_disabled_fallback_preserves_existing_timeout_behavior(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.5, "ask": 100.5}
        ex.open_limit_position.return_value = {"orderId": 1}
        times = iter([0.0, 0.1, 100.0])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 100.0)), \
             unittest.mock.patch("bot.main.time.sleep"):
            ex.get_order_status.return_value = {"status": "NEW", "executedQty": "0"}
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)
        self.assertFalse(result)
        ex.open_limit_position.assert_called_once_with("BTCUSDT", "LONG", 1.0, 99.5)

    def test_enabled_fallback_can_be_blocked_by_callsite_gate(self):
        c = cfg(
            limit_entry_aggressive_fallback_enabled=True,
            limit_entry_aggressive_wait_sec=1.0,
            limit_entry_aggressive_max_spread_pct=0.20,
            limit_entry_aggressive_max_chase_pct=0.20,
        )
        ex = MagicMock()
        ex.get_book_ticker.side_effect = [{"bid": 99.5, "ask": 100.5}]
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "NEW", "executedQty": "0"}
        times = iter([0.0, 0.1, 100.0])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 100.0)), \
             unittest.mock.patch("bot.main.time.sleep"):
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0, fallback_allowed=False)
        self.assertFalse(result)
        self.assertEqual(ex.open_limit_position.call_count, 1)

    def test_enabled_fallback_crosses_once_when_spread_and_chase_are_small(self):
        c = cfg(
            limit_entry_aggressive_fallback_enabled=True,
            limit_entry_aggressive_wait_sec=1.0,
            limit_entry_aggressive_max_spread_pct=0.20,
            limit_entry_aggressive_max_chase_pct=0.20,
        )
        ex = MagicMock()
        ex.get_book_ticker.side_effect = [
            {"bid": 100.00, "ask": 100.05},
            {"bid": 100.01, "ask": 100.06},
        ]
        ex.open_limit_position.side_effect = [{"orderId": 1}, {"orderId": 2}]
        ex.get_order_status.side_effect = [
            {"status": "NEW", "executedQty": "0"},
            {"status": "NEW", "executedQty": "0"},
            {"status": "FILLED", "executedQty": "1"},
        ]
        times = iter([0.0, 0.1, 100.0, 100.1, 100.2])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 100.2)), \
             unittest.mock.patch("bot.main.time.sleep"):
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)
        self.assertTrue(result)
        self.assertEqual(ex.open_limit_position.call_args_list[0], call("BTCUSDT", "LONG", 1.0, 100.0))
        self.assertEqual(ex.open_limit_position.call_args_list[1], call("BTCUSDT", "LONG", 1.0, 100.06))

    def test_enabled_fallback_skips_when_spread_or_chase_is_too_large(self):
        c = cfg(
            limit_entry_aggressive_fallback_enabled=True,
            limit_entry_aggressive_wait_sec=1.0,
            limit_entry_aggressive_max_spread_pct=0.10,
            limit_entry_aggressive_max_chase_pct=0.10,
        )
        ex = MagicMock()
        ex.get_book_ticker.side_effect = [
            {"bid": 99.5, "ask": 100.5},
            {"bid": 100.0, "ask": 100.4},
        ]
        ex.open_limit_position.return_value = {"orderId": 1}
        ex.get_order_status.return_value = {"status": "NEW", "executedQty": "0"}
        times = iter([0.0, 0.1, 100.0])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 100.0)), \
             unittest.mock.patch("bot.main.time.sleep"):
            result = place_entry_order(ex, c, "BTCUSDT", "LONG", 1.0)
        self.assertFalse(result)
        self.assertEqual(ex.open_limit_position.call_count, 1)


if __name__ == "__main__":
    unittest.main()
