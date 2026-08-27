"""[2026-08-25 원칙0 정합] 볼밴 관여 게이트 테스트.

원칙 0은 "볼밴 매매 + EMA(3분봉)"인데, 점수제에서 EMA 추세만으로 1.0점이 나오고
운영값 MIN_SIGNAL_CONFIRMATIONS=1이라 볼밴이 하나도 관여하지 않은 순수 EMA 진입이
통과하고 있었다. 이 게이트가 그 경로만 막는지 확인한다.
"""
import unittest

from bot.strategy import bb_participates


def _snap(**kw):
    base = {
        "long_mid_reclaim_raw": False,
        "long_band_break_raw": False,
        "short_mid_reject_raw": False,
        "short_band_break_raw": False,
        "width_expanding": False,
    }
    base.update(kw)
    return base


class BbParticipationTests(unittest.TestCase):
    def test_pure_ema_is_rejected(self):
        """볼밴 근거가 하나도 없으면 관여로 보지 않는다 — 이게 막으려던 경로다."""
        self.assertFalse(bb_participates(_snap(), "LONG"))
        self.assertFalse(bb_participates(_snap(), "SHORT"))

    def test_mid_reclaim_counts(self):
        self.assertTrue(bb_participates(_snap(long_mid_reclaim_raw=True), "LONG"))
        self.assertTrue(bb_participates(_snap(short_mid_reject_raw=True), "SHORT"))

    def test_band_break_counts(self):
        self.assertTrue(bb_participates(_snap(long_band_break_raw=True), "LONG"))
        self.assertTrue(bb_participates(_snap(short_band_break_raw=True), "SHORT"))

    def test_width_expansion_counts(self):
        """밴드폭 확장은 가격 위치 이벤트는 아니지만 볼밴 지표에서 나오는 판단 근거다.
        이벤트만 필수로 걸면 신호가 16~18%까지 떨어져 원칙 1을 깨기 때문에 여기까지 인정한다."""
        self.assertTrue(bb_participates(_snap(width_expanding=True), "LONG"))
        self.assertTrue(bb_participates(_snap(width_expanding=True), "SHORT"))

    def test_side_isolation(self):
        """반대편 볼밴 이벤트를 내 근거로 인정하면 안 된다."""
        self.assertFalse(bb_participates(_snap(short_band_break_raw=True), "LONG"))
        self.assertFalse(bb_participates(_snap(long_band_break_raw=True), "SHORT"))

    def test_code_default_is_off(self):
        """코드 기본값은 꺼짐 — 켜는 건 운영 .env의 명시적 결정이어야 한다(원복 경로 보존)."""
        import inspect

        from bot.config import Config

        self.assertIn(
            'bb_participation_required: bool = _bool("BB_PARTICIPATION_REQUIRED", "false")',
            inspect.getsource(Config),
        )


if __name__ == "__main__":
    unittest.main()


class BbParticipationAsScoreTests(unittest.TestCase):
    """[2026-08-25] 하드 게이트 -> 우선순위 점수로 전환.

    게이트로 막으면 신호의 33.9%가 통째로 잘리는데(원칙 1 손해), 원장 실측은 오히려
    bb_event=True 쪽이 더 나빴다(17건 건당 -0.1829 vs False 15건 -0.1360).
    검증 안 된 가정으로 거래를 없애는 대신, 순위에만 반영해 표본을 쌓는다.
    """

    @staticmethod
    def _cfg(bonus=0.03):
        from bot.config import Config

        cfg = Config()
        cfg.bb_participation_priority_bonus = bonus
        # 다른 가감치는 이 테스트에서 개입하지 않도록 0으로
        for k in (
            "worst_symbol_priority_penalty", "best_symbol_priority_boost",
            "short_reversal_risk_priority_penalty", "same_symbol_reentry_priority_penalty",
            "same_symbol_reentry_loss_priority_penalty", "short_low_strength_priority_penalty",
            "long_low_strength_priority_penalty", "chase_entry_priority_penalty",
            "btc_momentum_priority_penalty",
        ):
            setattr(cfg, k, 0.0)
        cfg.short_low_strength_floor_threshold = 0.0
        cfg.long_low_strength_threshold = 0.0
        return cfg

    def _run(self, bb, bonus=0.03):
        from bot.main import apply_entry_priority_penalties

        return apply_entry_priority_penalties(
            signal="SHORT", combined_score=1.0, strength=1.0,
            short_reversal_risk=False, same_symbol_reentry=False,
            same_symbol_loss_reentry=False, chase_entry=False,
            btc_momentum_opposes=False, cfg=self._cfg(bonus), bb_participates=bb,
        )

    def test_participation_ranks_higher(self):
        self.assertAlmostEqual(self._run(True), 1.03, places=6)

    def test_no_participation_ranks_lower_but_is_not_blocked(self):
        """핵심 — 볼밴이 없어도 후보에서 탈락하지 않는다(원칙 1). 순위만 내려간다."""
        self.assertAlmostEqual(self._run(False), 0.97, places=6)

    def test_bonus_zero_is_a_noop(self):
        """기본값 0이면 아무 일도 하지 않는다(원복 경로)."""
        self.assertAlmostEqual(self._run(True, bonus=0.0), 1.0, places=6)
        self.assertAlmostEqual(self._run(False, bonus=0.0), 1.0, places=6)

    def test_unknown_participation_is_a_noop(self):
        """정보가 없으면(None) 가감하지 않는다."""
        self.assertAlmostEqual(self._run(None), 1.0, places=6)
