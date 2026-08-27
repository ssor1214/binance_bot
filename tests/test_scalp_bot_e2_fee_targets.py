import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.scalp_bot_e2 import (
    Pos,
    early_cut_reason,
    fee_aware_bb_price,
    fee_aware_rr_price,
)


class E2FeeAwareTargetsTests(unittest.TestCase):
    def test_long_rr_target_includes_entry_exit_fees_and_loss_fee(self):
        # entry 100, stop 99 => gross stop risk 1%.
        # rr=2, roundtrip fee=0.1% => gross TP = 2*1% + 3*0.1% = 2.3%.
        self.assertAlmostEqual(
            fee_aware_rr_price(100.0, 99.0, "LONG", 2.0, 0.001),
            102.3,
            places=8,
        )

    def test_short_rr_target_includes_entry_exit_fees_and_loss_fee(self):
        self.assertAlmostEqual(
            fee_aware_rr_price(100.0, 101.0, "SHORT", 2.0, 0.001),
            97.7,
            places=8,
        )

    def test_bb_target_is_disabled_when_fee_adjusted_net_is_too_small(self):
        self.assertEqual(
            fee_aware_bb_price(100.0, 100.05, "LONG", 0.001, 0.0002),
            0.0,
        )

    def test_bb_target_is_kept_when_fee_adjusted_net_is_positive_enough(self):
        self.assertEqual(
            fee_aware_bb_price(100.0, 100.2, "LONG", 0.001, 0.0002),
            100.2,
        )


class E2EarlyCutTests(unittest.TestCase):
    def _pos(self, entered_at=1_000.0, fav=0.0, adv=0.0):
        return Pos(
            symbol="TESTUSDT",
            side="LONG",
            legs=[100.0],
            qty=1.0,
            entered_at=entered_at,
            leverage=2,
            max_favorable_roe=fav,
            max_adverse_roe=adv,
        )

    def test_early_adverse_triggers_inside_entry_window(self):
        pos = self._pos(fav=0.0, adv=-1.6)
        self.assertEqual(
            early_cut_reason(pos, -1.6, 1_120.0, 180, 1.5, 0.5, 3.0, 180, 1.0),
            "EARLY_ADVERSE",
        )

    def test_early_adverse_does_not_trigger_after_window(self):
        pos = self._pos(fav=0.0, adv=-1.6)
        self.assertIsNone(
            early_cut_reason(pos, -1.6, 1_240.0, 180, 1.5, 0.5, 3.0, 180, 1.0)
        )

    def test_early_adverse_does_not_cut_after_meaningful_favorable_move(self):
        pos = self._pos(fav=0.6, adv=-1.6)
        self.assertIsNone(
            early_cut_reason(pos, -1.6, 1_120.0, 180, 1.5, 0.5, 3.0, 180, 1.0)
        )

    def test_mae_cut_triggers_after_grace_when_trade_never_worked(self):
        pos = self._pos(fav=0.2, adv=-3.4)
        self.assertEqual(
            early_cut_reason(pos, -3.2, 1_240.0, 180, 1.5, 0.5, 3.0, 180, 1.0),
            "MAE_CUT",
        )

    def test_mae_cut_does_not_trigger_before_grace(self):
        pos = self._pos(fav=0.2, adv=-3.4)
        self.assertEqual(
            early_cut_reason(pos, -3.2, 1_120.0, 180, 1.5, 0.5, 3.0, 180, 1.0),
            "EARLY_ADVERSE",
        )

    def test_mae_cut_does_not_cut_after_meaningful_favorable_move(self):
        pos = self._pos(fav=1.2, adv=-3.4)
        self.assertIsNone(
            early_cut_reason(pos, -3.2, 1_240.0, 180, 1.5, 0.5, 3.0, 180, 1.0)
        )

    def test_zero_thresholds_disable_both_cuts(self):
        pos = self._pos(fav=0.0, adv=-10.0)
        self.assertIsNone(
            early_cut_reason(pos, -10.0, 1_240.0, 0, 0, 0.5, 0, 180, 1.0)
        )


if __name__ == "__main__":
    unittest.main()
