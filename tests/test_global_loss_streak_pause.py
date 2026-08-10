"""[2026-08-11 사용자요청] "전체 5연패 -> 10분 전체 정지" 서킷브레이커 검증.
심볼별 격리와 별개로, 서로 다른 심볼에서 연속으로 져도 카운트가 누적되는지,
승리 1회로 리셋되는지, 임계치 도달 시 10분 정지되는지 확인한다. 실 API 호출 없음."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.global_loss_streak_threshold = 5
    c.global_pause_min = 10.0
    c.symbol_blacklist_loss_threshold = 99  # 이 테스트에서 심볼 격리 부작용 방지
    c.symbol_blacklist_min_loss_streak = 99
    c.post_win_reentry_cooldown_min = 0.001
    return c


class GlobalLossStreakPauseTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stats_path = stats_path
        return PositionManager(cfg())

    def test_not_paused_initially(self):
        pm = self.make_manager()
        self.assertFalse(pm.is_globally_paused())

    def test_losses_across_different_symbols_accumulate(self):
        pm = self.make_manager()
        for i in range(4):
            pm.record_result(f"SYM{i}USDT", -1.0, -0.5, side="LONG")
        self.assertEqual(pm.global_consecutive_losses, 4)
        self.assertFalse(pm.is_globally_paused())

    def test_fifth_consecutive_loss_triggers_pause(self):
        pm = self.make_manager()
        for i in range(5):
            pm.record_result(f"SYM{i}USDT", -1.0, -0.5, side="LONG")
        self.assertTrue(pm.is_globally_paused())
        self.assertEqual(pm.global_consecutive_losses, 0)  # 트리거 후 카운터 리셋

    def test_win_resets_global_streak(self):
        pm = self.make_manager()
        for i in range(4):
            pm.record_result(f"SYM{i}USDT", -1.0, -0.5, side="LONG")
        pm.record_result("WINUSDT", 1.0, 0.5, side="LONG")
        self.assertEqual(pm.global_consecutive_losses, 0)
        pm.record_result("SYM5USDT", -1.0, -0.5, side="LONG")
        self.assertEqual(pm.global_consecutive_losses, 1)
        self.assertFalse(pm.is_globally_paused())

    def test_manual_origin_does_not_count_toward_global_streak(self):
        pm = self.make_manager()
        for i in range(5):
            pm.record_result(f"SYM{i}USDT", -1.0, -0.5, origin="manual", side="LONG")
        self.assertEqual(pm.global_consecutive_losses, 0)
        self.assertFalse(pm.is_globally_paused())

    def test_pause_persists_and_restores_across_restart(self):
        pm = self.make_manager()
        for i in range(5):
            pm.record_result(f"SYM{i}USDT", -1.0, -0.5, side="LONG")
        self.assertTrue(pm.is_globally_paused())
        pm2 = PositionManager(cfg())
        self.assertTrue(pm2.is_globally_paused())

    def test_pause_expires_after_duration(self):
        pm = self.make_manager()
        with patch("bot.position_manager.time.time", return_value=1000.0):
            for i in range(5):
                pm.record_result(f"SYM{i}USDT", -1.0, -0.5, side="LONG")
        with patch("bot.position_manager.time.time", return_value=1000.0 + 10 * 60 + 1):
            self.assertFalse(pm.is_globally_paused())


if __name__ == "__main__":
    unittest.main()
