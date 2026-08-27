"""[2026-08-25 A안] whipsaw 하한의 ATR 지각 보정 테스트.

배경: 하한 판정을 ATR(14기간 평균)만으로 해서, 급락/급등이 시작된 직후에는 아직 ATR이
낮아 신호가 차단되고 ATR이 하한을 넘길 무렵엔 이미 움직임이 끝나 있었다(AAVEUSDT 실사례).
하한 "값"은 그대로 두고, ATR이 못 따라온 경우에만 실제 변동폭으로 대신 판정한다.
"""
import unittest

import pandas as pd

from bot.config import Config
from bot.main import passes_whipsaw_volatility_filter


class _Ex:
    def __init__(self, df):
        self._df = df

    def get_klines(self, symbol, interval=None, limit=None):
        return self._df.copy()


def _df(base_range_pct, last_range_pct, close=100.0, n=60):
    """캔들 변동폭으로 ATR을 "실제로" 만들어낸다.

    passes_whipsaw_volatility_filter가 내부에서 add_indicators()를 다시 돌려 atr을
    재계산하므로, atr 컬럼을 주입하는 방식으로는 테스트가 성립하지 않는다(실측으로 확인).
    앞 구간 변동폭(base)과 마지막 2봉 변동폭(last)을 따로 주면
    ATR(14기간 평균)은 base에 가깝고 최근 실변동폭은 last가 된다 — ATR 후행 상황 재현.
    """
    rows = []
    for i in range(n):
        rng = last_range_pct if i >= n - 2 else base_range_pct
        half = close * rng / 100 / 2
        rows.append({
            "open": close, "high": close + half, "low": close - half, "close": close,
            # add_indicators가 요구하는 컬럼 — 빠지면 필터가 예외를 삼키고 무조건 통과(True)한다.
            "volume": 1.0, "quote_volume": 100.0, "taker_buy_base": 0.5, "taker_buy_quote": 50.0,
        })
    return pd.DataFrame(rows)


def _cfg(enabled=True):
    cfg = Config()
    cfg.whipsaw_immediate_vol_enabled = enabled
    cfg.whipsaw_immediate_lookback = 2
    cfg.stop_loss_pct = 6.0
    cfg.short_stop_loss_pct = 4.5
    cfg.min_atr_vs_stop_ratio = 0.7
    cfg.max_atr_vs_stop_ratio = 4.0
    return cfg


# SHORT 기준 손절폭 4.5/4 = 1.125% -> 하한 0.7875%, 상한 4.5%
LOW = 0.7875


def _run(cfg, base_range_pct, last_range_pct):
    df = _df(base_range_pct, last_range_pct)
    return passes_whipsaw_volatility_filter(_Ex(df), cfg, "AAVEUSDT", "SHORT", leverage=4.0)


class WhipsawImmediateVolTests(unittest.TestCase):
    def test_atr_lagging_but_real_move_passes(self):
        """AAVEUSDT 케이스 — ATR은 하한 미달인데 지금 실제로 그만큼 움직였다면 통과."""
        self.assertTrue(_run(_cfg(), base_range_pct=0.30, last_range_pct=2.5))

    def test_dead_symbol_still_blocked(self):
        """ATR도 낮고 실변동폭도 없으면 여전히 차단 — 죽은 코인 거르기라는 원래 목적 유지."""
        self.assertFalse(_run(_cfg(), base_range_pct=0.20, last_range_pct=0.20))

    def test_upper_bound_untouched(self):
        """상한(과열 차단)은 이 수정과 무관하게 그대로 동작해야 한다."""
        self.assertFalse(_run(_cfg(), base_range_pct=9.0, last_range_pct=9.0))

    def test_disabled_keeps_legacy_behavior(self):
        """플래그를 끄면 예전처럼 ATR만 보고 차단한다(원복 경로)."""
        self.assertFalse(_run(_cfg(enabled=False), base_range_pct=0.30, last_range_pct=2.5))

    def test_normal_atr_passes_regardless(self):
        self.assertTrue(_run(_cfg(), base_range_pct=1.5, last_range_pct=1.5))

    def test_code_default_is_off(self):
        import inspect
        self.assertIn(
            'whipsaw_immediate_vol_enabled: bool = _bool("WHIPSAW_IMMEDIATE_VOL_ENABLED", "false")',
            inspect.getsource(Config),
        )


if __name__ == "__main__":
    unittest.main()


class WhipsawUpperBoundConsistencyTests(unittest.TestCase):
    """[2026-08-25 C안] 하한을 실변동폭으로 통과시켰으면 상한도 같은 기준으로 봐야 한다.

    그러지 않으면 "실변동폭이 손절폭의 8배가 넘는 극단 휩쏘 종목"이 하한은 실변동폭으로
    뚫고 들어오는데 상한은 ATR로만 봐서 안 걸리는 구멍이 생긴다.
    """

    def test_extreme_immediate_range_is_blocked(self):
        """ATR은 낮아도 최근 실변동폭이 상한을 넘으면 차단 — A안이 연 구멍을 닫는다."""
        self.assertFalse(_run(_cfg(), base_range_pct=0.30, last_range_pct=12.0))

    def test_moderate_immediate_range_still_passes(self):
        """상한 안쪽(손절폭 1.125% x 4.0 = 4.5%)이면 그대로 통과한다."""
        self.assertTrue(_run(_cfg(), base_range_pct=0.30, last_range_pct=3.0))

    def test_disabled_flag_keeps_atr_only_upper_bound(self):
        """플래그를 끄면 상한도 예전처럼 ATR만 본다(원복 경로)."""
        self.assertTrue(_run(_cfg(enabled=False), base_range_pct=0.9, last_range_pct=12.0))
