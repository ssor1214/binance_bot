import unittest
from unittest.mock import patch

from bot.config import Config
from bot.main import check_unarmed_mid_hold_cut
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.take_profit_min = 4.0
    c.take_profit_hard_cap = 20.0
    c.small_profit_lock_balance_threshold = 0.0
    c.small_profit_balance_threshold = 0.0
    c.unarmed_mid_hold_cut_enabled = True
    c.unarmed_mid_hold_cut_min_minutes = 6.0
    c.unarmed_mid_hold_cut_max_minutes = 8.0
    c.unarmed_mid_hold_cut_max_favorable_roe = 0.8
    c.unarmed_mid_hold_cut_max_current_roe = 0.2
    c.unarmed_mid_hold_reversal_enabled = True
    c.unarmed_mid_hold_reversal_min_minutes = 6.0
    c.unarmed_mid_hold_reversal_max_minutes = 10.0
    c.unarmed_mid_hold_reversal_max_favorable_roe = 1.6
    c.unarmed_mid_hold_reversal_max_current_roe = -0.3
    c.stagnation_time_stop_enabled = True
    c.stagnation_time_stop_min_hold_min = 10.0
    c.stagnation_time_stop_min_roe = -1.0
    c.stagnation_time_stop_max_roe = 0.6
    c.scalp_max_hold_minutes = 60.0
    return c


class UnarmedMidHoldCutTests(unittest.TestCase):
    def test_triggers_for_weak_unarmed_trade_in_6_to_8m_window(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("TESTUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        pos.max_favorable_roe = 0.5
        with patch("bot.main.time.time", return_value=pos.entered_at + 7 * 60):
            self.assertTrue(check_unarmed_mid_hold_cut(pm, c, "TESTUSDT", 99.95))

    def test_does_not_trigger_after_armed(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("TESTUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        pos.armed = True
        pos.max_favorable_roe = 1.2
        with patch("bot.main.time.time", return_value=pos.entered_at + 7 * 60):
            self.assertFalse(check_unarmed_mid_hold_cut(pm, c, "TESTUSDT", 100.0))

    def test_triggers_for_unarmed_mid_hold_reversal_loss(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("TESTUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        pos.max_favorable_roe = 1.35
        with patch("bot.main.time.time", return_value=pos.entered_at + 9 * 60):
            self.assertTrue(check_unarmed_mid_hold_cut(pm, c, "TESTUSDT", 99.89))

    def test_does_not_trigger_for_stronger_unarmed_trend_candidate(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("TESTUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        pos.max_favorable_roe = 2.2
        with patch("bot.main.time.time", return_value=pos.entered_at + 7 * 60):
            self.assertFalse(check_unarmed_mid_hold_cut(pm, c, "TESTUSDT", 99.89))


class StagnationTimeStopTests(unittest.TestCase):
    def test_time_stop_triggers_in_10_to_15m_stagnation_band(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("TESTUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 12 * 60):
            self.assertEqual(pm.evaluate("TESTUSDT", 100.05), "TIME_STOP")

    def test_time_stop_skips_when_armed(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("TESTUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        pos.armed = True
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 12 * 60):
            self.assertIsNone(pm.evaluate("TESTUSDT", 100.05))


if __name__ == "__main__":
    unittest.main()
