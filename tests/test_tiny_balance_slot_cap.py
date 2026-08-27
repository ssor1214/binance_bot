import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.main import compute_max_positions


class TinyBalanceSlotCapTests(unittest.TestCase):
    class Cfg:
        tiny_balance_single_slot_threshold = 15.0
        aggressive_balance_threshold = 100.0
        aggressive_max_positions = 3
        balance_tier_threshold = 300.0
        max_positions_low = 3
        max_positions_high = 8
        temp_force_multi_slot_enabled = False
        temp_force_multi_slot_count = 4

    def test_below_tiny_balance_threshold_forces_single_slot(self):
        self.assertEqual(compute_max_positions(10.0, self.Cfg()), 1)

    def test_recovery_above_tiny_balance_threshold_restores_aggressive_slots(self):
        self.assertEqual(compute_max_positions(16.0, self.Cfg()), 3)

    def test_temp_force_multi_slot_override_can_open_four_slots_below_threshold(self):
        cfg = self.Cfg()
        cfg.temp_force_multi_slot_enabled = True
        cfg.temp_force_multi_slot_count = 4
        self.assertEqual(compute_max_positions(10.0, cfg), 4)


if __name__ == "__main__":
    unittest.main()
