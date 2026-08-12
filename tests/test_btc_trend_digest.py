"""[2026-08-11 사용자요청] "비트 흐름을 1h/4h/12h/1d/3d로 30분마다 텔레그램으로 알려달라" —
analyze_btc_multi_timeframe_trend()과 format_btc_trend_digest()를 검증한다. 자동 조치는
하지 않고(참고용 정보만) 텔레그램 메시지 포맷만 확인. 실 API 호출 없음."""
import unittest

import pandas as pd

from bot.config import Config
from bot.main import BTC_TREND_TIMEFRAMES, analyze_btc_multi_timeframe_trend, format_btc_trend_digest


class FakeExchange:
    def __init__(self, direction="up"):
        self.direction = direction  # "up" 또는 "down"

    def get_klines(self, symbol, limit=60, interval=None):
        n = max(limit, 60)
        if self.direction == "up":
            closes = [100.0 + i * 0.5 for i in range(n)]
        else:
            closes = [100.0 - i * 0.5 for i in range(n)]
        return pd.DataFrame({
            "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
            "close": closes, "volume": [1000.0] * n, "taker_buy_base": [500.0] * n,
        })


class BtcTrendDigestTests(unittest.TestCase):
    def test_all_timeframes_present(self):
        cfg = Config()
        ex = FakeExchange("up")
        results = analyze_btc_multi_timeframe_trend(ex, cfg)
        self.assertEqual(set(results.keys()), set(BTC_TREND_TIMEFRAMES))

    def test_uptrend_detected(self):
        cfg = Config()
        ex = FakeExchange("up")
        results = analyze_btc_multi_timeframe_trend(ex, cfg)
        for interval in BTC_TREND_TIMEFRAMES:
            self.assertEqual(results[interval]["trend"], "상승")
            self.assertGreater(results[interval]["change_pct"], 0)

    def test_downtrend_detected(self):
        cfg = Config()
        ex = FakeExchange("down")
        results = analyze_btc_multi_timeframe_trend(ex, cfg)
        for interval in BTC_TREND_TIMEFRAMES:
            self.assertEqual(results[interval]["trend"], "하락")
            self.assertLess(results[interval]["change_pct"], 0)

    def test_fetch_failure_handled_gracefully(self):
        class BrokenExchange:
            def get_klines(self, *a, **kw):
                raise RuntimeError("API 오류")
        cfg = Config()
        results = analyze_btc_multi_timeframe_trend(BrokenExchange(), cfg)
        self.assertTrue(all(v is None for v in results.values()))
        # 포맷팅도 죽지 않고 "조회 실패"로 안전하게 표시돼야 함
        text = format_btc_trend_digest(results)
        self.assertIn("조회 실패", text)

    def test_digest_format_mentions_bias_when_mostly_down(self):
        cfg = Config()
        ex = FakeExchange("down")
        results = analyze_btc_multi_timeframe_trend(ex, cfg)
        text = format_btc_trend_digest(results)
        self.assertIn("SHORT", text)


if __name__ == "__main__":
    unittest.main()
