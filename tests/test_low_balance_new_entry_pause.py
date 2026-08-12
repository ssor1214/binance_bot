import unittest
from unittest.mock import patch

from bot.config import Config
from bot.main import (
    passes_low_balance_recovery_gate,
    select_and_enter_best_candidates,
    should_pause_new_entries_for_low_balance,
)
from bot.position_manager import PositionManager


class LowBalanceNewEntryPauseTests(unittest.TestCase):
    def test_pauses_new_entries_below_survival_threshold(self):
        cfg = Config()
        cfg.low_balance_new_entry_pause_threshold = 25.0

        self.assertTrue(should_pause_new_entries_for_low_balance(22.0, cfg))
        self.assertFalse(should_pause_new_entries_for_low_balance(25.0, cfg))
        self.assertFalse(should_pause_new_entries_for_low_balance(26.0, cfg))

    def test_threshold_can_be_disabled(self):
        cfg = Config()
        cfg.low_balance_new_entry_pause_threshold = 0.0

        self.assertFalse(should_pause_new_entries_for_low_balance(1.0, cfg))

    def test_low_balance_still_reports_candidates_without_entering(self):
        cfg = Config()
        cfg.low_balance_new_entry_pause_threshold = 25.0
        cfg.low_balance_recovery_enabled = False
        pm = PositionManager(cfg)

        class FakeTelegram:
            trading_paused = False

            def __init__(self):
                self.notified = None

            def notify_scan_candidates(self, candidates, slots):
                self.notified = (candidates, slots)

        tg = FakeTelegram()

        class FakeExchange:
            def get_margin_ratio(self):
                return 0.0

        ex = FakeExchange()
        candidate = {
            "symbol": "TESTUSDT",
            "signal": "LONG",
            "probability": 0.99,
            "score": 0.95,
        }

        with patch("bot.main.execute_entry", side_effect=AssertionError("should not enter")):
            select_and_enter_best_candidates(None, pm, cfg, tg, total_balance=20.0, candidates=[candidate])

        self.assertIsNotNone(tg.notified)
        self.assertEqual(tg.notified[0][0]["symbol"], "TESTUSDT")
        self.assertGreaterEqual(tg.notified[1], 1)

    def test_low_balance_recovery_gate_requires_high_quality(self):
        cfg = Config()
        cfg.low_balance_recovery_min_probability = 0.80
        cfg.low_balance_recovery_min_score = 0.68

        self.assertTrue(passes_low_balance_recovery_gate({"probability": 0.81, "score": 0.69}, cfg))
        self.assertFalse(passes_low_balance_recovery_gate({"probability": 0.79, "score": 0.90}, cfg))
        self.assertFalse(passes_low_balance_recovery_gate({"probability": 0.90, "score": 0.67}, cfg))

    def test_low_balance_recovery_allows_only_one_high_confidence_entry(self):
        cfg = Config()
        cfg.low_balance_new_entry_pause_threshold = 25.0
        cfg.low_balance_recovery_enabled = True
        cfg.low_balance_recovery_max_positions = 1
        cfg.low_balance_recovery_min_probability = 0.82
        cfg.low_balance_recovery_min_score = 0.72
        pm = PositionManager(cfg)
        pm.global_pause_until = 0

        class FakeTelegram:
            trading_paused = False

            def __init__(self):
                self.notified = None

            def notify_scan_candidates(self, candidates, slots):
                self.notified = (candidates, slots)

        tg = FakeTelegram()

        class FakeExchange:
            def get_margin_ratio(self):
                return 0.0

        ex = FakeExchange()

        weak = {
            "symbol": "ETHUSDT",
            "signal": "LONG",
            "probability": 0.90,
            "score": 0.71,
        }
        strong = {
            "symbol": "BTCUSDT",
            "signal": "LONG",
            "probability": 0.83,
            "score": 0.75,
        }

        entered = []

        def fake_execute(_ex, _pm, _cfg, _tg, _balance, candidate):
            entered.append(candidate["symbol"])
            return True

        with patch("bot.main.mtf_trend_alignment", return_value=(3, 3)), \
             patch("bot.main.execute_entry", side_effect=fake_execute):
            select_and_enter_best_candidates(ex, pm, cfg, tg, total_balance=20.0, candidates=[weak, strong])

        self.assertEqual(entered, ["BTCUSDT"])

    def test_default_low_balance_recovery_allows_three_slots_for_frequency_target(self):
        cfg = Config()

        self.assertEqual(cfg.low_balance_recovery_max_positions, 3)


if __name__ == "__main__":
    unittest.main()
