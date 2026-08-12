"""[2026-08-11 사용자요청] "진입 직전 재검증 속도 개선" — get_last_price_fast()가 WS 캐시
신선하면 REST 없이 즉시 반환하고, 캐시 없거나 오래됐으면 안전하게 REST(get_mark_price)로
폴백하는지 검증한다. 실 API 호출 없음(전부 Mock)."""
import unittest
from unittest.mock import MagicMock, patch

from bot.config import Config
from bot.exchange import Exchange


def make_exchange():
    ex = Exchange.__new__(Exchange)  # __init__(REST 호출) 건드리지 않음
    ex.cfg = Config()
    ex.client = MagicMock()
    ex._ws_kline_cache = None
    ex._fill_tracker = None
    return ex


class GetLastPriceFastTests(unittest.TestCase):
    def test_uses_ws_cache_when_fresh(self):
        ex = make_exchange()
        cache = MagicMock()
        cache.is_fresh.return_value = True
        import pandas as pd
        cache.to_dataframe.return_value = pd.DataFrame({"close": [100.0, 101.5]})
        ex._ws_kline_cache = cache

        price = ex.get_last_price_fast("BTCUSDT")
        self.assertEqual(price, 101.5)
        ex.client.futures_mark_price.assert_not_called()  # REST 호출 없이 캐시로만 해결돼야 함

    def test_falls_back_to_rest_when_cache_stale(self):
        ex = make_exchange()
        cache = MagicMock()
        cache.is_fresh.return_value = False
        ex._ws_kline_cache = cache
        ex.client.futures_mark_price.return_value = {"markPrice": "99.9"}

        price = ex.get_last_price_fast("BTCUSDT")
        self.assertEqual(price, 99.9)
        ex.client.futures_mark_price.assert_called_once()

    def test_falls_back_to_rest_when_no_cache_attached(self):
        ex = make_exchange()  # _ws_kline_cache = None (기본)
        ex.client.futures_mark_price.return_value = {"markPrice": "50.0"}

        price = ex.get_last_price_fast("ETHUSDT")
        self.assertEqual(price, 50.0)
        ex.client.futures_mark_price.assert_called_once()

    def test_falls_back_to_rest_when_cache_read_raises(self):
        ex = make_exchange()
        cache = MagicMock()
        cache.is_fresh.return_value = True
        cache.to_dataframe.side_effect = Exception("깨진 캐시 파일")
        ex._ws_kline_cache = cache
        ex.client.futures_mark_price.return_value = {"markPrice": "77.7"}

        price = ex.get_last_price_fast("SOLUSDT")
        self.assertEqual(price, 77.7)


if __name__ == "__main__":
    unittest.main()
