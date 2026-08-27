"""[2026-08-13 실거래 복기] EARLY_EXIT/EXTERNAL_CLOSE_LOSS 21건 전부(100%)가 청산후15분내
회복됐고 그중 52%가 진입 60초 이내 초단기 청산이었던 실측을 근거로, 진입 직후
early_exit_min_hold_sec(기본 120초) 동안은 EARLY_EXIT 발동을 막는 가드를 검증한다.
120초 가드 백테스트(13건)에서 85%가 가드기간 동안 정식손절에 안 닿고 생존함을 확인 후
추가한 기능. 실 API 호출 없음."""
import unittest
from unittest.mock import MagicMock, patch

from bot.config import Config
import pandas as pd

from bot.main import check_ambiguous_quick_profit_exit, check_early_exit, check_first_60s_failure
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.early_exit_min_loss_roe = 1.0
    c.early_exit_min_hold_sec = 120.0
    c.reversal_min_votes = 3
    c.short_early_fail_enabled = True
    c.short_early_fail_min_hold_sec = 15.0
    c.short_early_fail_max_hold_sec = 120.0
    c.short_early_fail_min_favorable_roe = 0.5
    c.short_early_fail_trigger_roe = 1.0
    c.first_60s_fail_min_hold_sec = 30.0
    return c


class EarlyExitHoldGuardTests(unittest.TestCase):
    def make_manager_and_position(self, c, entered_seconds_ago, side="LONG"):
        pm = PositionManager(c)
        pm.track(symbol="BTCUSDT", side=side, entry_price=100.0, quantity=1.0, leverage=4.0)
        pm.positions["BTCUSDT"].entered_at = __import__("time").time() - entered_seconds_ago
        return pm

    def test_blocks_within_guard_window_even_if_reversal_would_fire(self):
        """진입 30초 후, 손실/반전 조건은 다 맞아도 120초 가드 안이면 발동하면 안 된다."""
        c = cfg()
        pm = self.make_manager_and_position(c, entered_seconds_ago=30, side="LONG")
        ex = MagicMock()
        ex.get_mark_price.return_value = 98.0  # -2% price -> ROE -8%(leverage 4x), 손실 충분
        with patch("bot.main.detect_reversal", return_value=True):
            result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertFalse(result)
        ex.get_klines.assert_not_called()  # 가드에 걸려 지표 조회까지도 안 가야 함

    def test_fires_after_guard_window_when_reversal_confirmed(self):
        """120초가 지나면 기존처럼 손실+반전 조건 충족 시 정상 발동해야 한다."""
        c = cfg()
        pm = self.make_manager_and_position(c, entered_seconds_ago=150, side="LONG")
        ex = MagicMock()
        ex.get_mark_price.return_value = 98.0  # ROE -8% (leverage 4x) — early_exit_min_loss_roe(1%) 이상, stop_loss_pct(6%) 이내는 아님
        # ROE가 -stop_loss_pct(6%)를 넘으면 정식손절 대상이라 early_exit이 관여 안 하므로
        # 손실을 -1.5%(ROE -6%보다 얕게) 정도로 맞춘다.
        ex.get_mark_price.return_value = 99.625  # price -0.375% -> ROE -1.5%(4x), 1%~6% 사이
        with patch("bot.main.add_indicators", side_effect=lambda df, cfg: df), \
             patch("bot.main.detect_reversal", return_value=True):
            result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertTrue(result)

    def test_guard_disabled_when_zero(self):
        c = cfg()
        c.early_exit_min_hold_sec = 0.0
        pm = self.make_manager_and_position(c, entered_seconds_ago=5, side="LONG")
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.625  # ROE -1.5%
        with patch("bot.main.add_indicators", side_effect=lambda df, cfg: df), \
             patch("bot.main.detect_reversal", return_value=True):
            result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertTrue(result)

    def test_blocks_when_mtf_still_supports_position_direction(self):
        c = cfg()
        c.early_exit_mtf_guard_min_ratio = 0.5
        pm = self.make_manager_and_position(c, entered_seconds_ago=150, side="LONG")
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.625  # ROE -1.5%
        with patch("bot.main.add_indicators", side_effect=lambda df, cfg: df), \
             patch("bot.main.detect_reversal", return_value=True), \
             patch("bot.main.mtf_trend_alignment", return_value=(1, 2)):
            result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertFalse(result)

    def test_fires_when_mtf_guard_disabled(self):
        c = cfg()
        c.early_exit_mtf_guard_min_ratio = 0.0
        pm = self.make_manager_and_position(c, entered_seconds_ago=150, side="LONG")
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.625
        with patch("bot.main.add_indicators", side_effect=lambda df, cfg: df), \
             patch("bot.main.detect_reversal", return_value=True):
            result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertTrue(result)

    def test_no_position_returns_false(self):
        c = cfg()
        pm = PositionManager(c)
        ex = MagicMock()
        self.assertFalse(check_early_exit(ex, pm, c, "BTCUSDT"))

    def test_short_early_fail_fires_after_its_own_min_hold(self):
        c = cfg()
        pm = self.make_manager_and_position(c, entered_seconds_ago=60, side="SHORT")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.2
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.4  # SHORT 기준 price +0.4% -> ROE -1.6%(4x)
        result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertTrue(result)
        ex.get_klines.assert_not_called()

    def test_short_early_fail_does_not_fire_before_its_min_hold(self):
        c = cfg()
        pm = self.make_manager_and_position(c, entered_seconds_ago=3, side="SHORT")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.0
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.4
        result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertFalse(result)
        ex.get_klines.assert_not_called()

    def test_short_early_fail_does_not_fire_if_favorable_move_was_large_enough(self):
        c = cfg()
        pm = self.make_manager_and_position(c, entered_seconds_ago=60, side="SHORT")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.8
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.4
        result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertFalse(result)

    def test_long_is_not_affected_by_short_early_fail_rule(self):
        c = cfg()
        pm = self.make_manager_and_position(c, entered_seconds_ago=60, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.0
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.6  # LONG 기준 ROE -1.6%
        with patch("bot.main.detect_reversal", return_value=True):
            result = check_early_exit(ex, pm, c, "BTCUSDT")
        self.assertFalse(result)  # LONG은 기존 hold guard 유지


class First60sFailureTests(unittest.TestCase):
    def make_manager_and_position(self, c, entered_seconds_ago, side="LONG"):
        pm = PositionManager(c)
        pm.track(symbol="BTCUSDT", side=side, entry_price=100.0, quantity=1.0, leverage=4.0)
        pm.positions["BTCUSDT"].entered_at = __import__("time").time() - entered_seconds_ago
        return pm

    def _df(self, open_price, close_price):
        return pd.DataFrame(
            [
                {"open": open_price, "close": close_price},
                {"open": open_price, "close": close_price},
            ]
        )

    def test_first_60s_fail_fires_on_fast_opposite_long(self):
        c = cfg()
        c.first_60s_fail_enabled = True
        c.first_60s_fail_max_hold_sec = 60.0
        c.first_60s_fail_trigger_roe = 1.0
        c.first_60s_fail_min_favorable_roe = 0.3
        pm = self.make_manager_and_position(c, entered_seconds_ago=35, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.1
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.7  # ROE -1.2%
        ex.get_klines.return_value = self._df(100.0, 99.8)  # opposite red candle
        self.assertTrue(check_first_60s_failure(ex, pm, c, "BTCUSDT"))

    def test_first_60s_fail_skips_when_small_profit_already_seen(self):
        c = cfg()
        c.first_60s_fail_enabled = True
        pm = self.make_manager_and_position(c, entered_seconds_ago=35, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.5
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.7
        ex.get_klines.return_value = self._df(100.0, 99.8)
        self.assertFalse(check_first_60s_failure(ex, pm, c, "BTCUSDT"))

    def test_first_60s_fail_skips_before_min_hold(self):
        c = cfg()
        c.first_60s_fail_enabled = True
        pm = self.make_manager_and_position(c, entered_seconds_ago=13, side="SHORT")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.0
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.4  # SHORT 기준 ROE -1.6%
        ex.get_klines.return_value = self._df(100.0, 100.2)  # opposite green candle for short
        self.assertFalse(check_first_60s_failure(ex, pm, c, "BTCUSDT"))

    def test_first_60s_fail_skips_after_window(self):
        c = cfg()
        c.first_60s_fail_enabled = True
        pm = self.make_manager_and_position(c, entered_seconds_ago=90, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.0
        ex = MagicMock()
        ex.get_mark_price.return_value = 99.7
        self.assertFalse(check_first_60s_failure(ex, pm, c, "BTCUSDT"))


class AmbiguousQuickProfitExitTests(unittest.TestCase):
    def make_manager_and_position(self, c, entered_seconds_ago, side="LONG"):
        pm = PositionManager(c)
        pm.track(symbol="BTCUSDT", side=side, entry_price=100.0, quantity=1.0, leverage=4.0)
        pm.positions["BTCUSDT"].entered_at = __import__("time").time() - entered_seconds_ago
        return pm

    def _df(self, open_price, close_price):
        return pd.DataFrame(
            [
                {"open": open_price, "close": close_price},
                {"open": open_price, "close": close_price},
            ]
        )

    def test_fires_for_unarmed_small_profit_pullback_long(self):
        c = cfg()
        c.ambiguous_quick_profit_exit_enabled = True
        c.ambiguous_quick_profit_exit_min_hold_sec = 60.0
        c.ambiguous_quick_profit_exit_max_hold_sec = 180.0
        c.ambiguous_quick_profit_exit_min_roe = 0.25
        c.ambiguous_quick_profit_exit_max_roe = 0.8
        c.ambiguous_quick_profit_exit_min_favorable_roe = 0.4
        c.ambiguous_quick_profit_exit_max_favorable_roe = 1.2
        c.ambiguous_quick_profit_exit_min_pullback_roe = 0.15
        c.min_net_take_profit_roe = 0.2
        pm = self.make_manager_and_position(c, entered_seconds_ago=90, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 0.7
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.1  # ROE +0.4%
        ex.get_klines.return_value = self._df(100.2, 99.9)  # opposite red candle
        self.assertTrue(check_ambiguous_quick_profit_exit(ex, pm, c, "BTCUSDT"))

    def test_skips_when_already_armed(self):
        c = cfg()
        c.ambiguous_quick_profit_exit_enabled = True
        pm = self.make_manager_and_position(c, entered_seconds_ago=90, side="LONG")
        pm.positions["BTCUSDT"].armed = True
        pm.positions["BTCUSDT"].max_favorable_roe = 0.7
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.1
        self.assertFalse(check_ambiguous_quick_profit_exit(ex, pm, c, "BTCUSDT"))

    def test_skips_when_profit_is_too_strong(self):
        c = cfg()
        c.ambiguous_quick_profit_exit_enabled = True
        c.ambiguous_quick_profit_exit_max_roe = 0.8
        pm = self.make_manager_and_position(c, entered_seconds_ago=90, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 1.4
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.3  # ROE +1.2%
        self.assertFalse(check_ambiguous_quick_profit_exit(ex, pm, c, "BTCUSDT"))

    def test_fires_for_mid_hold_unarmed_profit_reversal(self):
        c = cfg()
        c.ambiguous_quick_profit_exit_enabled = True
        c.ambiguous_quick_profit_exit_mid_hold_enabled = True
        c.ambiguous_quick_profit_exit_mid_min_hold_sec = 180.0
        c.ambiguous_quick_profit_exit_mid_max_hold_sec = 600.0
        c.ambiguous_quick_profit_exit_mid_min_roe = 0.0
        c.ambiguous_quick_profit_exit_mid_max_roe = 2.0
        c.ambiguous_quick_profit_exit_mid_min_favorable_roe = 0.8
        c.ambiguous_quick_profit_exit_mid_max_favorable_roe = 2.0
        c.ambiguous_quick_profit_exit_mid_min_pullback_roe = 0.4
        c.min_net_take_profit_roe = 0.0
        pm = self.make_manager_and_position(c, entered_seconds_ago=300, side="LONG")
        pm.positions["BTCUSDT"].max_favorable_roe = 1.37
        ex = MagicMock()
        ex.get_mark_price.return_value = 100.2  # ROE +0.8%, pullback 0.57%
        ex.get_klines.return_value = self._df(100.3, 100.0)
        self.assertTrue(check_ambiguous_quick_profit_exit(ex, pm, c, "BTCUSDT"))


class ExternalCloseConfirmGuardTests(unittest.TestCase):
    def test_reconcile_confirms_young_missing_position_by_symbol(self):
        src = __import__("pathlib").Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn('held_sec <= max(0.0, float(getattr(cfg, "external_close_confirm_max_hold_sec", 60.0)))', src)
        self.assertIn('confirmed_live = ex.get_position(symbol)', src)
        self.assertIn('live_positions[symbol] = confirmed_live', src)


if __name__ == "__main__":
    unittest.main()
