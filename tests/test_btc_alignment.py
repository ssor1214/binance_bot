"""BTC 정렬 가중치 테스트.

BTC를 절대 정답으로 쓰지 않고, 같은 방향이면 살짝 가산하고 반대면 살짝 감산하는
로직이 맞는지 확인한다. 실제 API는 호출하지 않고 klines만 가짜로 넣는다.
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.strategy import btc_alignment_multiplier


class FakeExchange:
    def __init__(self, df):
        self.df = df

    def get_klines(self, symbol, interval="1m"):
        return self.df


def make_df(direction: str):
    if direction == "up":
        close = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
    elif direction == "down":
        close = [109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
    else:
        close = [100] * 10
    return pd.DataFrame({"close": close})


class BtcAlignmentTests(unittest.TestCase):
    def test_btc_match_boosts_same_direction(self):
        cfg = Config()
        cfg.btc_alignment_fast_ema = 2
        cfg.btc_alignment_slow_ema = 4
        cfg.btc_alignment_match_mult = 1.12
        cfg.btc_alignment_mismatch_mult = 0.88
        ex = FakeExchange(make_df("up"))

        self.assertAlmostEqual(btc_alignment_multiplier(ex, cfg, "LONG"), 1.12)
        self.assertAlmostEqual(btc_alignment_multiplier(ex, cfg, "SHORT"), 0.88)

    def test_btc_match_boosts_short_when_down(self):
        cfg = Config()
        cfg.btc_alignment_fast_ema = 2
        cfg.btc_alignment_slow_ema = 4
        cfg.btc_alignment_match_mult = 1.12
        cfg.btc_alignment_mismatch_mult = 0.88
        ex = FakeExchange(make_df("down"))

        self.assertAlmostEqual(btc_alignment_multiplier(ex, cfg, "SHORT"), 1.12)
        self.assertAlmostEqual(btc_alignment_multiplier(ex, cfg, "LONG"), 0.88)

    def test_neutral_when_not_enough_data(self):
        cfg = Config()
        cfg.btc_alignment_fast_ema = 20
        cfg.btc_alignment_slow_ema = 50
        ex = FakeExchange(pd.DataFrame({"close": [100, 101, 102, 103, 104]}))

        self.assertAlmostEqual(btc_alignment_multiplier(ex, cfg, "LONG"), 1.0)


if __name__ == "__main__":
    unittest.main()
