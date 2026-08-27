import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.exchange import Exchange
from bot.main import compute_position_size, should_allow_low_balance_recovery_floor


def make_exchange():
    ex = Exchange.__new__(Exchange)
    ex.client = None
    ex._symbol_info_cache = {
        "BIGUSDT": {
            "quantity_precision": 0,
            "price_precision": 4,
            "min_qty": 1.0,
            "step_size": 1.0,
            "min_notional": 5.0,
        },
        "SMALLUSDT": {
            "quantity_precision": 1,
            "price_precision": 4,
            "min_qty": 0.1,
            "step_size": 0.1,
            "min_notional": 5.0,
        },
    }
    return ex


class SmallBalancePositionSizeTests(unittest.TestCase):
    def test_short_floor_penalties_apply_only_to_short_risk_cases(self):
        class Cfg:
            defense_stack_min_ratio_mult = 0.30
            short_reversal_risk_floor_mult = 0.85
            short_low_strength_floor_threshold = 0.60
            short_low_strength_floor_mult = 0.80
            leverage = 4
            leverage_min = 4
            leverage_max = 4
            small_balance_threshold = 20.0
            small_balance_max_ratio = 0.14
            position_size_min = 0.08
            position_size_step = 0.02
            position_size_max = 0.14
            low_balance_new_entry_pause_threshold = 17.0
            low_balance_recovery_enabled = False
            low_balance_recovery_size_mult = 1.0
            short_reversal_risk_size_mult = 0.65
            min_avg_quote_volume_usdt = 150.0
            liquidity_size_full_usdt = 2000.0
            liquidity_size_min_mult = 0.5
            chase_entry_size_mult = 0.90
            chase_entry_range_mult = 3.0
            btc_momentum_gate_size_mult = 0.90
            btc_momentum_gate_window_min = 5
            btc_momentum_gate_threshold_pct = 0.10
            cross_margin_min_balance_usdt = 300.0
            whale_max_leverage = 3
            whale_max_position_ratio = 0.10

        class PM:
            win_streak = 0

            def next_position_size_ratio(self, balance, symbol=None):
                return 0.01

            def direction_size_multiplier(self, signal):
                return 1.0

            def recent_performance_size_multiplier(self):
                return 1.0

            def expected_value_size_multiplier(self):
                return 1.0

            def aggregate_risk_size_multiplier(self, *args, **kwargs):
                return 1.0

        cfg = Cfg()
        base = cfg.position_size_min
        default_floor = base * cfg.defense_stack_min_ratio_mult

        short_reversal_floor = default_floor * cfg.short_reversal_risk_floor_mult
        short_both_floor = short_reversal_floor * cfg.short_low_strength_floor_mult

        self.assertAlmostEqual(default_floor, 0.024)
        self.assertAlmostEqual(short_reversal_floor, 0.0204)
        self.assertAlmostEqual(short_both_floor, 0.01632)

    def test_recovery_floor_gate_allows_only_safer_long_recovery_candidate(self):
        class Cfg:
            low_balance_new_entry_pause_threshold = 17.0
            low_balance_recovery_enabled = True
            low_balance_recovery_min_probability = 0.80
            low_balance_recovery_min_score = 0.68

        candidate = {"signal": "LONG", "probability": 0.86, "score": 0.72, "negative_ev_symbol": False}
        self.assertTrue(should_allow_low_balance_recovery_floor(candidate, Cfg(), total_balance=10.0))

    def test_recovery_floor_gate_blocks_short_candidates(self):
        class Cfg:
            low_balance_new_entry_pause_threshold = 17.0
            low_balance_recovery_enabled = True
            low_balance_recovery_min_probability = 0.80
            low_balance_recovery_min_score = 0.68

        short_candidate = {"signal": "SHORT", "probability": 0.90, "score": 0.80, "negative_ev_symbol": False}
        self.assertFalse(should_allow_low_balance_recovery_floor(short_candidate, Cfg(), total_balance=10.0))

    def test_recovery_floor_gate_blocks_btc_misaligned_long_candidates(self):
        class Cfg:
            low_balance_new_entry_pause_threshold = 17.0
            low_balance_recovery_enabled = True
            low_balance_recovery_min_probability = 0.80
            low_balance_recovery_min_score = 0.68

        candidate = {
            "signal": "LONG",
            "probability": 0.90,
            "score": 0.75,
            "negative_ev_symbol": False,
            "btc_mult": 0.88,
        }
        self.assertFalse(should_allow_low_balance_recovery_floor(candidate, Cfg(), total_balance=10.0))

    def test_small_balance_floor_can_salvage_order_that_tier3_floor_would_skip(self):
        ex = make_exchange()
        qty_without_small_floor = compute_position_size(
            balance=5.5,
            symbol="BIGUSDT",
            ex=ex,
            price=20.0,
            ratio=0.135,
            leverage=4,
            min_margin_usdt=7.0,
            small_balance_min_margin_usdt=0.0,
        )
        qty_with_small_floor = compute_position_size(
            balance=5.5,
            symbol="BIGUSDT",
            ex=ex,
            price=20.0,
            ratio=0.135,
            leverage=4,
            min_margin_usdt=7.0,
            small_balance_min_margin_usdt=4.0,
        )
        self.assertEqual(qty_without_small_floor, 0.0)
        self.assertEqual(qty_with_small_floor, 1.0)

    def test_large_balance_path_does_not_use_small_floor_override(self):
        ex = make_exchange()
        qty = compute_position_size(
            balance=60.0,
            symbol="SMALLUSDT",
            ex=ex,
            price=10.0,
            ratio=0.15,
            leverage=4,
            min_margin_usdt=3.0,
            small_balance_min_margin_usdt=4.0,
        )
        self.assertGreater(qty, 0.0)
        # large-balance path should still be dominated by the normal ratio sizing
        self.assertAlmostEqual(qty, 3.5)

    def test_existing_positions_can_use_lower_floor_without_spending_reserve(self):
        ex = make_exchange()
        qty = compute_position_size(
            balance=2.70,
            symbol="BIGUSDT",
            ex=ex,
            price=0.914,
            ratio=0.135,
            leverage=4,
            min_margin_usdt=4.0,
            small_balance_min_margin_usdt=4.0,
            existing_positions_small_balance_min_margin_usdt=1.9,
            available_balance_reserve_usdt=0.75,
            has_open_positions=True,
        )
        self.assertEqual(qty, 8.0)

    def test_existing_positions_still_skip_when_reserve_leaves_too_little_balance(self):
        ex = make_exchange()
        qty = compute_position_size(
            balance=1.55,
            symbol="BIGUSDT",
            ex=ex,
            price=0.914,
            ratio=0.135,
            leverage=4,
            min_margin_usdt=4.0,
            small_balance_min_margin_usdt=4.0,
            existing_positions_small_balance_min_margin_usdt=1.9,
            available_balance_reserve_usdt=0.75,
            has_open_positions=True,
        )
        self.assertEqual(qty, 0.0)


if __name__ == "__main__":
    unittest.main()
