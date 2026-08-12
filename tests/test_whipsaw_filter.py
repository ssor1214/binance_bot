"""5m 변동성 대비 손절폭이 너무 좁거나(=하한 미달) 너무 넓은(=상한 초과, 휩쏘 위험) 후보를
거르는 필터 테스트. [2026-08-11 사용자요청] 상한선 추가 — SHORT 손절의 39%가 진입 1분
이내(순수STOP_LOSS 중앙값1.84분)에 발생, LONG은 0%(중앙값4.55분)였던 실측 근거."""
import sys
import unittest
from pathlib import Path

import pandas as pd
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import (
    assess_short_reversal_risk,
    passes_entry_range_position_filter,
    passes_one_min_noise_filter,
    passes_short_scalp_reversal_filter,
    passes_whipsaw_volatility_filter,
)


class FakeExchange:
    def __init__(self, df):
        self.df = df

    def get_klines(self, symbol, interval="5m"):
        return self.df


def make_df(close_last: float, atr: float):
    rows = [{"close": 100.0, "atr": atr} for _ in range(9)]
    rows.append({"close": close_last, "atr": atr})
    return pd.DataFrame(rows)


class WhipsawFilterTests(unittest.TestCase):
    def test_passes_when_atr_is_sufficient(self):
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.min_atr_vs_stop_ratio = 0.8
        cfg.max_atr_vs_stop_ratio = 4.0
        ex = FakeExchange(make_df(100.0, 2.5))  # 2.5%
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertTrue(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_blocks_when_atr_too_small(self):
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.min_atr_vs_stop_ratio = 0.8
        cfg.max_atr_vs_stop_ratio = 4.0
        ex = FakeExchange(make_df(100.0, 0.5))  # 0.5%
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertFalse(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_blocks_when_atr_too_large(self):
        """[2026-08-11] 상한선 신규 테스트 — 노이즈가 손절폭보다 훨씬 크면(휩쏘 위험) 진입 생략."""
        cfg = Config()
        cfg.stop_loss_pct = 5.0  # stop_dist_pct = 5/4 = 1.25%
        cfg.min_atr_vs_stop_ratio = 0.8
        cfg.max_atr_vs_stop_ratio = 4.0  # 상한 = 1.25% * 4 = 5.0%
        ex = FakeExchange(make_df(100.0, 6.0))  # 6.0% — 상한(5.0%) 초과
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertFalse(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_upper_bound_disabled_when_zero(self):
        """MAX_ATR_VS_STOP_RATIO=0이면 상한선 없이 기존(하한선만) 동작으로 돌아가야 한다."""
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.min_atr_vs_stop_ratio = 0.8
        cfg.max_atr_vs_stop_ratio = 0.0
        ex = FakeExchange(make_df(100.0, 6.0))  # 예전 같으면 통과했어야 할 큰 변동성
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertTrue(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_short_uses_short_stop_loss_pct(self):
        """SHORT_STOP_LOSS_PCT가 설정돼 있으면 SHORT 판단은 그 값을 기준으로 손절폭을 계산해야 한다."""
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.short_stop_loss_pct = 3.0  # SHORT는 stop_dist_pct = 3/4 = 0.75%, 상한 = 0.75*4=3.0%
        cfg.min_atr_vs_stop_ratio = 0.8
        cfg.max_atr_vs_stop_ratio = 4.0
        ex = FakeExchange(make_df(100.0, 3.5))  # LONG 기준(5%)이면 상한 안 넘지만, SHORT 기준(3%)이면 넘음
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertTrue(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "LONG"))
            self.assertFalse(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "SHORT"))

    def test_neutral_on_bad_data(self):
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.min_atr_vs_stop_ratio = 0.8
        cfg.max_atr_vs_stop_ratio = 4.0
        ex = FakeExchange(pd.DataFrame({"close": [0.0], "atr": [0.0]}))
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertTrue(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_one_min_noise_filter_blocks_large_wick(self):
        cfg = Config()
        df = pd.DataFrame([{"open": 100.0, "high": 103.0, "low": 99.5, "close": 100.5}])
        ex = FakeExchange(df)

        self.assertFalse(passes_one_min_noise_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_one_min_noise_filter_allows_clean_directional_candle(self):
        cfg = Config()
        df = pd.DataFrame([{"open": 100.0, "high": 101.2, "low": 99.9, "close": 101.0}])
        ex = FakeExchange(df)

        self.assertTrue(passes_one_min_noise_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_short_reversal_risk_flags_but_does_not_block(self):
        cfg = Config()
        df = pd.DataFrame([{"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.6}])
        ex = FakeExchange(df)

        risk = assess_short_reversal_risk(ex, cfg, "EPICUSDT")

        self.assertTrue(risk["risky"])
        self.assertTrue(risk["reasons"])

    def test_short_scalp_reversal_filter_blocks_close_far_above_low(self):
        cfg = Config()
        cfg.short_scalp_max_close_from_low_pct = 0.5
        df = pd.DataFrame([{"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0}])
        ex = FakeExchange(df)

        self.assertFalse(passes_short_scalp_reversal_filter(ex, cfg, "EPICUSDT"))

    def test_short_scalp_reversal_filter_allows_close_near_low(self):
        cfg = Config()
        cfg.short_scalp_max_close_from_low_pct = 0.5
        df = pd.DataFrame([{"open": 100.0, "high": 101.0, "low": 99.5, "close": 99.8}])
        ex = FakeExchange(df)

        self.assertTrue(passes_short_scalp_reversal_filter(ex, cfg, "EPICUSDT"))


    def test_entry_range_position_blocks_long_chasing_top(self):
        """[2026-08-12 사용자요청] 직전20분 범위 상단 근처(꼭대기 추격)에서 LONG 진입을 막는다."""
        cfg = Config()
        cfg.entry_range_position_lookback_min = 3
        cfg.entry_range_position_max_pct = 70.0
        # 직전 3분 범위 [100, 110], 마지막 종가 109 -> 위치 90% (상단 근접)
        df = pd.DataFrame([
            {"open": 100.0, "high": 105.0, "low": 100.0, "close": 103.0},
            {"open": 103.0, "high": 110.0, "low": 102.0, "close": 108.0},
            {"open": 108.0, "high": 109.5, "low": 107.0, "close": 109.0},
        ])
        ex = FakeExchange(df)
        self.assertFalse(passes_entry_range_position_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_entry_range_position_allows_long_near_low(self):
        """범위 하단 근처(눌림목 재진입)면 LONG 진입을 허용한다."""
        cfg = Config()
        cfg.entry_range_position_lookback_min = 3
        cfg.entry_range_position_max_pct = 70.0
        # 직전 3분 범위 [100, 110], 마지막 종가 103 -> 위치 30%
        df = pd.DataFrame([
            {"open": 108.0, "high": 110.0, "low": 107.0, "close": 108.0},
            {"open": 108.0, "high": 108.5, "low": 102.0, "close": 104.0},
            {"open": 104.0, "high": 105.0, "low": 100.0, "close": 103.0},
        ])
        ex = FakeExchange(df)
        self.assertTrue(passes_entry_range_position_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_entry_range_position_blocks_short_chasing_bottom(self):
        """SHORT는 대칭적으로 범위 하단 근처(바닥 추격매도)를 막는다."""
        cfg = Config()
        cfg.entry_range_position_lookback_min = 3
        cfg.entry_range_position_max_pct = 70.0
        # 직전 3분 범위 [100, 110], 마지막 종가 101 -> 위치 10% (하단 근접, 임계값 100-70=30% 미만)
        df = pd.DataFrame([
            {"open": 108.0, "high": 110.0, "low": 107.0, "close": 108.0},
            {"open": 108.0, "high": 108.5, "low": 102.0, "close": 104.0},
            {"open": 104.0, "high": 105.0, "low": 100.0, "close": 101.0},
        ])
        ex = FakeExchange(df)
        self.assertFalse(passes_entry_range_position_filter(ex, cfg, "EPICUSDT", "SHORT"))

    def test_entry_range_position_disabled_passthrough(self):
        cfg = Config()
        cfg.entry_range_position_filter_enabled = False
        cfg.entry_range_position_lookback_min = 3
        df = pd.DataFrame([
            {"open": 100.0, "high": 105.0, "low": 100.0, "close": 103.0},
            {"open": 103.0, "high": 110.0, "low": 102.0, "close": 108.0},
            {"open": 108.0, "high": 109.5, "low": 107.0, "close": 109.0},
        ])
        ex = FakeExchange(df)
        self.assertTrue(passes_entry_range_position_filter(ex, cfg, "EPICUSDT", "LONG"))

    def test_entry_range_position_neutral_on_insufficient_data(self):
        cfg = Config()
        cfg.entry_range_position_lookback_min = 20
        df = pd.DataFrame([{"open": 100.0, "high": 105.0, "low": 100.0, "close": 104.9}])
        ex = FakeExchange(df)
        self.assertTrue(passes_entry_range_position_filter(ex, cfg, "EPICUSDT", "LONG"))


if __name__ == "__main__":
    unittest.main()
