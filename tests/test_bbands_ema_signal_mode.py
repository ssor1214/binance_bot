import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.strategy import _direction_scores, _signal_component_snapshot, estimate_entry_probability, generate_frequency_signal_with_probability, generate_signal_with_probability


def _cfg(**overrides):
    c = Config()
    c.min_confirmations = 2
    c.probability_adx_cap = 25.0
    c.ema_slope_lookback = 2
    c.ema_gap_min_pct = 0.10
    c.bb_width_expansion_ratio = 1.01
    c.bb_breakout_lookback = 1
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _expand_rows(rows, total=80):
    seed = rows[0]
    filler = [dict(seed) for _ in range(max(0, total - len(rows)))]
    return pd.DataFrame(filler + rows)


class BbandsEmaSignalModeTests(unittest.TestCase):
    def test_long_signal_requires_ema_trend_and_bband_reclaim(self):
        df = _expand_rows(
            [
                {
                    "open": 99.6,
                    "high": 100.1,
                    "low": 99.2,
                    "close": 99.9,
                    "ema_fast": 99.4,
                    "ema_slow": 99.0,
                    "bb_mid": 99.5,
                    "bb_high": 100.4,
                    "bb_low": 97.0,
                    "adx": 25.0,
                },
                {
                    "open": 99.8,
                    "high": 100.2,
                    "low": 99.4,
                    "close": 100.0,
                    "ema_fast": 99.6,
                    "ema_slow": 99.1,
                    "bb_mid": 100.0,
                    "bb_high": 100.5,
                    "bb_low": 98.0,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 101.4,
                    "low": 100.1,
                    "close": 101.0,
                    "ema_fast": 100.3,
                    "ema_slow": 99.4,
                    "bb_mid": 100.0,
                    "bb_high": 101.0,
                    "bb_low": 97.8,
                    "adx": 25.0,
                },
            ]
        )
        long_score, short_score, _ = _direction_scores(df, _cfg())
        self.assertEqual(long_score, 2.75)
        self.assertEqual(short_score, 0)
        signal, probability = generate_signal_with_probability(df, _cfg(), min_confirmations=2)
        self.assertEqual(signal, "LONG")
        self.assertAlmostEqual(probability, 1.0)

    def test_short_signal_requires_ema_trend_and_bband_reject(self):
        df = _expand_rows(
            [
                {
                    "open": 100.5,
                    "high": 100.8,
                    "low": 99.8,
                    "close": 100.1,
                    "ema_fast": 100.7,
                    "ema_slow": 101.0,
                    "bb_mid": 100.0,
                    "bb_high": 102.0,
                    "bb_low": 99.0,
                    "adx": 25.0,
                },
                {
                    "open": 100.2,
                    "high": 100.0,
                    "low": 99.4,
                    "close": 99.8,
                    "ema_fast": 100.4,
                    "ema_slow": 100.8,
                    "bb_mid": 99.7,
                    "bb_high": 101.7,
                    "bb_low": 98.6,
                    "adx": 25.0,
                },
                {
                    "open": 99.7,
                    "high": 99.6,
                    "low": 98.4,
                    "close": 98.9,
                    "ema_fast": 99.4,
                    "ema_slow": 100.3,
                    "bb_mid": 99.7,
                    "bb_high": 101.5,
                    "bb_low": 98.1,
                    "adx": 25.0,
                },
            ]
        )
        long_score, short_score, _ = _direction_scores(df, _cfg())
        self.assertEqual(long_score, 0)
        self.assertEqual(short_score, 2.25)
        signal, probability = generate_signal_with_probability(df, _cfg(), min_confirmations=2)
        self.assertEqual(signal, "SHORT")
        self.assertAlmostEqual(probability, 1.0)

    def test_probability_scales_with_two_point_signal_model(self):
        self.assertAlmostEqual(estimate_entry_probability(0, 0.0), 0.0)
        self.assertAlmostEqual(estimate_entry_probability(1, 0.0), 0.3)
        self.assertAlmostEqual(estimate_entry_probability(2, 25.0), 1.0)

    def test_no_signal_when_band_width_is_not_expanding(self):
        df = _expand_rows(
            [
                {
                    "open": 99.6,
                    "high": 100.1,
                    "low": 99.2,
                    "close": 99.9,
                    "ema_fast": 99.4,
                    "ema_slow": 99.0,
                    "bb_mid": 99.5,
                    "bb_high": 101.4,
                    "bb_low": 97.6,
                    "adx": 25.0,
                },
                {
                    "open": 99.8,
                    "high": 100.2,
                    "low": 99.4,
                    "close": 100.0,
                    "ema_fast": 99.6,
                    "ema_slow": 99.1,
                    "bb_mid": 100.0,
                    "bb_high": 101.5,
                    "bb_low": 97.5,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 101.1,
                    "low": 100.1,
                    "close": 100.8,
                    "ema_fast": 100.3,
                    "ema_slow": 99.4,
                    "bb_mid": 100.0,
                    "bb_high": 101.3,
                    "bb_low": 97.7,
                    "adx": 25.0,
                },
            ]
        )
        long_score, short_score, _ = _direction_scores(df, _cfg())
        self.assertEqual(long_score, 1.75)
        self.assertEqual(short_score, 0)

    def test_soft_width_score_rescues_raw_mid_reclaim_without_full_expansion(self):
        df = _expand_rows(
            [
                {
                    "open": 99.6,
                    "high": 100.1,
                    "low": 99.2,
                    "close": 99.9,
                    "ema_fast": 99.4,
                    "ema_slow": 99.0,
                    "bb_mid": 99.5,
                    "bb_high": 101.4,
                    "bb_low": 97.6,
                    "adx": 25.0,
                },
                {
                    "open": 99.8,
                    "high": 100.2,
                    "low": 99.4,
                    "close": 100.0,
                    "ema_fast": 99.6,
                    "ema_slow": 99.1,
                    "bb_mid": 100.0,
                    "bb_high": 101.5,
                    "bb_low": 97.5,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 101.1,
                    "low": 100.1,
                    "close": 100.8,
                    "ema_fast": 100.3,
                    "ema_slow": 99.4,
                    "bb_mid": 100.0,
                    "bb_high": 101.3,
                    "bb_low": 97.7,
                    "adx": 25.0,
                },
            ]
        )
        signal, probability = generate_signal_with_probability(df, _cfg(), min_confirmations=2)
        self.assertEqual(signal, None)
        self.assertAlmostEqual(probability, 0.0)
        long_score, _, _ = _direction_scores(df, _cfg(bb_width_soft_score=1.0))
        self.assertEqual(long_score, 2.0)

    def test_no_trend_score_when_ema_gap_is_too_narrow(self):
        df = _expand_rows(
            [
                {
                    "open": 100.0,
                    "high": 100.4,
                    "low": 99.8,
                    "close": 100.0,
                    "ema_fast": 99.95,
                    "ema_slow": 99.92,
                    "bb_mid": 99.9,
                    "bb_high": 100.5,
                    "bb_low": 99.2,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.9,
                    "close": 100.1,
                    "ema_fast": 99.97,
                    "ema_slow": 99.93,
                    "bb_mid": 100.0,
                    "bb_high": 100.6,
                    "bb_low": 99.3,
                    "adx": 25.0,
                },
                {
                    "open": 100.1,
                    "high": 101.0,
                    "low": 100.2,
                    "close": 100.8,
                    "ema_fast": 100.08,
                    "ema_slow": 99.99,
                    "bb_mid": 100.0,
                    "bb_high": 100.8,
                    "bb_low": 99.4,
                    "adx": 25.0,
                },
            ]
        )
        long_score, short_score, _ = _direction_scores(df, _cfg())
        self.assertEqual(long_score, 1.5)  # trend 0 + reclaim 1 + breakout 0.5
        self.assertEqual(short_score, 0)

    def test_signal_snapshot_reports_missing_components(self):
        df = _expand_rows(
            [
                {
                    "open": 100.0,
                    "high": 100.4,
                    "low": 99.8,
                    "close": 100.0,
                    "ema_fast": 99.95,
                    "ema_slow": 99.92,
                    "bb_mid": 99.9,
                    "bb_high": 100.5,
                    "bb_low": 99.2,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.9,
                    "close": 100.1,
                    "ema_fast": 99.97,
                    "ema_slow": 99.93,
                    "bb_mid": 100.0,
                    "bb_high": 100.6,
                    "bb_low": 99.3,
                    "adx": 25.0,
                },
                {
                    "open": 100.1,
                    "high": 101.0,
                    "low": 100.2,
                    "close": 100.8,
                    "ema_fast": 100.08,
                    "ema_slow": 99.99,
                    "bb_mid": 100.0,
                    "bb_high": 100.8,
                    "bb_low": 99.4,
                    "adx": 25.0,
                },
            ]
        )
        snapshot = _signal_component_snapshot(df, _cfg())
        self.assertFalse(snapshot["ema_gap_ok"])
        self.assertTrue(snapshot["long_band_break"])
        self.assertFalse(snapshot["long_trend"])
        self.assertTrue(snapshot["long_trend_soft"])
        self.assertTrue(snapshot["long_band_break_raw"])

    def test_frequency_signal_rescues_near_miss_gap_case(self):
        df = _expand_rows(
            [
                {
                    "open": 100.0,
                    "high": 100.4,
                    "low": 99.8,
                    "close": 100.0,
                    "ema_fast": 99.95,
                    "ema_slow": 99.92,
                    "bb_mid": 99.9,
                    "bb_high": 100.5,
                    "bb_low": 99.2,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.9,
                    "close": 100.1,
                    "ema_fast": 99.97,
                    "ema_slow": 99.93,
                    "bb_mid": 100.0,
                    "bb_high": 100.6,
                    "bb_low": 99.3,
                    "adx": 25.0,
                },
                {
                    "open": 100.1,
                    "high": 101.0,
                    "low": 100.2,
                    "close": 100.8,
                    "ema_fast": 100.08,
                    "ema_slow": 99.99,
                    "bb_mid": 100.0,
                    "bb_high": 100.8,
                    "bb_low": 99.4,
                    "adx": 25.0,
                },
            ]
        )
        signal, probability, detail = generate_frequency_signal_with_probability(df, _cfg(), min_confirmations=2)
        self.assertEqual(signal, "LONG")
        self.assertAlmostEqual(probability, 0.85)
        self.assertEqual(detail["direction"], "LONG")

    def test_lower_ema_gap_threshold_restores_trend_score(self):
        df = _expand_rows(
            [
                {
                    "open": 100.0,
                    "high": 100.4,
                    "low": 99.8,
                    "close": 100.0,
                    "ema_fast": 99.95,
                    "ema_slow": 99.92,
                    "bb_mid": 99.9,
                    "bb_high": 100.5,
                    "bb_low": 99.2,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.9,
                    "close": 100.1,
                    "ema_fast": 99.97,
                    "ema_slow": 99.93,
                    "bb_mid": 100.0,
                    "bb_high": 100.6,
                    "bb_low": 99.3,
                    "adx": 25.0,
                },
                {
                    "open": 100.1,
                    "high": 101.0,
                    "low": 100.2,
                    "close": 100.8,
                    "ema_fast": 100.08,
                    "ema_slow": 99.99,
                    "bb_mid": 100.0,
                    "bb_high": 100.8,
                    "bb_low": 99.4,
                    "adx": 25.0,
                },
            ]
        )
        long_score, short_score, _ = _direction_scores(df, _cfg(ema_gap_min_pct=0.08))
        self.assertEqual(long_score, 2.75)
        self.assertEqual(short_score, 0)

    def test_small_normalized_slope_mismatch_can_pass_with_tolerance(self):
        df = _expand_rows(
            [
                {
                    "open": 99.8,
                    "high": 100.1,
                    "low": 99.6,
                    "close": 100.0,
                    "ema_fast": 100.0,
                    "ema_slow": 99.97,
                    "bb_mid": 99.8,
                    "bb_high": 100.6,
                    "bb_low": 99.2,
                    "adx": 25.0,
                },
                {
                    "open": 100.0,
                    "high": 100.3,
                    "low": 99.9,
                    "close": 100.1,
                    "ema_fast": 100.10,
                    "ema_slow": 100.10,
                    "bb_mid": 99.9,
                    "bb_high": 100.7,
                    "bb_low": 99.3,
                    "adx": 25.0,
                },
                {
                    "open": 100.1,
                    "high": 100.6,
                    "low": 100.0,
                    "close": 100.2,
                    "ema_fast": 100.22,
                    "ema_slow": 100.21,
                    "bb_mid": 100.0,
                    "bb_high": 100.8,
                    "bb_low": 99.4,
                    "adx": 25.0,
                },
            ]
        )
        strict = _cfg(ema_gap_min_pct=0.0, ema_slope_relation_tolerance_pct=0.0)
        relaxed = _cfg(ema_gap_min_pct=0.0, ema_slope_relation_tolerance_pct=0.02)
        self.assertFalse(_signal_component_snapshot(df, strict)["long_trend"])
        self.assertTrue(_signal_component_snapshot(df, relaxed)["long_trend"])


if __name__ == "__main__":
    unittest.main()
