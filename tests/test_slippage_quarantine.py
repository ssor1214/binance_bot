"""[2026-08-10 사용자요청] "실제손절이 설정손절의 1.7배 이상 크게 나면 즉시 24시간 격리"
단위테스트. 연속손실 카운트 기반 블랙리스트와 별개로, 단 1회 손실이라도 심한 슬리피지가
있으면 즉시 발동해야 한다. 실 API를 절대 호출하지 않는다."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.slippage_quarantine_multiplier = 1.7
    c.slippage_quarantine_cooldown_min = 1440.0
    c.symbol_blacklist_loss_threshold = 99  # 연속손실 격리는 이 테스트에서 분리해서 봄
    c.symbol_blacklist_min_loss_streak = 99
    c.symbol_blacklist_cooldown_min = 60.0
    return c


class SlippageQuarantineTests(unittest.TestCase):
    def make_manager(self, config=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return PositionManager(config or cfg())

    def test_single_loss_within_normal_range_does_not_quarantine(self):
        """설정 손절폭(6%)을 정확히 맞은 정상적인 손절은 격리 대상이 아니다."""
        pm = self.make_manager()
        # price% -1.5%, leverage 4 -> ROE -6.0% (정확히 stop_loss_pct)
        pm.record_result("BTCUSDT", -1.5, -0.5, side="LONG", leverage=4.0)
        self.assertFalse(pm.is_symbol_blacklisted("BTCUSDT"))

    def test_single_severe_slippage_loss_quarantines_immediately(self):
        """[핵심] 연속손실 1회뿐이어도(threshold=99라 연속손실 격리는 발동 안 함), 실제
        ROE가 설정 손절폭의 1.7배를 넘으면 즉시 격리돼야 한다."""
        pm = self.make_manager()
        # price% -2.6%, leverage 4 -> ROE -10.4% (6.0%*1.7=10.2%를 넘김)
        pm.record_result("SCAMUSDT", -2.6, -1.5, side="LONG", leverage=4.0)
        self.assertTrue(pm.is_symbol_blacklisted("SCAMUSDT"))

    def test_quarantine_duration_is_approximately_24_hours(self):
        pm = self.make_manager()
        import time
        before = time.time()
        pm.record_result("SCAMUSDT", -2.6, -1.5, side="LONG", leverage=4.0)
        cooldown_until = pm.symbol_blacklist_until["SCAMUSDT"]
        elapsed_hours = (cooldown_until - before) / 3600
        self.assertGreater(elapsed_hours, 23.9)
        self.assertLess(elapsed_hours, 24.1)

    def test_does_not_shorten_an_already_longer_quarantine(self):
        """이미 더 긴 격리(예: 다른 사유로)가 걸려있으면, 슬리피지 격리(24시간)가 그보다
        짧다고 해서 줄여버리면 안 된다 — 더 긴 쪽을 유지."""
        pm = self.make_manager()
        import time
        far_future = time.time() + 999999  # 슬리피지 격리(24시간)보다 훨씬 긴 미래
        pm.symbol_blacklist_until["SCAMUSDT"] = far_future
        pm.record_result("SCAMUSDT", -2.6, -1.5, side="LONG", leverage=4.0)
        self.assertEqual(pm.symbol_blacklist_until["SCAMUSDT"], far_future)

    def test_win_does_not_trigger_24h_slippage_quarantine(self):
        """이익 실현은 (기존 기능인 짧은 익절-후-재진입 쿨다운과 별개로) 24시간짜리
        슬리피지 격리 대상은 아니다."""
        pm = self.make_manager()
        pm.record_result("BTCUSDT", 5.0, 2.0, side="LONG", leverage=4.0)
        cooldown_until = pm.symbol_blacklist_until.get("BTCUSDT", 0)
        import time
        elapsed_hours = (cooldown_until - time.time()) / 3600
        self.assertLess(elapsed_hours, 1.0)  # post_win_reentry_cooldown_min(분 단위)만 적용됨

    def test_manual_origin_never_quarantines(self):
        pm = self.make_manager()
        pm.record_result("BTCUSDT", -5.0, -3.0, origin="manual", side="LONG", leverage=4.0)
        self.assertFalse(pm.is_symbol_blacklisted("BTCUSDT"))

    def test_default_leverage_of_one_still_works_correctly(self):
        """leverage 인자를 안 주면 기본값 1.0으로 동작(하위호환) — 기존 record_result
        호출부(테스트 등)가 깨지지 않아야 한다."""
        pm = self.make_manager()
        # leverage 미지정 시 price% 자체가 ROE로 취급됨: -10.5% >= 6.0*1.7=10.2%
        pm.record_result("SCAMUSDT", -10.5, -3.0, side="LONG")
        self.assertTrue(pm.is_symbol_blacklisted("SCAMUSDT"))


if __name__ == "__main__":
    unittest.main()
