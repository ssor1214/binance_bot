"""BTC 초단기 모멘텀 역행 게이트 테스트.

[2026-08-14 사용자요청] 09:26~09:36 LONG 4연속손실(BTC가 그 10분간 -0.22% 미끄러짐)
재발방지 — 15분봉 기반 btc_alignment_multiplier로는 못 잡는 짧은(기본 5분) 역행을
감지하는지 확인한다. 실제 API는 호출하지 않고 klines만 가짜로 넣는다.
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.strategy import btc_short_term_momentum_opposes


class FakeExchange:
    def __init__(self, df):
        self.df = df

    def get_klines(self, symbol, interval="1m"):
        return self.df


def make_df(closes):
    return pd.DataFrame({"close": closes})


class BtcMomentumGateTests(unittest.TestCase):
    def _cfg(self):
        cfg = Config()
        cfg.btc_momentum_gate_enabled = True
        cfg.btc_momentum_gate_window_min = 5
        cfg.btc_momentum_gate_threshold_pct = 0.10
        return cfg

    def test_long_blocked_when_btc_dropped_beyond_threshold(self):
        cfg = self._cfg()
        # 5분 전 100.0 -> 지금 99.8 (-0.20%, 임계값 0.10%를 넘어 하락)
        closes = [100.0, 99.95, 99.9, 99.85, 99.82, 99.8]
        ex = FakeExchange(make_df(closes))
        self.assertTrue(btc_short_term_momentum_opposes(ex, cfg, "LONG"))

    def test_long_not_blocked_when_btc_flat(self):
        cfg = self._cfg()
        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        ex = FakeExchange(make_df(closes))
        self.assertFalse(btc_short_term_momentum_opposes(ex, cfg, "LONG"))

    def test_short_blocked_when_btc_rose_beyond_threshold(self):
        cfg = self._cfg()
        closes = [100.0, 100.05, 100.1, 100.15, 100.18, 100.2]
        ex = FakeExchange(make_df(closes))
        self.assertTrue(btc_short_term_momentum_opposes(ex, cfg, "SHORT"))

    def test_long_not_blocked_when_btc_rose(self):
        """SHORT을 막는 상승은 LONG은 오히려 순방향이라 막지 않는다."""
        cfg = self._cfg()
        closes = [100.0, 100.05, 100.1, 100.15, 100.18, 100.2]
        ex = FakeExchange(make_df(closes))
        self.assertFalse(btc_short_term_momentum_opposes(ex, cfg, "LONG"))

    def test_disabled_flag_always_returns_false(self):
        cfg = self._cfg()
        cfg.btc_momentum_gate_enabled = False
        closes = [100.0, 99.95, 99.9, 99.85, 99.82, 99.8]
        ex = FakeExchange(make_df(closes))
        self.assertFalse(btc_short_term_momentum_opposes(ex, cfg, "LONG"))

    def test_insufficient_data_returns_false(self):
        cfg = self._cfg()
        ex = FakeExchange(make_df([100.0, 99.0]))  # window=5인데 데이터 2개뿐
        self.assertFalse(btc_short_term_momentum_opposes(ex, cfg, "LONG"))

    def test_exchange_error_returns_false(self):
        cfg = self._cfg()

        class BrokenExchange:
            def get_klines(self, symbol, interval="1m"):
                raise RuntimeError("boom")

        self.assertFalse(btc_short_term_momentum_opposes(BrokenExchange(), cfg, "LONG"))


if __name__ == "__main__":
    unittest.main()
