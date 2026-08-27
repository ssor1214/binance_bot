"""[2026-08-25] RSI 방향일치를 차단이 아니라 우선순위 점수로 반영.

캐시 재생(2,471 신호)에서 볼밴+EMA+RSI(방향일치)가 15분 후 선행수익률 +0.446%,
승률 52.2%로 가장 좋았다(전체 +0.057%, 47.0%). 다만 차단으로 걸면 whipsaw 통과분의
37.7%만 남아 거래수가 62% 줄어 원칙 1을 깬다. 그래서 순위에만 반영한다.
"""
import unittest

import pandas as pd

from bot.config import Config
from bot.main import apply_entry_priority_penalties, rsi_direction_aligned


def _df(rsi_prev, rsi_now):
    return pd.DataFrame([{"rsi": rsi_prev}, {"rsi": rsi_now}])


def _cfg(bonus=0.03):
    cfg = Config()
    cfg.rsi_alignment_priority_bonus = bonus
    cfg.rsi_alignment_overbought = 70.0
    cfg.rsi_alignment_oversold = 30.0
    for k in (
        "worst_symbol_priority_penalty", "best_symbol_priority_boost",
        "short_reversal_risk_priority_penalty", "same_symbol_reentry_priority_penalty",
        "same_symbol_reentry_loss_priority_penalty", "short_low_strength_priority_penalty",
        "long_low_strength_priority_penalty", "chase_entry_priority_penalty",
        "btc_momentum_priority_penalty", "bb_participation_priority_bonus",
    ):
        setattr(cfg, k, 0.0)
    cfg.short_low_strength_floor_threshold = 0.0
    cfg.long_low_strength_threshold = 0.0
    return cfg


class RsiDirectionAlignedTests(unittest.TestCase):
    def test_long_rising_below_overbought_is_aligned(self):
        self.assertTrue(rsi_direction_aligned(_df(52.0, 55.0), _cfg(), "LONG"))

    def test_long_in_overbought_is_not_aligned(self):
        """과매수에서 롱은 제외 — 캐시 재생에서 이 조건이 15분 수익률을 7배 갈랐다."""
        self.assertFalse(rsi_direction_aligned(_df(68.0, 72.0), _cfg(), "LONG"))

    def test_long_falling_rsi_is_not_aligned(self):
        self.assertFalse(rsi_direction_aligned(_df(58.0, 55.0), _cfg(), "LONG"))

    def test_short_falling_above_oversold_is_aligned(self):
        self.assertTrue(rsi_direction_aligned(_df(48.0, 45.0), _cfg(), "SHORT"))

    def test_short_in_oversold_is_not_aligned(self):
        self.assertFalse(rsi_direction_aligned(_df(32.0, 28.0), _cfg(), "SHORT"))

    def test_missing_data_returns_none(self):
        """정보가 없으면 None — 가감을 건너뛴다(중립). 차단하지 않는다."""
        self.assertIsNone(rsi_direction_aligned(pd.DataFrame([{"rsi": 50.0}]), _cfg(), "LONG"))
        self.assertIsNone(rsi_direction_aligned(pd.DataFrame(), _cfg(), "LONG"))


class RsiPriorityScoreTests(unittest.TestCase):
    def _run(self, aligned, bonus=0.03):
        return apply_entry_priority_penalties(
            signal="LONG", combined_score=1.0, strength=1.0,
            short_reversal_risk=False, same_symbol_reentry=False,
            same_symbol_loss_reentry=False, chase_entry=False,
            btc_momentum_opposes=False, cfg=_cfg(bonus), rsi_aligned=aligned,
        )

    def test_aligned_ranks_higher(self):
        self.assertAlmostEqual(self._run(True), 1.03, places=6)

    def test_misaligned_ranks_lower_but_is_not_blocked(self):
        """핵심 — RSI가 안 맞아도 후보에서 탈락하지 않는다(원칙 1). 순위만 내려간다."""
        self.assertAlmostEqual(self._run(False), 0.97, places=6)

    def test_none_and_zero_are_noops(self):
        self.assertAlmostEqual(self._run(None), 1.0, places=6)
        self.assertAlmostEqual(self._run(True, bonus=0.0), 1.0, places=6)

    def test_code_default_is_off(self):
        import inspect
        self.assertIn(
            'rsi_alignment_priority_bonus: float = _float("RSI_ALIGNMENT_PRIORITY_BONUS", 0.0)',
            inspect.getsource(Config),
        )


if __name__ == "__main__":
    unittest.main()
