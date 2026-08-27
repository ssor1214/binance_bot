"""[2026-08-25 B안] 1분봉 노이즈 필터를 방향별 꼬리로 본다.

기존엔 위아래 꼬리를 합쳐 몸통과 비교했다. 그런데 이 필터가 막으려는 건 "되돌림 위험"이지
"꼬리가 있다"가 아니다. 합산으로 보면 방향에 유리한 꼬리까지 벌점을 줘서 좋은 신호를 버린다.
실측상 이 필터는 신호의 22.5%를 차단하는 2위 병목이고, AAVEUSDT 급락 구간에서
되돌림 봉(꼬리/몸통 10.75)을 9번 막아 SHORT를 놓친 사례가 있었다.
"""
import unittest

import pandas as pd

from bot.config import Config
from bot.main import passes_one_min_noise_filter


class _Ex:
    def __init__(self, o, h, l, c):
        self._df = pd.DataFrame([{"open": o, "high": h, "low": l, "close": c}])

    def get_klines(self, symbol, interval=None, limit=None):
        return self._df


def _cfg(directional=True, ratio=2.5):
    cfg = Config()
    cfg.one_min_noise_filter_enabled = True
    cfg.one_min_noise_directional_wick = directional
    cfg.one_min_noise_max_wick_body_ratio = ratio
    return cfg


def _run(cfg, side, o, h, l, c):
    return passes_one_min_noise_filter(_Ex(o, h, l, c), cfg, "TESTUSDT", side)


class DirectionalWickTests(unittest.TestCase):
    def test_short_with_long_upper_wick_now_passes(self):
        """음봉 + 긴 윗꼬리 = 위에서 눌린 자국이라 SHORT에 유리하다. 예전엔 막혔다."""
        # 몸통 1, 윗꼬리 5(불리하지 않음), 아랫꼬리 0
        self.assertTrue(_run(_cfg(), "SHORT", o=100.0, h=105.0, l=99.0, c=99.0))
        self.assertFalse(_run(_cfg(directional=False), "SHORT", o=100.0, h=105.0, l=99.0, c=99.0))

    def test_short_with_long_lower_wick_is_still_blocked(self):
        """음봉 + 긴 아랫꼬리 = 내려갔다 되받힌 자국. SHORT에 불리하므로 계속 막아야 한다."""
        # 몸통 1, 아랫꼬리 5
        self.assertFalse(_run(_cfg(), "SHORT", o=100.0, h=100.0, l=94.0, c=99.0))

    def test_long_with_long_lower_wick_now_passes(self):
        """양봉 + 긴 아랫꼬리 = 눌렸다 회복한 자국이라 LONG에 유리하다."""
        self.assertTrue(_run(_cfg(), "LONG", o=100.0, h=101.0, l=95.0, c=101.0))
        self.assertFalse(_run(_cfg(directional=False), "LONG", o=100.0, h=101.0, l=95.0, c=101.0))

    def test_long_with_long_upper_wick_is_still_blocked(self):
        """양봉 + 긴 윗꼬리 = 올라갔다 밀린 자국. LONG에 불리하므로 계속 막아야 한다."""
        self.assertFalse(_run(_cfg(), "LONG", o=100.0, h=107.0, l=100.0, c=101.0))

    def test_candle_color_guard_is_unchanged(self):
        """봉 색 조건은 그대로 — 방향과 반대 색이면 여전히 막는다."""
        self.assertFalse(_run(_cfg(), "LONG", o=100.0, h=100.5, l=99.0, c=99.5))
        self.assertFalse(_run(_cfg(), "SHORT", o=100.0, h=101.0, l=99.5, c=100.5))

    def test_directional_never_blocks_more_than_combined(self):
        """불변식 — 방향별 꼬리는 합산 꼬리보다 클 수 없으므로 차단이 늘어날 수 없다.
        (원칙 1이 확실히 개선되는 근거)"""
        cases = [
            ("LONG", 100.0, 103.0, 97.0, 102.0),
            ("LONG", 100.0, 110.0, 99.0, 101.0),
            ("SHORT", 100.0, 103.0, 97.0, 98.0),
            ("SHORT", 100.0, 101.0, 90.0, 99.0),
        ]
        for side, o, h, l, c in cases:
            combined = _run(_cfg(directional=False), side, o, h, l, c)
            directional = _run(_cfg(), side, o, h, l, c)
            if combined:
                self.assertTrue(directional, msg=f"{side} {o}/{h}/{l}/{c} 에서 더 빡빡해졌다")

    def test_code_default_is_off(self):
        import inspect
        self.assertIn(
            'one_min_noise_directional_wick: bool = _bool("ONE_MIN_NOISE_DIRECTIONAL_WICK", "false")',
            inspect.getsource(Config),
        )


if __name__ == "__main__":
    unittest.main()
