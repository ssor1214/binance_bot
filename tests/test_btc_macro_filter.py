"""[2026-08-10 사용자요청] "BTC 3단계 필터" 중 저희한테 없던 2번째 단계(매크로/지속 하락
감지) 단위테스트. is_btc_unstable()이 짧은 창(순간급변동)과 긴 창(지속하락) 중 하나라도
걸리면 True를 반환하는지 검증한다. 실 API를 절대 호출하지 않는다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import is_btc_unstable


def cfg():
    c = Config()
    c.btc_lookback_candles = 3
    c.btc_sudden_move_pct = 1.2
    c.btc_macro_lookback_candles = 15
    c.btc_macro_move_pct = 1.5
    return c


def make_df(closes):
    """종가 리스트로 간단한 DataFrame을 만든다(open_time 등은 불필요, close만 사용됨)."""
    return pd.DataFrame({"close": closes})


class IsBtcUnstableTests(unittest.TestCase):
    def test_false_when_market_calm(self):
        ex = MagicMock()
        # 20개 캔들, 거의 안 움직임(둘 다 문턱 미달)
        ex.get_klines.return_value = make_df([100.0] * 20)
        self.assertFalse(is_btc_unstable(ex, cfg()))

    def test_true_when_short_window_sudden_move(self):
        """짧은 창(3분)만으로도 걸리는 순간 급변동."""
        ex = MagicMock()
        closes = [100.0] * 17 + [100.0, 100.0, 100.0, 98.0]  # 마지막 4개 구간 -2%
        ex.get_klines.return_value = make_df(closes)
        self.assertTrue(is_btc_unstable(ex, cfg()))

    def test_true_when_long_window_macro_decline_even_if_short_window_calm(self):
        """[핵심] 짧은 창(최근 3분)은 잠잠해 보여도, 긴 창(15분) 전체로 보면 꾸준히
        하락해온 매크로 붕괴는 잡아야 한다."""
        ex = MagicMock()
        # 15분에 걸쳐 100 -> 98(-2%)로 서서히 하락, 마지막 3분은 거의 평평(짧은 창 안 걸림)
        closes = [100.0 - i * (2.0 / 15) for i in range(17)]  # 완만한 하락
        closes[-3:] = [closes[-4], closes[-4], closes[-4]]  # 최근 3분은 평평하게 고정
        ex.get_klines.return_value = make_df(closes)
        cfg_obj = cfg()
        self.assertTrue(is_btc_unstable(ex, cfg_obj))

    def test_false_when_not_enough_candles_for_either_window(self):
        ex = MagicMock()
        ex.get_klines.return_value = make_df([100.0, 99.0])  # 너무 적음
        self.assertFalse(is_btc_unstable(ex, cfg()))

    def test_false_when_rest_call_fails(self):
        """[회귀] 조회 실패시 신규 진입을 막지 않는다(기존 동작 유지) — 지나치게
        보수적으로 모든 진입을 막아버리면 안 됨."""
        ex = MagicMock()
        ex.get_klines.side_effect = Exception("REST 실패(테스트 모의)")
        self.assertFalse(is_btc_unstable(ex, cfg()))

    def test_fetches_klines_with_limit_covering_the_longer_window(self):
        """두 창 중 더 긴 쪽(매크로, 15분)을 커버할 만큼 충분히 캔들을 가져와야 한다."""
        ex = MagicMock()
        ex.get_klines.return_value = make_df([100.0] * 20)
        is_btc_unstable(ex, cfg())
        _, call_kwargs = ex.get_klines.call_args
        self.assertGreaterEqual(call_kwargs["limit"], 15)


if __name__ == "__main__":
    unittest.main()
