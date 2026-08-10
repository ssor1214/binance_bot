import unittest

from bot.config import Config
from bot.main import entry_market_quality_ok


class FakeExchange:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask

    def get_book_ticker(self, symbol: str) -> dict:
        return {"bid": self.bid, "ask": self.ask}


class EntryMarketQualityTests(unittest.TestCase):
    def cfg(self):
        c = Config()
        c.max_entry_spread_pct = 0.18
        return c

    def test_allows_tight_spread(self):
        self.assertTrue(entry_market_quality_ok(FakeExchange(99.95, 100.05), self.cfg(), "TESTUSDT"))

    def test_blocks_wide_spread(self):
        self.assertFalse(entry_market_quality_ok(FakeExchange(99.0, 101.0), self.cfg(), "TESTUSDT"))

    def test_disabled_when_threshold_is_zero(self):
        c = self.cfg()
        c.max_entry_spread_pct = 0.0
        self.assertTrue(entry_market_quality_ok(FakeExchange(90.0, 110.0), c, "TESTUSDT"))


if __name__ == "__main__":
    unittest.main()
