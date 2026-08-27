import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.exchange import Exchange


class PriceTickRoundingTests(unittest.TestCase):
    def test_round_price_aligns_to_tick_size_not_only_precision(self):
        ex = Exchange.__new__(Exchange)
        ex.get_symbol_filters = lambda symbol: {
            "price_precision": 6,
            "tick_size": 0.0001,
        }

        self.assertEqual(ex.round_price("CLOUSDT", 0.1160448), 0.1160)
        self.assertEqual(ex.round_price("VELVETUSDT", 0.550165), 0.5501)
        self.assertEqual(ex.round_price("BICOUSDT", 0.038158549), 0.0381)

    def test_round_price_falls_back_to_precision_when_tick_missing(self):
        ex = Exchange.__new__(Exchange)
        ex.get_symbol_filters = lambda symbol: {
            "price_precision": 4,
            "tick_size": 0.0,
        }

        self.assertEqual(ex.round_price("TESTUSDT", 1.23456), 1.2346)

    def test_format_price_avoids_scientific_notation(self):
        ex = Exchange.__new__(Exchange)
        ex.get_symbol_filters = lambda symbol: {
            "price_precision": 8,
            "tick_size": 0.00000001,
        }

        self.assertEqual(ex.format_price("NEIROUSDT", 0.000083), "0.00008300")


if __name__ == "__main__":
    unittest.main()
