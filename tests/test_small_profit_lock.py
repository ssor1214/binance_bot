import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 3.5
    c.take_profit_hard_cap = 20.0
    c.take_profit_min = 4.0
    c.short_take_profit_min = 4.0
    c.trail_drawdown_pct = 1.5
    c.fee_rate_roundtrip = 0.001
    c.small_profit_balance_threshold = 0.0
    c.small_profit_target_usdt = 0.04
    c.small_profit_immediate_max_roe = 1.5
    c.small_profit_lock_balance_threshold = 50.0
    c.small_profit_lock_roe = 1.0
    c.small_profit_lock_drawdown_roe = 0.4
    return c


class SmallProfitLockTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        pm = PositionManager(cfg())
        pm.total_balance = 38.0
        return pm

    def test_sub_50_account_arms_at_one_percent_roe_and_locks_after_drawdown(self):
        pm = self.make_manager()
        pm.track("UBUSDT", "SHORT", entry_price=0.13125, quantity=243.0, leverage=4)

        # SHORT price move of -0.25% is +1.0% ROE. It should arm, not close yet.
        self.assertIsNone(pm.evaluate("UBUSDT", 0.130921875))
        self.assertTrue(pm.positions["UBUSDT"].armed)
        self.assertAlmostEqual(pm.positions["UBUSDT"].peak_pnl, 1.0)

        # Back to +0.6% ROE leaves roughly +0.2% ROE after 4x roundtrip fee.
        action = pm.evaluate("UBUSDT", 0.13106)
        self.assertEqual(action, "TAKE_PROFIT")

    def test_lock_does_not_arm_when_roundtrip_fee_would_eat_profit(self):
        pm = self.make_manager()
        pm.cfg.fee_rate_roundtrip = 0.01
        pm.track("UBUSDT", "SHORT", entry_price=0.13125, quantity=243.0, leverage=4)

        self.assertIsNone(pm.evaluate("UBUSDT", 0.130921875))
        self.assertFalse(pm.positions["UBUSDT"].armed)

    def test_lock_is_disabled_at_50_usdt_or_more(self):
        pm = self.make_manager()
        pm.total_balance = 50.0
        pm.track("UBUSDT", "SHORT", entry_price=0.13125, quantity=243.0, leverage=4)

        self.assertIsNone(pm.evaluate("UBUSDT", 0.130921875))
        self.assertFalse(pm.positions["UBUSDT"].armed)

    def test_bot_small_profit_target_does_not_cut_strong_runner(self):
        pm = self.make_manager()
        pm.cfg.small_profit_balance_threshold = 50.0
        pm.cfg.small_profit_target_usdt = 0.10
        pm.track("TUTUSDT", "LONG", entry_price=0.1764340909, quantity=440.0, leverage=3, origin="bot")

        self.assertIsNone(pm.evaluate("TUTUSDT", 0.18004))
        self.assertTrue(pm.positions["TUTUSDT"].armed)

    def test_manual_position_is_not_closed_by_small_profit_target(self):
        pm = self.make_manager()
        pm.cfg.small_profit_balance_threshold = 50.0
        pm.cfg.small_profit_target_usdt = 0.10
        pm.track("TUTUSDT", "LONG", entry_price=0.1764340909, quantity=440.0, leverage=3, origin="manual")

        self.assertIsNone(pm.evaluate("TUTUSDT", 0.18004))
        self.assertTrue(pm.positions["TUTUSDT"].armed)


if __name__ == "__main__":
    unittest.main()
