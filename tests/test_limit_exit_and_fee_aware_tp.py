import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import fee_aware_take_profit_price, fee_aware_take_profit_roe, place_exit_order


def cfg(**overrides):
    c = Config()
    c.limit_exit_enabled = True
    c.limit_exit_wait_sec = 1.0
    c.limit_exit_improve_pct = 0.0
    c.take_profit_min = 3.0
    c.short_take_profit_min = 0.3
    c.fee_rate_roundtrip = 0.001
    c.min_net_take_profit_roe = 0.2
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class FeeAwareTakeProfitTests(unittest.TestCase):
    def test_uses_fee_floor_when_base_take_profit_is_too_low(self):
        c = cfg(short_take_profit_min=0.3, fee_rate_roundtrip=0.001, min_net_take_profit_roe=0.2)
        roe = fee_aware_take_profit_roe(c, "SHORT", leverage=5)
        self.assertAlmostEqual(roe, 0.4)

    def test_long_price_uses_higher_of_base_or_fee_floor(self):
        c = cfg(take_profit_min=3.0, fee_rate_roundtrip=0.001, min_net_take_profit_roe=0.2)
        price = fee_aware_take_profit_price(c, 100.0, "LONG", leverage=2)
        self.assertAlmostEqual(price, 101.5)


class PlaceExitOrderTests(unittest.TestCase):
    def test_market_close_used_when_limit_exit_disabled(self):
        c = cfg(limit_exit_enabled=False)
        ex = MagicMock()
        result = place_exit_order(ex, c, "BTCUSDT", "LONG", 1.0)
        ex.close_market_position.assert_called_once_with("BTCUSDT", "LONG", 1.0)
        ex.close_limit_position.assert_not_called()
        self.assertEqual(result, ex.close_market_position.return_value)

    def test_long_exit_uses_ask_limit_price(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.9, "ask": 100.1}
        ex.close_limit_position.return_value = {"orderId": 7}
        ex.get_order_status.return_value = {"status": "FILLED"}

        result = place_exit_order(ex, c, "BTCUSDT", "LONG", 1.0)

        ex.close_limit_position.assert_called_once_with("BTCUSDT", "LONG", 1.0, 100.1)
        ex.close_market_position.assert_not_called()
        self.assertEqual(result, {"orderId": 7})

    def test_partial_fill_falls_back_to_market_for_remaining_qty(self):
        c = cfg()
        ex = MagicMock()
        ex.get_book_ticker.return_value = {"bid": 99.9, "ask": 100.1}
        ex.close_limit_position.return_value = {"orderId": 7}
        ex.close_market_position.return_value = {"orderId": 8}
        times = iter([0.0, 0.1, 2.0])
        with unittest.mock.patch("bot.main.time.time", side_effect=lambda: next(times, 2.0)), \
             unittest.mock.patch("bot.main.time.sleep"):
            ex.get_order_status.return_value = {"status": "PARTIALLY_FILLED", "executedQty": "0.4"}
            result = place_exit_order(ex, c, "BTCUSDT", "LONG", 1.0)

        ex.cancel_regular_order.assert_called_once_with("BTCUSDT", 7)
        ex.close_market_position.assert_called_once_with("BTCUSDT", "LONG", 0.6)
        self.assertEqual(result, {"orderId": 8})
