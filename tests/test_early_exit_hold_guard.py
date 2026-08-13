"""[2026-08-13 실거래 복기] EARLY_EXIT/EXTERNAL_CLOSE_LOSS 21건 전부(100%)가 청산후15분내
회복됐고 그중 52%가 진입 60초 이내 초단기 청산이었던 실측을 근거로, 진입 직후
early_exit_min_hold_sec(기본 120초) 동안은 EARLY_EXIT 발동을 막는 가드를 검증한다.
120초 가드 백테스트(13건)에서 85%가 가드기간 동안 정식손절에 안 닿고 생존함을 확인 후
추가한 기능. 실 API 호출 없음."""
import unittest
from unittest.mock import MagicMock, patch

from bot.config import Config
from bot.main import check_early_exit
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.early_exit_min_loss_roe = 1.0
    c.early_exit_min_hold_sec = 120.0
    c.reversal_min_votes = 3
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

    def test_no_position_returns_false(self):
        c = cfg()
        pm = PositionManager(c)
        ex = MagicMock()
        self.assertFalse(check_early_exit(ex, pm, c, "BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
