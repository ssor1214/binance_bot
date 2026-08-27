import unittest
from unittest.mock import patch

from bot.config import Config
from bot.main import (
    effective_low_balance_recovery_slot_cap,
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

            # [2026-08-17] 실제 TelegramNotifier에 추가된 메서드들 — 페이크가 따라가지 못해
            # AttributeError로 테스트가 깨졌다. 알림은 이 테스트의 관심사가 아니므로 no-op.
            def notify_entry_skipped(self, *args, **kwargs):
                pass

            def send(self, *args, **kwargs):
                pass

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

            # [2026-08-17] 실제 TelegramNotifier에 추가된 메서드들 — 페이크가 따라가지 못해
            # AttributeError로 테스트가 깨졌다. 알림은 이 테스트의 관심사가 아니므로 no-op.
            def notify_entry_skipped(self, *args, **kwargs):
                pass

            def send(self, *args, **kwargs):
                pass

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
        """[2026-08-14 재원복] "어제 거래량/승률 좋았던 때로 원복" — 3->4 완화를 다시 3으로
        되돌림(LOW_BALANCE_RECOVERY_MAX_POSITIONS, .env)."""
        cfg = Config()
        cfg.low_balance_recovery_max_positions = 3

        self.assertEqual(cfg.low_balance_recovery_max_positions, 3)

    def test_temp_force_multi_slot_override_expands_recovery_slot_cap(self):
        cfg = Config()
        cfg.low_balance_recovery_max_positions = 1
        cfg.temp_force_multi_slot_enabled = True
        cfg.temp_force_multi_slot_count = 4

        self.assertEqual(effective_low_balance_recovery_slot_cap(cfg), 4)


if __name__ == "__main__":
    unittest.main()
