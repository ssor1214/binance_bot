import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.grid_e3 import GridState, build_grid_levels


class GridE3Tests(unittest.TestCase):
    def test_build_grid_levels_spans_symmetric_range(self):
        levels = build_grid_levels(100.0, 10.0, 5)
        self.assertEqual(len(levels), 5)
        self.assertAlmostEqual(levels[0], 90.0)
        self.assertAlmostEqual(levels[-1], 110.0)
        self.assertAlmostEqual(levels[2], 100.0)

    def test_buy_then_sell_releases_same_rung_inventory(self):
        grid = GridState([90.0, 95.0, 100.0, 105.0, 110.0])
        sell_rung = grid.register_buy_fill(1)
        self.assertEqual(sell_rung, 2)
        self.assertEqual(grid.held_buy_rungs, {1})
        buy_rung = grid.register_sell_fill(2)
        self.assertEqual(buy_rung, 1)
        self.assertEqual(grid.held_buy_rungs, set())

    def test_eligible_orders_respect_current_price_and_inventory(self):
        grid = GridState([90.0, 95.0, 100.0, 105.0, 110.0], {1})
        self.assertEqual(grid.eligible_buy_rungs(102.0), [0, 2])
        self.assertEqual(grid.eligible_sell_rungs(102.0), [2])


if __name__ == "__main__":
    unittest.main()
