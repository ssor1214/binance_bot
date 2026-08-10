"""[2026-08-10 사용자요청] "전체 승률만 보면 롱/숏 중 한쪽이 나빠도 숨겨진다" — 방향별
(LONG/SHORT) 승/패 카운터가 record_result에서 정확히 누적되고, 재시작해도 파일에서
복원되는지 검증한다. 실 API를 절대 호출하지 않는다."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.post_win_reentry_cooldown_min = 1.0
    c.symbol_blacklist_loss_threshold = 99  # 이 테스트에서 블랙리스트 부작용 방지
    c.symbol_blacklist_min_loss_streak = 99
    return c


class DirectionSplitStatsTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", self.stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return PositionManager(cfg())

    def test_long_win_increments_long_wins_only(self):
        pm = self.make_manager()
        pm.record_result("BTCUSDT", 1.0, 0.5, side="LONG")
        self.assertEqual(pm.long_wins, 1)
        self.assertEqual(pm.long_losses, 0)
        self.assertEqual(pm.short_wins, 0)
        self.assertEqual(pm.short_losses, 0)
        self.assertEqual(pm.wins, 1)  # 기존 통합 카운터도 그대로 유지

    def test_short_loss_increments_short_losses_only(self):
        pm = self.make_manager()
        pm.record_result("ETHUSDT", -1.0, -0.5, side="SHORT")
        self.assertEqual(pm.short_losses, 1)
        self.assertEqual(pm.short_wins, 0)
        self.assertEqual(pm.long_wins, 0)
        self.assertEqual(pm.long_losses, 0)
        self.assertEqual(pm.losses, 1)

    def test_mixed_results_split_correctly(self):
        """블로그 예시와 동일한 시나리오: 롱 5전5승, 숏 5전1승4패."""
        pm = self.make_manager()
        for i in range(5):
            pm.record_result(f"L{i}USDT", 1.0, 0.1, side="LONG")
        pm.record_result("S0USDT", 1.0, 0.1, side="SHORT")
        for i in range(1, 5):
            pm.record_result(f"S{i}USDT", -1.0, -0.1, side="SHORT")

        self.assertEqual((pm.long_wins, pm.long_losses), (5, 0))
        self.assertEqual((pm.short_wins, pm.short_losses), (1, 4))
        self.assertEqual(pm.wins, 6)
        self.assertEqual(pm.losses, 4)
        self.assertEqual(pm.total_trades, 10)

    def test_long_short_pnl_accumulated_separately(self):
        """[2026-08-10 사용자요청] 방향별 "횟수"뿐 아니라 "손익 금액"도 따로 누적돼야
        평균이익/평균손실/손익비 같은 정밀 분석이 가능하다."""
        pm = self.make_manager()
        pm.record_result("BTCUSDT", 1.0, 0.5, side="LONG")
        pm.record_result("ETHUSDT", -1.0, -0.3, side="LONG")
        pm.record_result("SOLUSDT", 1.0, 0.2, side="SHORT")
        self.assertAlmostEqual(pm.long_pnl_usdt, 0.2)  # 0.5 - 0.3
        self.assertAlmostEqual(pm.short_pnl_usdt, 0.2)
        self.assertAlmostEqual(pm.realized_pnl_usdt, 0.4)  # 전체 합계는 그대로 유지

    def test_pnl_persists_and_restores_across_restart(self):
        pm = self.make_manager()
        pm.record_result("BTCUSDT", 1.0, 1.5, side="LONG")
        pm.record_result("ETHUSDT", -1.0, -0.7, side="SHORT")
        pm2 = PositionManager(cfg())
        self.assertAlmostEqual(pm2.long_pnl_usdt, 1.5)
        self.assertAlmostEqual(pm2.short_pnl_usdt, -0.7)

    def test_manual_origin_does_not_affect_direction_stats(self):
        """[회귀] 수동 진입은 기존처럼 통계에서 완전히 제외돼야 한다(방향별 카운터도 동일)."""
        pm = self.make_manager()
        pm.record_result("BTCUSDT", 5.0, 2.0, origin="manual", side="LONG")
        self.assertEqual(pm.long_wins, 0)
        self.assertEqual(pm.total_trades, 0)

    def test_persists_and_restores_direction_stats_across_restart(self):
        pm = self.make_manager()
        pm.record_result("BTCUSDT", 1.0, 0.5, side="LONG")
        pm.record_result("ETHUSDT", -1.0, -0.5, side="SHORT")

        raw = json.loads(self.stats_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["long_wins"], 1)
        self.assertEqual(raw["short_losses"], 1)

        pm2 = PositionManager(cfg())  # 같은 STATS_FILE 경로(패치 유지 중)에서 재로드
        self.assertEqual(pm2.long_wins, 1)
        self.assertEqual(pm2.short_losses, 1)

    def test_loads_old_stats_file_without_direction_fields_defaults_to_zero(self):
        """[회귀] 오늘 이전에 저장된 구버전 통계 파일(long_wins 등 필드 자체가 없음)을
        읽어도 죽지 않고 0으로 안전하게 시작해야 한다."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        stats_path.write_text(json.dumps({
            "win_streak": 2, "total_trades": 10, "wins": 6, "losses": 4,
            "realized_pnl_usdt": 1.23, "recent_trade_results": [],
        }), encoding="utf-8")
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        pm = PositionManager(cfg())
        self.assertEqual(pm.total_trades, 10)  # 기존 필드는 정상 복원
        self.assertEqual(pm.long_wins, 0)  # 신규 필드는 안전하게 0


if __name__ == "__main__":
    unittest.main()
