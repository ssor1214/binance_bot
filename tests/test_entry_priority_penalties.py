import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import (
    LOG_DIR,
    MTF_ZERO_AGREE_EXCEPTION_LOG_PATH,
    _RECENT_MTF_ZERO_AGREE_EXCEPTIONS,
    _RECENT_SHORT_ALIGNMENT_EXCEPTIONS,
    apply_entry_priority_penalties,
    is_long_low_strength_candidate,
    is_short_low_strength_candidate,
    record_mtf_zero_agree_exception_entry,
    required_mtf_agree_ratio,
    should_allow_short_alignment_exception,
    should_allow_zero_agree_mtf_exception,
)
from bot.strategy import timeframe_trend_matches


def cfg(**overrides):
    c = Config()
    c.short_reversal_risk_priority_penalty = 0.08
    c.short_low_strength_floor_threshold = 0.60
    c.short_low_strength_priority_penalty = 0.04
    c.long_low_strength_threshold = 0.60
    c.long_low_strength_priority_penalty = 0.03
    c.chase_entry_priority_penalty = 0.02
    c.btc_momentum_priority_penalty = 0.03
    c.worst_symbol_priority_penalty = 0.05
    c.best_symbol_priority_boost = 0.03
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class EntryPriorityPenaltyTests(unittest.TestCase):
    def test_short_priority_stacks_reversal_and_low_strength_penalties(self):
        c = cfg()
        priority = apply_entry_priority_penalties(
            signal="SHORT",
            combined_score=0.80,
            strength=0.55,
            short_reversal_risk=True,
            same_symbol_reentry=False,
            same_symbol_loss_reentry=False,
            chase_entry=False,
            btc_momentum_opposes=False,
            cfg=c,
            symbol=None,
            worst_priority_symbols=set(),
            best_priority_symbols=set(),
        )
        self.assertAlmostEqual(priority, 0.68)

    def test_long_low_strength_priority_penalty_applies_without_blocking_trade(self):
        c = cfg()
        priority = apply_entry_priority_penalties(
            signal="LONG",
            combined_score=0.81,
            strength=0.58,
            short_reversal_risk=False,
            same_symbol_reentry=False,
            same_symbol_loss_reentry=False,
            chase_entry=False,
            btc_momentum_opposes=False,
            cfg=c,
            symbol=None,
            worst_priority_symbols=set(),
            best_priority_symbols=set(),
        )
        self.assertAlmostEqual(priority, 0.78)

    def test_common_tiebreak_penalties_apply_to_both_directions(self):
        c = cfg()
        long_priority = apply_entry_priority_penalties(
            signal="LONG",
            combined_score=0.90,
            strength=0.72,
            short_reversal_risk=False,
            same_symbol_reentry=False,
            same_symbol_loss_reentry=False,
            chase_entry=True,
            btc_momentum_opposes=True,
            cfg=c,
            symbol=None,
            worst_priority_symbols=set(),
            best_priority_symbols=set(),
        )
        short_priority = apply_entry_priority_penalties(
            signal="SHORT",
            combined_score=0.90,
            strength=0.72,
            short_reversal_risk=False,
            same_symbol_reentry=False,
            same_symbol_loss_reentry=False,
            chase_entry=True,
            btc_momentum_opposes=True,
            cfg=c,
            symbol=None,
            worst_priority_symbols=set(),
            best_priority_symbols=set(),
        )
        self.assertAlmostEqual(long_priority, 0.85)
        self.assertAlmostEqual(short_priority, 0.85)

    def test_same_symbol_reentry_penalties_stack(self):
        c = cfg(
            same_symbol_reentry_priority_penalty=0.05,
            same_symbol_reentry_loss_priority_penalty=0.08,
        )
        priority = apply_entry_priority_penalties(
            signal="SHORT",
            combined_score=0.90,
            strength=0.72,
            short_reversal_risk=False,
            same_symbol_reentry=True,
            same_symbol_loss_reentry=True,
            chase_entry=False,
            btc_momentum_opposes=False,
            cfg=c,
            symbol=None,
            worst_priority_symbols=set(),
            best_priority_symbols=set(),
        )
        self.assertAlmostEqual(priority, 0.77)

    def test_symbol_priority_overrides_adjust_priority_without_blocking_trade(self):
        c = cfg()
        worst_priority = apply_entry_priority_penalties(
            signal="LONG",
            combined_score=0.80,
            strength=0.72,
            short_reversal_risk=False,
            same_symbol_reentry=False,
            same_symbol_loss_reentry=False,
            chase_entry=False,
            btc_momentum_opposes=False,
            cfg=c,
            symbol="BADUSDT",
            worst_priority_symbols={"BADUSDT"},
            best_priority_symbols=set(),
        )
        best_priority = apply_entry_priority_penalties(
            signal="LONG",
            combined_score=0.80,
            strength=0.72,
            short_reversal_risk=False,
            same_symbol_reentry=False,
            same_symbol_loss_reentry=False,
            chase_entry=False,
            btc_momentum_opposes=False,
            cfg=c,
            symbol="GOODUSDT",
            worst_priority_symbols=set(),
            best_priority_symbols={"GOODUSDT"},
        )
        self.assertAlmostEqual(worst_priority, 0.75)
        self.assertAlmostEqual(best_priority, 0.83)

    def test_direction_specific_low_strength_helpers_use_separate_thresholds(self):
        c = cfg(short_low_strength_floor_threshold=0.62, long_low_strength_threshold=0.57)
        self.assertTrue(is_short_low_strength_candidate(0.60, c))
        self.assertFalse(is_long_low_strength_candidate(0.60, c))


class LongLowStrengthSizingWiringTests(unittest.TestCase):
    def test_execute_entry_contains_long_low_strength_size_adjustment(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        idx = src.rindex('if signal == "LONG" and is_long_low_strength_candidate')
        snippet = src[idx:idx + 600]
        self.assertIn('long_low_strength_size_mult', snippet)
        self.assertIn('LONG 신호 강도 낮음', snippet)

    def test_mtf_mismatch_skip_notifies_telegram(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        idx = src.rindex('상위 시간대 추세 불일치(%d/%d) — 이 후보는 건너뜀')
        snippet = src[idx:idx + 350]
        self.assertIn('tg.notify_entry_skipped(', snippet)
        self.assertIn('"상위 시간대 추세 불일치"', snippet)


class MtfZeroAgreeExceptionTests(unittest.TestCase):
    def setUp(self):
        _RECENT_MTF_ZERO_AGREE_EXCEPTIONS.clear()
        _RECENT_SHORT_ALIGNMENT_EXCEPTIONS.clear()

    def test_requires_explicit_enable_and_strict_thresholds(self):
        c = cfg()
        c.mtf_zero_agree_exception_enabled = True
        c.mtf_zero_agree_exception_probability_min = 1.0
        c.mtf_zero_agree_exception_priority_min = 0.88
        c.mtf_zero_agree_exception_max_total = 2
        c.mtf_zero_agree_exception_cooldown_sec = 180.0
        c.mtf_zero_agree_exception_block_chase = True
        c.mtf_zero_agree_exception_block_btc_opposes = True
        candidate = {
            "symbol": "AIOUSDT",
            "probability": 1.0,
            "entry_priority": 0.89,
            "chase_entry": False,
            "btc_momentum_opposes": False,
        }
        self.assertTrue(should_allow_zero_agree_mtf_exception(candidate, 0, 2, c, now_ts=1000.0))

    def test_blocks_repeat_chase_and_btc_opposition_cases(self):
        c = cfg()
        c.mtf_zero_agree_exception_enabled = True
        c.mtf_zero_agree_exception_probability_min = 1.0
        c.mtf_zero_agree_exception_priority_min = 0.88
        c.mtf_zero_agree_exception_max_total = 2
        c.mtf_zero_agree_exception_cooldown_sec = 180.0
        c.mtf_zero_agree_exception_block_chase = True
        c.mtf_zero_agree_exception_block_btc_opposes = True

        chase_candidate = {
            "symbol": "HUSDT",
            "probability": 1.0,
            "entry_priority": 0.93,
            "chase_entry": True,
            "btc_momentum_opposes": False,
        }
        oppose_candidate = {
            "symbol": "CYSUSDT",
            "probability": 1.0,
            "entry_priority": 0.93,
            "chase_entry": False,
            "btc_momentum_opposes": True,
        }
        repeat_candidate = {
            "symbol": "AIOUSDT",
            "probability": 1.0,
            "entry_priority": 0.89,
            "chase_entry": False,
            "btc_momentum_opposes": False,
        }

        self.assertFalse(should_allow_zero_agree_mtf_exception(chase_candidate, 0, 2, c, now_ts=1000.0))
        self.assertFalse(should_allow_zero_agree_mtf_exception(oppose_candidate, 0, 2, c, now_ts=1000.0))
        self.assertTrue(should_allow_zero_agree_mtf_exception(repeat_candidate, 0, 2, c, now_ts=1000.0))
        self.assertFalse(should_allow_zero_agree_mtf_exception(repeat_candidate, 0, 2, c, now_ts=1100.0))
        self.assertTrue(should_allow_zero_agree_mtf_exception(repeat_candidate, 0, 2, c, now_ts=1181.0))

    def test_records_exception_entry_sample_as_jsonl(self):
        import json
        import tempfile

        candidate = {
            "symbol": "AIOUSDT",
            "probability": 1.0,
            "entry_priority": 0.91,
            "score": 0.90,
            "strength": 0.77,
            "price": 0.1234,
            "chase_entry": False,
            "btc_momentum_opposes": False,
            "btc_mult": 1.0,
            "early_entry_spike": False,
        }
        with tempfile.TemporaryDirectory(dir=str(LOG_DIR.parent)) as tmpdir:
            path = Path(tmpdir) / "mtf_zero_agree_exceptions.jsonl"
            record_mtf_zero_agree_exception_entry(candidate, "LONG", 0, 2, path=path)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AIOUSDT")
        self.assertEqual(rows[0]["side"], "LONG")
        self.assertEqual(rows[0]["agree"], 0)
        self.assertEqual(rows[0]["total"], 2)
        self.assertAlmostEqual(rows[0]["probability"], 1.0)


class ShortTimeframeAlignmentTests(unittest.TestCase):
    def test_timeframe_trend_matches_short_when_fast_below_slow(self):
        class Ex:
            def get_klines(self, _symbol, limit=0, interval=None):
                import pandas as pd
                closes = list(range(200, 120, -1))
                return pd.DataFrame({"close": closes})

        c = cfg(ema_fast=5, ema_slow=20)
        self.assertTrue(timeframe_trend_matches(Ex(), c, "ACEUSDT", "SHORT", "15m"))

    def test_short_alignment_exception_requires_strong_clean_candidate(self):
        c = cfg()
        c.short_alignment_exception_enabled = True
        c.short_alignment_exception_probability_min = 0.78
        c.short_alignment_exception_priority_min = 0.78
        c.short_alignment_exception_cooldown_sec = 180.0
        c.short_alignment_exception_block_chase = True
        c.short_alignment_exception_block_btc_opposes = True
        candidate = {
            "symbol": "STORJUSDT",
            "probability": 1.0,
            "entry_priority": 0.82,
            "chase_entry": False,
            "btc_momentum_opposes": False,
        }
        self.assertTrue(should_allow_short_alignment_exception(candidate, c, now_ts=1000.0))
        self.assertFalse(should_allow_short_alignment_exception(candidate, c, now_ts=1100.0))
        self.assertTrue(should_allow_short_alignment_exception(candidate, c, now_ts=1181.0))

    def test_short_alignment_exception_blocks_chase_and_btc_opposition(self):
        c = cfg()
        c.short_alignment_exception_enabled = True
        c.short_alignment_exception_probability_min = 0.78
        c.short_alignment_exception_priority_min = 0.78
        c.short_alignment_exception_cooldown_sec = 180.0
        c.short_alignment_exception_block_chase = True
        c.short_alignment_exception_block_btc_opposes = True
        chase_candidate = {
            "symbol": "UAIUSDT",
            "probability": 0.88,
            "entry_priority": 0.80,
            "chase_entry": True,
            "btc_momentum_opposes": False,
        }
        oppose_candidate = {
            "symbol": "ONGUSDT",
            "probability": 0.78,
            "entry_priority": 0.78,
            "chase_entry": False,
            "btc_momentum_opposes": True,
        }
        weak_candidate = {
            "symbol": "GRASSUSDT",
            "probability": 0.74,
            "entry_priority": 0.73,
            "chase_entry": False,
            "btc_momentum_opposes": False,
        }
        self.assertFalse(should_allow_short_alignment_exception(chase_candidate, c, now_ts=1000.0))
        self.assertFalse(should_allow_short_alignment_exception(oppose_candidate, c, now_ts=1000.0))
        self.assertFalse(should_allow_short_alignment_exception(weak_candidate, c, now_ts=1000.0))


class ShortBiasMtfRatioTests(unittest.TestCase):
    def test_short_bias_mode_uses_direction_specific_ratios(self):
        c = cfg(
            mtf_min_agree_ratio=0.67,
            short_bias_mode_enabled=True,
            short_bias_short_mtf_min_agree_ratio=0.34,
            short_bias_long_mtf_min_agree_ratio=1.0,
        )
        self.assertAlmostEqual(required_mtf_agree_ratio(c, "SHORT"), 0.34)
        self.assertAlmostEqual(required_mtf_agree_ratio(c, "LONG"), 1.0)
        self.assertAlmostEqual(required_mtf_agree_ratio(c, "long"), 1.0)


class ShortProbabilityRelaxationWiringTests(unittest.TestCase):
    def test_scan_entry_candidate_uses_short_probability_relaxation(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn('short_probability_relaxation', src)
        self.assertIn('cfg.short_min_entry_probability', src)


if __name__ == "__main__":
    unittest.main()
