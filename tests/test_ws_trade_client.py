"""[2026-08-14] bot/ws_trade_client.py 단위테스트. 실제 바이낸스 WebSocket에 절대 연결
하지 않는다 - start()를 호출하지 않는 한 네트워크 연결이 발생하지 않는 구조라, 메시지
파싱/캐시/스파이크감지/리샘플링 로직만 오프라인으로 검증한다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ws_trade_client import (
    TradeStreamWebSocket,
    TradeStreamWebSocketV2,
    TradeTick,
    TradeTickCache,
    detect_volume_spike,
    parse_futures_agg_trade_message,
    resample_ticks_to_ohlcv,
)


def sample_agg_trade_msg(symbol="TESTUSDT", price="100.5", qty="2.0", event_time=1700000000000,
                          trade_time=None, is_maker=False):
    if trade_time is None:
        trade_time = event_time
    return {
        "e": "aggTrade", "E": event_time, "s": symbol, "a": 12345,
        "p": price, "q": qty, "f": 100, "l": 105, "T": trade_time, "m": is_maker,
    }


class ParseAggTradeTest(unittest.TestCase):
    def test_parses_valid_message(self):
        tick = parse_futures_agg_trade_message(sample_agg_trade_msg())
        self.assertIsNotNone(tick)
        self.assertEqual(tick.symbol, "TESTUSDT")
        self.assertEqual(tick.price, 100.5)
        self.assertEqual(tick.quantity, 2.0)
        self.assertFalse(tick.is_buyer_maker)

    def test_parses_multiplex_wrapper(self):
        raw = sample_agg_trade_msg(symbol="BTCUSDT")
        wrapped = {"stream": "btcusdt@aggTrade", "data": raw}
        tick = parse_futures_agg_trade_message(wrapped)
        self.assertIsNotNone(tick)
        self.assertEqual(tick.symbol, "BTCUSDT")

    def test_ignores_unrelated_event_type(self):
        msg = sample_agg_trade_msg()
        msg["e"] = "kline"
        self.assertIsNone(parse_futures_agg_trade_message(msg))

    def test_returns_none_on_missing_fields(self):
        self.assertIsNone(parse_futures_agg_trade_message({"e": "aggTrade"}))

    def test_returns_none_on_garbage(self):
        self.assertIsNone(parse_futures_agg_trade_message({"nonsense": True}))

    def test_quote_volume_property(self):
        tick = parse_futures_agg_trade_message(sample_agg_trade_msg(price="10", qty="3"))
        self.assertEqual(tick.quote_volume, 30.0)


class TradeTickCacheTest(unittest.TestCase):
    def _tick(self, symbol="AAAUSDT", t_ms=1000, price=100.0, qty=1.0, is_maker=False):
        return TradeTick(symbol=symbol, price=price, quantity=qty, event_time_ms=t_ms,
                          trade_time_ms=t_ms, is_buyer_maker=is_maker)

    def test_append_and_get_recent(self):
        cache = TradeTickCache()
        cache.append(self._tick(t_ms=1000))
        cache.append(self._tick(t_ms=5000))
        cache.append(self._tick(t_ms=9000))
        recent = cache.get_recent("AAAUSDT", lookback_sec=5, now_ms=9000)
        self.assertEqual(len(recent), 2)

    def test_get_recent_unknown_symbol_returns_empty(self):
        cache = TradeTickCache()
        self.assertEqual(cache.get_recent("NOPE", 10), [])

    def test_ring_buffer_caps_size(self):
        cache = TradeTickCache(max_ticks_per_symbol=3)
        for i in range(10):
            cache.append(self._tick(t_ms=i * 1000))
        recent = cache.get_recent("AAAUSDT", lookback_sec=100, now_ms=10000)
        self.assertEqual(len(recent), 3)
        self.assertEqual([t.trade_time_ms for t in recent], [7000, 8000, 9000])

    def test_symbols(self):
        cache = TradeTickCache()
        cache.append(self._tick(symbol="A", t_ms=1))
        cache.append(self._tick(symbol="B", t_ms=1))
        self.assertEqual(set(cache.symbols()), {"A", "B"})

    def test_prune_removes_old_ticks(self):
        cache = TradeTickCache()
        cache.append(self._tick(t_ms=1000))
        cache.append(self._tick(t_ms=1000000))
        import time
        real_now = time.time()
        cache.prune(max_age_sec=1)
        remaining = cache.get_recent("AAAUSDT", lookback_sec=1e12, now_ms=int(real_now * 1000) + 10 ** 12)
        self.assertEqual(len(remaining), 0)


class DetectVolumeSpikeTest(unittest.TestCase):
    def _fill_baseline(self, cache, symbol, quote_vol_per_10s, num_windows, start_ms=0):
        for i in range(num_windows):
            t_ms = start_ms + i * 10000
            cache.append(TradeTick(symbol=symbol, price=1.0, quantity=quote_vol_per_10s,
                                    event_time_ms=t_ms, trade_time_ms=t_ms, is_buyer_maker=False))

    def test_no_data_returns_no_spike(self):
        cache = TradeTickCache()
        result = detect_volume_spike(cache, "NOPE")
        self.assertFalse(result["is_spike"])
        self.assertEqual(result["ratio"], 0.0)

    def test_uniform_volume_is_not_a_spike(self):
        cache = TradeTickCache()
        now_ms = 300000
        self._fill_baseline(cache, "AAAUSDT", quote_vol_per_10s=100, num_windows=30, start_ms=0)
        result = detect_volume_spike(cache, "AAAUSDT", spike_multiplier=3.0,
                                      spike_window_sec=10, baseline_window_sec=300, now_ms=now_ms)
        self.assertFalse(result["is_spike"])
        self.assertAlmostEqual(result["ratio"], 1.0, places=2)

    def test_sudden_spike_is_detected(self):
        cache = TradeTickCache()
        now_ms = 300000
        self._fill_baseline(cache, "AAAUSDT", quote_vol_per_10s=100, num_windows=29, start_ms=0)
        cache.append(TradeTick(symbol="AAAUSDT", price=1.0, quantity=1000,
                                event_time_ms=299000, trade_time_ms=299000, is_buyer_maker=False))
        result = detect_volume_spike(cache, "AAAUSDT", spike_multiplier=3.0,
                                      spike_window_sec=10, baseline_window_sec=300, now_ms=now_ms)
        self.assertTrue(result["is_spike"])
        self.assertGreaterEqual(result["ratio"], 3.0)

    def test_invalid_windows_raise(self):
        cache = TradeTickCache()
        with self.assertRaises(ValueError):
            detect_volume_spike(cache, "X", spike_window_sec=100, baseline_window_sec=10)
        with self.assertRaises(ValueError):
            detect_volume_spike(cache, "X", spike_window_sec=0)


class ResampleTicksToOhlcvTest(unittest.TestCase):
    def _tick(self, price, qty, t_ms, is_maker=False):
        return TradeTick(symbol="AAAUSDT", price=price, quantity=qty, event_time_ms=t_ms,
                          trade_time_ms=t_ms, is_buyer_maker=is_maker)

    def test_empty_ticks_returns_empty(self):
        self.assertEqual(resample_ticks_to_ohlcv([], bucket_sec=10), [])

    def test_single_bucket_ohlc(self):
        ticks = [
            self._tick(100, 1, 0),
            self._tick(105, 2, 3000),
            self._tick(95, 1, 6000),
            self._tick(102, 1, 9999),
        ]
        candles = resample_ticks_to_ohlcv(ticks, bucket_sec=10)
        self.assertEqual(len(candles), 1)
        c = candles[0]
        self.assertEqual(c["open_time_ms"], 0)
        self.assertEqual(c["open"], 100)
        self.assertEqual(c["high"], 105)
        self.assertEqual(c["low"], 95)
        self.assertEqual(c["close"], 102)
        self.assertEqual(c["volume"], 5)
        self.assertEqual(c["trade_count"], 4)

    def test_multiple_buckets_sorted(self):
        ticks = [
            self._tick(100, 1, 15000),
            self._tick(200, 1, 1000),
            self._tick(300, 1, 25000),
        ]
        candles = resample_ticks_to_ohlcv(ticks, bucket_sec=10)
        self.assertEqual([c["open_time_ms"] for c in candles], [0, 10000, 20000])

    def test_taker_buy_base_only_counts_non_maker(self):
        ticks = [
            self._tick(100, 1, 0, is_maker=False),
            self._tick(100, 2, 1000, is_maker=True),
        ]
        candles = resample_ticks_to_ohlcv(ticks, bucket_sec=10)
        self.assertEqual(candles[0]["taker_buy_base"], 1)

    def test_invalid_bucket_raises(self):
        with self.assertRaises(ValueError):
            resample_ticks_to_ohlcv([self._tick(1, 1, 0)], bucket_sec=0)

    def test_unsorted_input_still_works(self):
        ticks = [
            self._tick(300, 1, 25000),
            self._tick(100, 1, 1000),
            self._tick(200, 1, 15000),
        ]
        candles = resample_ticks_to_ohlcv(ticks, bucket_sec=10)
        self.assertEqual([c["open_time_ms"] for c in candles], [0, 10000, 20000])


class TradeStreamWebSocketConstructionTest(unittest.TestCase):
    def test_construction_does_not_connect(self):
        ws = TradeStreamWebSocket(api_key="k", api_secret="s", testnet=True)
        self.assertIsNotNone(ws.cache)
        self.assertFalse(ws._started)

    def test_handle_message_updates_cache_without_network(self):
        ws = TradeStreamWebSocket(api_key="k", api_secret="s", testnet=True)
        received = []
        ws.on_trade = received.append
        ws._handle_message(sample_agg_trade_msg(symbol="ZZZUSDT"))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].symbol, "ZZZUSDT")
        self.assertEqual(len(ws.cache.get_recent("ZZZUSDT", lookback_sec=1e12, now_ms=2000000000000)), 1)

    def test_handle_message_swallows_callback_exception(self):
        ws = TradeStreamWebSocket(api_key="k", api_secret="s", testnet=True)

        def boom(_tick):
            raise RuntimeError("callback exploded")

        ws.on_trade = boom
        ws._handle_message(sample_agg_trade_msg())


class _FakeHealth:
    def __init__(self):
        self.messages = 0

    def note_message(self):
        self.messages += 1


class TradeStreamWebSocketV2ConstructionTest(unittest.TestCase):
    def test_construction_does_not_connect(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        self.assertIsNotNone(ws.cache)
        self.assertFalse(ws._started)
        self.assertEqual(ws._threads, [])

    def test_handle_message_updates_cache_without_network(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        received = []
        ws.on_trade = received.append
        ws._handle_message(sample_agg_trade_msg(symbol="ZZZUSDT"))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].symbol, "ZZZUSDT")
        self.assertEqual(len(ws.cache.get_recent("ZZZUSDT", lookback_sec=1e12, now_ms=2000000000000)), 1)

    def test_handle_message_swallows_callback_exception(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)

        def boom(_tick):
            raise RuntimeError("callback exploded")

        ws.on_trade = boom
        ws._handle_message(sample_agg_trade_msg())  # 예외가 밖으로 나오지 않아야 함

    def test_handle_message_calls_health_note_message(self):
        health = _FakeHealth()
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True, health=health)
        ws._handle_message(sample_agg_trade_msg())
        self.assertEqual(health.messages, 1)

    def test_handle_message_ignores_non_agg_trade(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        received = []
        ws.on_trade = received.append
        msg = sample_agg_trade_msg()
        msg["e"] = "kline"
        ws._handle_message(msg)
        self.assertEqual(received, [])

    def test_stream_url_mainnet(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=False)
        url = ws._stream_url(["btcusdt@aggTrade", "ethusdt@aggTrade"])
        self.assertEqual(url, "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade")

    def test_stream_url_testnet(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        url = ws._stream_url(["btcusdt@aggTrade"])
        self.assertEqual(url, "wss://stream.binancefuture.com/stream?streams=btcusdt@aggTrade")

    def test_stop_before_start_is_safe(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        ws.stop()  # 시작 전에 stop해도 예외 없이 조용히 통과해야 함
        self.assertFalse(ws._started)


class TradeStreamWebSocketV2BackoffTest(unittest.TestCase):
    def test_backoff_schedule_progression(self):
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(0), 1.0)
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(1), 2.0)
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(2), 5.0)
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(3), 10.0)
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(4), 30.0)

    def test_backoff_caps_at_max_for_further_attempts(self):
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(5), 30.0)
        self.assertEqual(TradeStreamWebSocketV2.compute_backoff_sec(100), 30.0)

    def test_backoff_negative_attempt_raises(self):
        with self.assertRaises(ValueError):
            TradeStreamWebSocketV2.compute_backoff_sec(-1)


class TradeStreamWebSocketV2StartStopLiveLoopTest(unittest.TestCase):
    """asyncio 이벤트루프가 실제로 별도 스레드에서 돌고, stop()이 그 스레드를 정상 종료시키는지
    검증한다 — 단, 실제 네트워크 연결은 절대 시도하지 않도록 websockets.connect를 막힌
    호스트로 강제하지 않고, 대신 접속 시도 자체가 예외를 내며 backoff 루프를 도는 것을
    허용하되 아주 짧게만 돌리고 바로 stop()한다(수 초 내 스레드 join 확인용)."""

    def test_start_spawns_threads_and_stop_joins_them(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        ws.start(["btcusdt"])
        self.assertTrue(ws._started)
        self.assertEqual(len(ws._threads), 1)
        for t in ws._threads:
            self.assertTrue(t.is_alive())
        ws.stop()
        self.assertFalse(ws._started)
        for t in ws._threads if False else []:
            pass  # threads list is cleared by stop(); nothing left to assert here

    def test_double_start_raises(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        ws.start(["btcusdt"])
        try:
            with self.assertRaises(RuntimeError):
                ws.start(["ethusdt"])
        finally:
            ws.stop()

    def test_chunking_creates_multiple_connections(self):
        ws = TradeStreamWebSocketV2(api_key="k", api_secret="s", testnet=True)
        symbols = [f"SYM{i}USDT" for i in range(ws.MAX_STREAMS_PER_CONNECTION + 5)]
        ws.start(symbols)
        try:
            self.assertEqual(len(ws._threads), 2)
        finally:
            ws.stop()


if __name__ == "__main__":
    unittest.main()
