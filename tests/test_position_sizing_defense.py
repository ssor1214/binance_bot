import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.position_size_min = 0.1
    c.position_size_step = 0.1
    c.position_size_max = 0.5
    c.small_balance_threshold = 100.0
    c.small_balance_max_ratio = 1.0
    c.symbol_blacklist_loss_threshold = 2
    c.symbol_blacklist_min_loss_streak = 3
    c.symbol_blacklist_cooldown_min = 60.0
    c.symbol_cooldown_loss_count = 2
    c.symbol_cooldown_window_min = 30.0
    c.symbol_cooldown_block_min = 10.0
    c.post_win_reentry_cooldown_min = 0.0
    c.loss_reentry_size_mult = 0.55
    c.loss_reentry_min_mult = 0.30
    c.short_size_multiplier = 0.6
    c.recent_performance_window = 10
    c.recent_performance_min_trades = 5
    c.recent_defense_winrate_threshold = 0.45
    c.recent_defense_size_mult = 0.75
    c.direction_performance_window = 5
    c.direction_performance_min_trades = 3
    c.direction_loss_size_mult = 0.85
    c.direction_min_size_mult = 0.35
    c.ev_filter_min_sample = 15
    c.ev_filter_hard_pause = False
    c.ev_defense_size_mult = 0.75
    c.take_profit_min = 4.0
    c.stop_loss_pct = 3.5
    c.fee_rate_roundtrip = 0.001
    c.leverage_max = 4
    return c


class PositionSizingDefenseTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return PositionManager(cfg())

    def test_losses_reduce_reentry_size_before_blocking_symbol(self):
        pm = self.make_manager()
        base = pm.next_position_size_ratio(38.0, symbol="ABCUSDT")

        pm.record_result("ABCUSDT", -1.0, -0.2, side="LONG")
        after_one_loss = pm.next_position_size_ratio(38.0, symbol="ABCUSDT")
        self.assertLess(after_one_loss, base)
        self.assertFalse(pm.is_symbol_blacklisted("ABCUSDT"))

        # [2026-08-13] 2번째 손실은 연속손실 스트릭(min_loss_streak=3) 기준으로는 아직
        # 차단 대상이 아니지만, 새로 추가된 시간창 쿨다운(symbol_cooldown_loss_count=2,
        # window=30min)이 먼저 걸려 짧게(block_min=10) 차단된다.
        pm.record_result("ABCUSDT", -1.0, -0.2, side="LONG")
        self.assertTrue(pm.is_symbol_blacklisted("ABCUSDT"))

        pm.record_result("ABCUSDT", -1.0, -0.2, side="LONG")
        self.assertTrue(pm.is_symbol_blacklisted("ABCUSDT"))

    def test_symbol_cooldown_window_blocks_repeated_losses_even_after_a_win(self):
        """연속(스트릭) 조건과 달리, 창 안에서는 중간에 승리가 끼어도 손실 횟수가 누적된다."""
        pm = self.make_manager()
        pm.record_result("XYZUSDT", -1.0, -0.2, side="LONG")
        self.assertFalse(pm.is_symbol_blacklisted("XYZUSDT"))
        pm.record_result("XYZUSDT", 1.0, 0.2, side="LONG")  # 승리로 스트릭은 끊기지만
        self.assertFalse(pm.is_symbol_blacklisted("XYZUSDT"))
        pm.record_result("XYZUSDT", -1.0, -0.2, side="LONG")
        self.assertTrue(pm.is_symbol_blacklisted("XYZUSDT"))

    def test_symbol_blacklist_loss_threshold_applies_without_being_overridden_by_min_loss_streak(self):
        """loss_threshold를 더 낮췄다면 그 의도가 그대로 발동 시점에 반영돼야 한다."""
        pm = self.make_manager()
        pm.cfg.symbol_blacklist_loss_threshold = 1
        pm.cfg.symbol_blacklist_min_loss_streak = 3
        pm.cfg.symbol_cooldown_loss_count = 99  # 연속손실 규칙만 분리해서 확인

        pm.record_result("FASTCUTUSDT", -1.0, -0.2, side="LONG")
        self.assertTrue(
            pm.is_symbol_blacklisted("FASTCUTUSDT"),
            "loss_threshold=1이면 min_loss_streak=3에 가로막히지 않고 첫 손실 뒤 바로 차단돼야 한다",
        )

    def test_short_size_recovers_when_recent_short_results_are_positive(self):
        pm = self.make_manager()
        self.assertAlmostEqual(pm.direction_size_multiplier("SHORT"), 0.6)

        for _ in range(3):
            pm.record_result("SUSDT", 1.0, 0.2, side="SHORT")

        self.assertAlmostEqual(pm.direction_size_multiplier("SHORT"), 1.0)

    def test_direction_loss_reduces_size_without_blocking_entries(self):
        pm = self.make_manager()
        for _ in range(3):
            pm.record_result("LUSDT", -1.0, -0.2, side="LONG")

        self.assertAlmostEqual(pm.direction_size_multiplier("LONG"), 0.85)

    def test_recent_account_defense_scales_size_after_weak_window(self):
        pm = self.make_manager()
        for i in range(5):
            pm.record_result(f"X{i}USDT", -1.0, -0.2, side="LONG")

        self.assertAlmostEqual(pm.recent_performance_size_multiplier(), 0.75)

    def test_same_symbol_reentry_context_reports_recent_loss_streak(self):
        pm = self.make_manager()
        pm.record_entry_ratio("ABCUSDT", 0.20)
        pm.record_result("ABCUSDT", -1.0, -0.2, side="SHORT")

        ctx = pm.get_same_symbol_reentry_context("ABCUSDT")

        self.assertTrue(ctx["recent_reentry"])
        self.assertEqual(ctx["loss_streak"], 1)
        self.assertAlmostEqual(ctx["prev_ratio"], 0.20)

    def test_negative_structural_ev_reduces_size_without_blocking_frequency(self):
        pm = self.make_manager()
        pm.total_trades = 60
        pm.wins = 31
        pm.losses = 29

        self.assertFalse(pm.cfg.ev_filter_hard_pause)
        self.assertAlmostEqual(pm.expected_value_size_multiplier(), 0.75)

        pm.wins = 45
        pm.losses = 15
        self.assertAlmostEqual(pm.expected_value_size_multiplier(), 1.0)


if __name__ == "__main__":
    unittest.main()
