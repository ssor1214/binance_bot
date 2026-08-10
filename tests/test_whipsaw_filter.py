"""5m 변동성 대비 손절폭이 너무 좁은 후보를 거르는 필터 테스트."""
import sys
import unittest
from pathlib import Path

import pandas as pd
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import passes_whipsaw_volatility_filter


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
        ex = FakeExchange(make_df(100.0, 2.5))  # 2.5%
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertTrue(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT"))

    def test_blocks_when_atr_too_small(self):
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.min_atr_vs_stop_ratio = 0.8
        ex = FakeExchange(make_df(100.0, 0.5))  # 0.5%
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertFalse(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT"))

    def test_neutral_on_bad_data(self):
        cfg = Config()
        cfg.stop_loss_pct = 5.0
        cfg.min_atr_vs_stop_ratio = 0.8
        ex = FakeExchange(pd.DataFrame({"close": [0.0], "atr": [0.0]}))
        with patch("bot.main.add_indicators", lambda df, cfg: df):
            self.assertTrue(passes_whipsaw_volatility_filter(ex, cfg, "EPICUSDT"))


if __name__ == "__main__":
    unittest.main()
