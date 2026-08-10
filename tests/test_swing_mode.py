"""[2026-08-10 사용자요청] "양봉이 많아지면 스윙의 관점에서 보고 익절 구간을 최대한으로
늘려라" 기능 단위테스트. strategy.is_swing_continuing()과 PositionManager.evaluate()의
swing_continuing 파라미터(익절 절대상한/트레일링 허용폭 확대)를 검증한다. 실 API 미호출."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from bot.config import Config
from bot.position_manager import PositionManager
from bot.strategy import is_swing_continuing


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
    c.swing_streak_candles = 8
    c.swing_take_profit_hard_cap = 40.0
    c.swing_trail_drawdown_multiplier = 3.0
    c.swing_cumulative_threshold_ratio = 0.6
    return c


def make_streak_df(rows):
    """rows: [(open, close, volume, volume_ma), ...] 시간순."""
    return pd.DataFrame([
        {"open": o, "close": c, "high": max(o, c), "low": min(o, c), "volume": v, "volume_ma": vm}
        for o, c, v, vm in rows
    ])


def make_chained_df(pct_changes, volume=250, volume_ma=100, start_price=100.0):
    """[2026-08-10 사용자요청] 실제 예시(TUTUSDT/BMTUSDT/BICOUSDT 분당 변동%)처럼, 이전
    캔들의 종가에 이어서 다음 캔들이 시작하는 연쇄 시세열을 만든다 — 캔들마다 open/close가
    독립적인 make_streak_df와 달리, 진짜 "누적" 흐름(중간 눌림목 포함)을 재현할 수 있다."""
    rows = []
    price = start_price
    for pct in pct_changes:
        open_ = price
        close = open_ * (1 + pct / 100)
        rows.append({"open": open_, "close": close, "high": max(open_, close), "low": min(open_, close),
                     "volume": volume, "volume_ma": volume_ma})
        price = close
    return pd.DataFrame(rows)


BIG_LONG_CANDLE = (100.0, 101.0, 300, 100)  # +1%, 3x volume
WEAK_CANDLE = (100.0, 100.3, 300, 100)  # +0.3% < threshold
BIG_SHORT_CANDLE = (100.0, 99.0, 300, 100)  # -1%, 3x volume

# 2026-08-10 사용자가 실제로 보여준 BICOUSDT 1분봉 변동률(17:49~17:57) — 중간에 -0.50%,
# -0.97% 같은 작은 눌림목이 끼어있어도 전체적으로는 뚜렷한 상승 흐름인 실제 사례.
BICOUSDT_PATTERN = [1.12, 1.36, 2.93, 1.41, -0.50, 1.71, -0.97, 1.48, 2.55]


class IsSwingContinuingTests(unittest.TestCase):
    def test_true_when_last_n_candles_all_strong_same_direction_long(self):
        df = make_chained_df([1.0] * 8)  # 연쇄 +1%씩 8개 -> 누적 약 8.3%(문턱 3.84% 크게 상회)
        self.assertTrue(is_swing_continuing(df, cfg(), "LONG"))

    def test_false_when_not_enough_candles(self):
        df = make_streak_df([BIG_LONG_CANDLE] * 7)  # need 8
        self.assertFalse(is_swing_continuing(df, cfg(), "LONG"))

    def test_true_for_real_bicousdt_pattern_with_pullback_candles(self):
        """[핵심, 사용자 실제 사례] 중간에 -0.50%, -0.97% 눌림목이 있어도, 전체 누적
        흐름이 충분히 강하면 스윙으로 인정해야 한다 — 이게 이번 재설계의 핵심 요구사항."""
        df = make_chained_df(BICOUSDT_PATTERN)
        self.assertTrue(is_swing_continuing(df, cfg(), "LONG"))

    def test_false_when_cumulative_move_too_small_despite_no_pullback(self):
        """캔들이 전부 같은 방향이어도, 누적 변동폭 자체가 문턱(pump_min_candle_chg_pct*N*0.6)
        에 못 미치면(전부 약한 캔들이면) 스윙으로 인정하면 안 된다."""
        df = make_chained_df([0.05] * 8)  # 8개 다 +0.05%씩, 누적 0.4% << 문턱(3.84%)
        self.assertFalse(is_swing_continuing(df, cfg(), "LONG"))

    def test_false_when_volume_not_elevated_even_if_price_cumulative_strong(self):
        df = make_chained_df(BICOUSDT_PATTERN, volume=110, volume_ma=100)  # 거래량 비율 1.1배(문턱 미달)
        self.assertFalse(is_swing_continuing(df, cfg(), "LONG"))

    def test_only_looks_at_last_n_even_if_earlier_history_is_weak(self):
        df = make_chained_df([0.05] + [1.0] * 8)  # 맨 앞 약한 캔들은 윈도(마지막8개)에서 제외됨
        self.assertTrue(is_swing_continuing(df, cfg(), "LONG"))

    def test_true_for_short_streak(self):
        df = make_chained_df([-1.0] * 8)
        self.assertTrue(is_swing_continuing(df, cfg(), "SHORT"))

    def test_true_for_short_with_pullback_pattern(self):
        df = make_chained_df([-p for p in BICOUSDT_PATTERN])  # 부호 반전(하락 흐름 재현)
        self.assertTrue(is_swing_continuing(df, cfg(), "SHORT"))

    def test_false_for_short_when_direction_mismatched(self):
        df = make_streak_df([BIG_LONG_CANDLE] * 8)
        self.assertFalse(is_swing_continuing(df, cfg(), "SHORT"))


class EvaluateSwingModeTests(unittest.TestCase):
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

    def test_hard_cap_extended_when_swing_continuing(self):
        """[핵심] 일반 상한(20%)을 넘겨도 swing_continuing=True면 확정하지 않고, 스윙
        상한(40%)에서만 확정돼야 한다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        # +25% ROE = price +6.25% — 일반 상한(20%)은 넘었지만 스윙 상한(40%) 미만
        action = pm.evaluate("BTCUSDT", 106.25, momentum_continuing=False, swing_continuing=True)
        self.assertIsNone(action)
        # +40% ROE = price +10% — 스윙 상한 도달
        action2 = pm.evaluate("BTCUSDT", 110.0, momentum_continuing=False, swing_continuing=True)
        self.assertEqual(action2, "TAKE_PROFIT")

    def test_hard_cap_normal_when_swing_not_continuing(self):
        """[회귀] swing_continuing=False(기본값)면 기존 20% 상한 그대로 동작해야 한다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        action = pm.evaluate("BTCUSDT", 106.25)  # +25% ROE, swing_continuing 기본 False
        self.assertEqual(action, "TAKE_PROFIT")

    def test_trailing_drawdown_tolerance_widened_when_swing_continuing(self):
        """[핵심] 트레일링 확정 문턱(1.5%p)을 넘는 하락이 와도 swing_continuing=True면
        스윙 허용폭(1.5*3=4.5%p) 안에서는 계속 태워야 한다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        pm.evaluate("BTCUSDT", 101.25)  # arm at +5% ROE
        # 고점 대비 -3%p 하락 (일반 문턱 1.5%p는 넘었지만, 스윙 허용폭 4.5%p 미만)
        action = pm.evaluate("BTCUSDT", 100.5, swing_continuing=True)
        self.assertIsNone(action)
        self.assertTrue(pm.positions["BTCUSDT"].armed)

    def test_trailing_confirms_once_swing_drawdown_tolerance_exceeded(self):
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        pm.evaluate("BTCUSDT", 101.25)  # arm at +5% ROE, peak=5%
        # 고점 대비 -5%p 하락 (스윙 허용폭 4.5%p 초과)
        action = pm.evaluate("BTCUSDT", 100.0, swing_continuing=True)
        self.assertEqual(action, "TAKE_PROFIT")

    def test_swing_mode_does_not_bypass_stop_loss(self):
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=40.0, leverage=4)
        action = pm.evaluate("BTCUSDT", 98.5, swing_continuing=True)  # -6% ROE = stop loss
        self.assertEqual(action, "STOP_LOSS")


if __name__ == "__main__":
    unittest.main()
