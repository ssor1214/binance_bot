"""Unit tests for risk-based sizing.

These tests avoid any real API calls and only validate quantity calculation
and the opt-in default behavior.
"""

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.exchange import Exchange
from bot.main import compute_risk_based_position_size


def make_exchange():
    ex = Exchange.__new__(Exchange)
    ex.client = None
    ex._symbol_info_cache = {
        "TESTUSDT": {
            "quantity_precision": 1,
            "price_precision": 4,
            "min_qty": 0.1,
            "step_size": 0.1,
            "min_notional": 5.0,
        }
    }
    return ex


class RiskBasedSizingTests(unittest.TestCase):
    def test_tighter_stop_distance_yields_larger_quantity(self):
        ex = make_exchange()
        qty_tight_stop = compute_risk_based_position_size(
            balance=100.0,
            symbol="TESTUSDT",
            ex=ex,
            price=10.0,
            stop_price=9.9,
            risk_pct_of_balance=0.01,
            leverage=4,
            min_margin_usdt=1.0,
        )
        qty_wide_stop = compute_risk_based_position_size(
            balance=100.0,
            symbol="TESTUSDT",
            ex=ex,
            price=10.0,
            stop_price=9.0,
            risk_pct_of_balance=0.01,
            leverage=4,
            min_margin_usdt=1.0,
        )
        self.assertGreater(qty_tight_stop, qty_wide_stop)

    def test_risk_amount_is_respected_within_leverage_cap(self):
        ex = make_exchange()
        balance = 100.0
        risk_pct = 0.02
        price = 10.0
        stop_price = 9.5
        qty = compute_risk_based_position_size(
            balance=balance,
            symbol="TESTUSDT",
            ex=ex,
            price=price,
            stop_price=stop_price,
            risk_pct_of_balance=risk_pct,
            leverage=4,
            min_margin_usdt=1.0,
        )
        actual_loss_if_stopped = qty * (price - stop_price)
        expected_risk = balance * risk_pct
        self.assertAlmostEqual(actual_loss_if_stopped, expected_risk, delta=expected_risk * 0.1)

    def test_zero_distance_returns_zero(self):
        ex = make_exchange()
        qty = compute_risk_based_position_size(
            balance=100.0,
            symbol="TESTUSDT",
            ex=ex,
            price=10.0,
            stop_price=10.0,
            risk_pct_of_balance=0.01,
            leverage=4,
            min_margin_usdt=1.0,
        )
        self.assertEqual(qty, 0.0)

    def test_capped_by_leverage_when_risk_calc_exceeds_max_notional(self):
        ex = make_exchange()
        qty = compute_risk_based_position_size(
            balance=100.0,
            symbol="TESTUSDT",
            ex=ex,
            price=10.0,
            stop_price=9.99,
            risk_pct_of_balance=0.5,
            leverage=4,
            min_margin_usdt=1.0,
        )
        max_notional = 100.0 * 4
        self.assertLessEqual(qty * 10.0, max_notional + 1e-6)

    def test_small_balance_floor_applies_only_below_50_usdt(self):
        ex = make_exchange()
        qty = compute_risk_based_position_size(
            balance=28.0,
            symbol="TESTUSDT",
            ex=ex,
            price=10.0,
            stop_price=9.85,
            risk_pct_of_balance=0.00039,
            leverage=4,
            min_margin_usdt=8.0,
            small_balance_min_margin_usdt=4.0,
        )
        self.assertEqual(qty, 1.6)
        self.assertAlmostEqual(qty * 10.0 / 4, 4.0)
        self.assertGreater(qty * 10.0 / 4, 1.25)

    def test_large_balance_keeps_floor_disabled(self):
        ex = make_exchange()
        qty = compute_risk_based_position_size(
            balance=100.0,
            symbol="TESTUSDT",
            ex=ex,
            price=10.0,
            stop_price=9.85,
            risk_pct_of_balance=0.00039,
            leverage=4,
            min_margin_usdt=8.0,
            small_balance_min_margin_usdt=4.0,
        )
        self.assertEqual(qty, 0.5)
        self.assertAlmostEqual(qty * 10.0 / 4, 1.25)


class RiskBasedSizingOptInDefaultTests(unittest.TestCase):
    def test_bool_helper_defaults_to_false_when_env_var_absent(self):
        from bot.config import _bool
        import os

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RISK_BASED_SIZING_ENABLED_TEST_ONLY_UNSET", None)
            self.assertFalse(_bool("RISK_BASED_SIZING_ENABLED_TEST_ONLY_UNSET", "false"))

    def test_config_field_declared_with_false_fallback_string(self):
        source = inspect.getsource(Config)
        self.assertIn('_bool("RISK_BASED_SIZING_ENABLED", "false")', source)


if __name__ == "__main__":
    unittest.main()
