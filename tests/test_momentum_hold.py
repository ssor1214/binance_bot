"""[2026-08-10] "큰 양봉/음봉 중에는 트레일링 확정을 보류하고 더 태운다" 기능 단위테스트.
strategy.is_momentum_continuing()과 PositionManager.evaluate()의 momentum_continuing
파라미터를 각각/함께 검증한다. 실제 API를 절대 호출하지 않는다."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from bot.config import Config
from bot.position_manager import PositionManager
from bot.strategy import is_momentum_continuing


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.take_profit_hard_cap = 20.0
    c.take_profit_min = 4.0
    c.short_take_profit_min = 4.0
    c.trail_drawdown_pct = 1.5
    c.fee_rate_roundtrip = 0.001
    c.small_profit_balance_threshold = 0.0
    c.small_profit_lock_balance_threshold = 0.0
    c.pump_min_candle_chg_pct = 0.8
    c.pump_min_volume_ratio = 2.3
    return c


def make_df(open_, close, volume, volume_ma):
    return pd.DataFrame([{"open": open_, "close": close, "high": max(open_, close),
                           "low": min(open_, close), "volume": volume, "volume_ma": volume_ma}])


class IsMomentumContinuingTests(unittest.TestCase):
    def test_true_for_long_when_big_bullish_candle_with_volume(self):
        df = make_df(open_=100.0, close=101.0, volume=300, volume_ma=100)  # +1% candle, 3x volume
        self.assertTrue(is_momentum_continuing(df, cfg(), "LONG"))

    def test_false_for_long_when_candle_change_below_threshold(self):
        df = make_df(open_=100.0, close=100.3, volume=300, volume_ma=100)  # +0.3% < 0.8% threshold
        self.assertFalse(is_momentum_continuing(df, cfg(), "LONG"))

    def test_false_for_long_when_volume_not_elevated(self):
        df = make_df(open_=100.0, close=101.0, volume=105, volume_ma=100)  # big candle but volume_ratio only 1.05
        self.assertFalse(is_momentum_continuing(df, cfg(), "LONG"))

    def test_false_for_long_when_candle_is_bearish(self):
        df = make_df(open_=100.0, close=99.0, volume=300, volume_ma=100)  # moving against LONG
        self.assertFalse(is_momentum_continuing(df, cfg(), "LONG"))

    def test_true_for_short_when_big_bearish_candle_with_volume(self):
        df = make_df(open_=100.0, close=99.0, volume=300, volume_ma=100)  # -1% candle, favors SHORT
        self.assertTrue(is_momentum_continuing(df, cfg(), "SHORT"))

    def test_false_for_short_when_candle_is_bullish(self):
        df = make_df(open_=100.0, close=101.0, volume=300, volume_ma=100)
        self.assertFalse(is_momentum_continuing(df, cfg(), "SHORT"))

    def test_false_when_volume_ma_is_zero_or_missing(self):
        df = make_df(open_=100.0, close=101.0, volume=300, volume_ma=0)
        self.assertFalse(is_momentum_continuing(df, cfg(), "LONG"))


class EvaluateMomentumHoldTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        pm = PositionManager(cfg())
        pm.total_balance = 100.0
        return pm

    def test_trailing_confirms_normally_when_momentum_not_continuing(self):
        """[회귀] momentum_continuing 파라미터를 안 주거나 False면 기존 동작(즉시 확정)
        그대로여야 한다 — 하위호환 확인."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        # arm at +10% ROE (price +2.5%)
        self.assertIsNone(pm.evaluate("BTCUSDT", 102.5))
        self.assertTrue(pm.positions["BTCUSDT"].armed)
        # drawdown well past 1.5%p triggers normally without momentum flag
        action = pm.evaluate("BTCUSDT", 100.875, momentum_continuing=False)
        self.assertEqual(action, "TAKE_PROFIT")

    def test_trailing_confirm_is_held_when_momentum_continuing(self):
        """[2026-08-10 신규기능 핵심 테스트] 트레일링 확정 조건(peak-drawdown)이 충족돼도
        momentum_continuing=True면 이번 주기엔 확정하지 않고 계속 태워야 한다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        self.assertIsNone(pm.evaluate("BTCUSDT", 101.25))  # arm at +5%
        action = pm.evaluate("BTCUSDT", 100.875, momentum_continuing=True)  # same drawdown, but momentum still on
        self.assertIsNone(action)
        self.assertTrue(pm.positions["BTCUSDT"].armed)  # still armed/tracked, not closed

    def test_peak_still_updates_while_held_by_momentum(self):
        """모멘텀 보류 중에도 고점(peak_pnl)은 계속 갱신돼야, 모멘텀이 꺾인 뒤 정상적으로
        그 시점 고점 기준 트레일링이 재개된다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        pm.evaluate("BTCUSDT", 101.25)  # arm at +5%
        pm.evaluate("BTCUSDT", 103.0, momentum_continuing=True)  # ride higher, +12% ROE, held by momentum
        self.assertAlmostEqual(pm.positions["BTCUSDT"].peak_pnl, 12.0)

    def test_momentum_hold_does_not_bypass_hard_cap(self):
        """절대 상한(take_profit_hard_cap)은 momentum_continuing과 무관하게 항상 확정돼야
        한다 — 무한정 노출을 막는 안전장치가 이 기능으로 우회되면 안 된다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        # +20% ROE = price +5% (hard cap)
        action = pm.evaluate("BTCUSDT", 105.0, momentum_continuing=True)
        self.assertEqual(action, "TAKE_PROFIT")

    def test_momentum_hold_does_not_bypass_stop_loss(self):
        """손절선은 momentum_continuing과 무관하게 그대로 작동해야 한다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        # -6% ROE = price -1.5% (stop loss)
        action = pm.evaluate("BTCUSDT", 98.5, momentum_continuing=True)
        self.assertEqual(action, "STOP_LOSS")


if __name__ == "__main__":
    unittest.main()
