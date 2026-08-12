"""[2026-08-10] bot/ws_client.py 단위테스트. 실제 바이낸스 WebSocket에 절대 연결하지
않는다 — start()를 호출하지 않는 한 네트워크 연결이 발생하지 않는 구조라, 메시지
파싱/캐시 로직만 오프라인으로 검증한다."""
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ws_client import (
    FillTracker,
    KlineCache,
    KlineUpdate,
    MarketDataWebSocket,
    OrderFill,
    RollingKlineCache,
    UserDataWebSocket,
    parse_futures_kline_message,
    parse_order_trade_update,
)


def sample_msg(symbol="TESTUSDT", closed=False):
    return {
        "e": "kline", "E": 123456789, "s": symbol,
        "k": {
            "t": 100000, "T": 100059, "s": symbol, "i": "1m",
            "o": "1.0000", "c": "1.0500", "h": "1.0600", "l": "0.9900",
            "v": "1000.0", "x": closed,
        },
    }


def continuous_kline_msg(symbol="TESTUSDT", closed=False):
    """[2026-08-10] start_kline_futures_socket이 테스트넷 실증에서 실제로 보내는 형식 —
    최상위 심볼 필드가 "s"가 아니라 "ps"(pair symbol)이고 이벤트명이 "continuous_kline"."""
    return {
        "e": "continuous_kline", "E": 123456789, "ps": symbol, "ct": "PERPETUAL",
        "k": {
            "t": 100000, "T": 100059, "i": "1m",
            "o": "1.0000", "c": "1.0500", "h": "1.0600", "l": "0.9900",
            "v": "1000.0", "x": closed,
        },
    }


class ParseKlineMessageTests(unittest.TestCase):
    def test_parses_continuous_kline_event_shape(self):
        """[2026-08-10 테스트넷 실증 회귀테스트] 이 형식을 놓치면 실제 소켓에서 오는 모든
        메시지가 조용히 버려진다 — 처음 구현이 정확히 이 버그였음."""
        k = parse_futures_kline_message(continuous_kline_msg("BTCUSDT"))
        self.assertIsNotNone(k)
        self.assertEqual(k.symbol, "BTCUSDT")
        self.assertEqual(k.close, 1.05)

    def test_parses_valid_single_socket_message(self):
        k = parse_futures_kline_message(sample_msg())
        self.assertIsNotNone(k)
        self.assertEqual(k.symbol, "TESTUSDT")
        self.assertEqual(k.close, 1.05)
        self.assertFalse(k.is_closed)

    def test_parses_closed_candle_flag(self):
        k = parse_futures_kline_message(sample_msg(closed=True))
        self.assertTrue(k.is_closed)

    def test_parses_multiplex_wrapped_message(self):
        wrapped = {"stream": "testusdt@kline_1m", "data": sample_msg()}
        k = parse_futures_kline_message(wrapped)
        self.assertIsNotNone(k)
        self.assertEqual(k.symbol, "TESTUSDT")

    def test_malformed_message_returns_none_without_raising(self):
        self.assertIsNone(parse_futures_kline_message({"unexpected": "shape"}))
        self.assertIsNone(parse_futures_kline_message({}))
        self.assertIsNone(parse_futures_kline_message({"k": {"t": "not-a-number", "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "x": False, "T": 1, "s": "X"}}))


class KlineCacheTests(unittest.TestCase):
    def test_update_and_get_roundtrip(self):
        cache = KlineCache()
        k = parse_futures_kline_message(sample_msg())
        cache.update(k)
        self.assertEqual(cache.get("TESTUSDT").close, 1.05)

    def test_get_missing_symbol_returns_none(self):
        cache = KlineCache()
        self.assertIsNone(cache.get("NOPEUSDT"))

    def test_symbols_lists_all_tracked(self):
        cache = KlineCache()
        cache.update(parse_futures_kline_message(sample_msg("AUSDT")))
        cache.update(parse_futures_kline_message(sample_msg("BUSDT")))
        self.assertEqual(sorted(cache.symbols()), ["AUSDT", "BUSDT"])

    def test_update_overwrites_previous_value_for_same_symbol(self):
        cache = KlineCache()
        cache.update(parse_futures_kline_message(sample_msg("AUSDT")))
        newer = KlineUpdate(symbol="AUSDT", open_time=2, open=1, high=1, low=1, close=9.9, volume=1, is_closed=True, close_time=2)
        cache.update(newer)
        self.assertEqual(cache.get("AUSDT").close, 9.9)


class MarketDataWebSocketConstructionTests(unittest.TestCase):
    """생성만으로는(= .start()를 부르지 않는 한) 절대 네트워크 연결이 발생하면 안 된다."""

    def test_construction_does_not_start_connection(self):
        ws = MarketDataWebSocket(api_key="fake", api_secret="fake", testnet=True)
        self.assertFalse(ws._started)
        self.assertIsNone(ws._twm)

    def test_handle_message_updates_cache_and_invokes_callback(self):
        received = []
        ws = MarketDataWebSocket(api_key="fake", api_secret="fake", on_kline=received.append)
        ws._handle_message(sample_msg("CBUSDT"))
        self.assertEqual(len(received), 1)
        self.assertEqual(ws.cache.get("CBUSDT").symbol, "CBUSDT")

    def test_handle_message_swallows_callback_exceptions(self):
        def boom(_k):
            raise RuntimeError("콜백 내부 오류(테스트 모의)")

        ws = MarketDataWebSocket(api_key="fake", api_secret="fake", on_kline=boom)
        try:
            ws._handle_message(sample_msg())
        except Exception as e:
            self.fail(f"_handle_message이 콜백 예외를 밖으로 던지면 안 됨: {e}")

    def test_handle_malformed_message_does_not_raise(self):
        ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
        try:
            ws._handle_message({"garbage": True})
        except Exception as e:
            self.fail(f"형식이 이상한 메시지에도 예외를 던지면 안 됨: {e}")

    def test_stop_without_start_is_a_noop(self):
        ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
        ws.stop()  # 시작한 적 없어도 예외 없이 조용히 넘어가야 함


class UserDataWebSocketConstructionTests(unittest.TestCase):
    def test_construction_does_not_start_connection(self):
        ws = UserDataWebSocket(api_key="fake", api_secret="fake")
        self.assertFalse(ws._started)
        self.assertIsNone(ws._twm)

    def test_handle_message_invokes_callback(self):
        received = []
        ws = UserDataWebSocket(api_key="fake", api_secret="fake", on_account_update=received.append)
        ws._handle_message({"e": "ORDER_TRADE_UPDATE"})
        self.assertEqual(received, [{"e": "ORDER_TRADE_UPDATE"}])

    def test_handle_message_swallows_callback_exceptions(self):
        ws = UserDataWebSocket(api_key="fake", api_secret="fake", on_account_update=lambda _m: 1 / 0)
        try:
            ws._handle_message({"e": "x"})
        except Exception as e:
            self.fail(f"_handle_message이 콜백 예외를 밖으로 던지면 안 됨: {e}")

    def test_stop_without_start_is_a_noop(self):
        ws = UserDataWebSocket(api_key="fake", api_secret="fake")
        ws.stop()


class StartStopWiringTests(unittest.TestCase):
    """실제 네트워크 없이, start()가 ThreadedWebsocketManager를 올바른 인자로 호출하는지만
    검증한다. `binance.ThreadedWebsocketManager` 자체를 가짜로 바꿔치기하므로 실제 소켓
    연결은 이 테스트 전체에서 단 한 번도 발생하지 않는다(진짜 testnet 검증이 필요하면
    별도로 사용자 확인 후 실제 키로 수동 확인해야 함 — 이 테스트는 "배선"만 검증)."""

    def test_market_data_start_uses_single_multiplex_connection_for_small_symbol_count(self):
        """[2026-08-10 실거래 사고 이후 재설계] 심볼마다 개별 소켓을 열던 이전 방식이 250개
        동시 연결에서 handshake 대량 타임아웃을 일으켰다(실거래 로그로 확인) — combined/multiplex
        스트림 하나로 여러 심볼을 묶어서 구독하는 방식으로 바꿔야 한다."""
        fake_twm_instance = MagicMock()
        fake_twm_instance.start_futures_multiplex_socket.side_effect = ["key1"]
        fake_twm_cls = MagicMock(return_value=fake_twm_instance)
        with patch("binance.ThreadedWebsocketManager", fake_twm_cls):
            ws = MarketDataWebSocket(api_key="fake", api_secret="fake", testnet=True)
            ws.start(["AUSDT", "BUSDT"], interval="1m")

        fake_twm_cls.assert_called_once_with(api_key="fake", api_secret="fake", testnet=True)
        fake_twm_instance.start.assert_called_once()
        # 심볼 2개 -> 연결은 딱 1개만 열려야 한다(개별 소켓 250개를 열던 예전 방식으로 회귀 금지)
        self.assertEqual(fake_twm_instance.start_futures_multiplex_socket.call_count, 1)
        streams = fake_twm_instance.start_futures_multiplex_socket.call_args.kwargs["streams"]
        self.assertEqual(streams, ["ausdt_perpetual@continuousKline_1m", "busdt_perpetual@continuousKline_1m"])
        self.assertEqual(ws._stream_keys, ["key1"])
        self.assertTrue(ws._started)

    def test_market_data_start_splits_large_symbol_list_into_multiple_connections(self):
        """250개처럼 한도를 넘는 심볼 수는 여러 connection으로 쪼개져야 한다(연결 하나에
        전부 몰아넣지 않음) — 이게 실제 사고를 일으킨 부분에 대한 핵심 방어."""
        fake_twm_instance = MagicMock()
        fake_twm_instance.start_futures_multiplex_socket.side_effect = ["key1", "key2"]
        fake_twm_cls = MagicMock(return_value=fake_twm_instance)
        symbols = [f"S{i}USDT" for i in range(250)]
        with patch("binance.ThreadedWebsocketManager", fake_twm_cls):
            ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
            ws.start(symbols, interval="1m")

        self.assertEqual(fake_twm_instance.start_futures_multiplex_socket.call_count, 2)
        calls = fake_twm_instance.start_futures_multiplex_socket.call_args_list
        first_chunk = calls[0].kwargs["streams"]
        second_chunk = calls[1].kwargs["streams"]
        self.assertEqual(len(first_chunk), ws.MAX_STREAMS_PER_CONNECTION)
        self.assertEqual(len(second_chunk), 250 - ws.MAX_STREAMS_PER_CONNECTION)
        self.assertEqual(ws._stream_keys, ["key1", "key2"])

    def test_market_data_double_start_raises(self):
        fake_twm_instance = MagicMock()
        fake_twm_cls = MagicMock(return_value=fake_twm_instance)
        with patch("binance.ThreadedWebsocketManager", fake_twm_cls):
            ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
            ws.start(["AUSDT"])
            with self.assertRaises(RuntimeError):
                ws.start(["AUSDT"])

    def test_market_data_stop_calls_twm_stop(self):
        fake_twm_instance = MagicMock()
        fake_twm_cls = MagicMock(return_value=fake_twm_instance)
        with patch("binance.ThreadedWebsocketManager", fake_twm_cls):
            ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
            ws.start(["AUSDT"])
            ws.stop()
        fake_twm_instance.stop.assert_called_once()
        self.assertFalse(ws._started)

    def test_user_data_start_calls_user_socket(self):
        fake_twm_instance = MagicMock()
        fake_twm_cls = MagicMock(return_value=fake_twm_instance)
        with patch("binance.ThreadedWebsocketManager", fake_twm_cls):
            ws = UserDataWebSocket(api_key="fake", api_secret="fake", testnet=True)
            ws.start()

        fake_twm_cls.assert_called_once_with(api_key="fake", api_secret="fake", testnet=True)
        fake_twm_instance.start.assert_called_once()
        fake_twm_instance.start_futures_user_socket.assert_called_once()
        self.assertTrue(ws._started)


def order_trade_update_msg(symbol="TESTUSDT", exec_type="TRADE", status="FILLED", side="BUY",
                            avg_price="1.2345", commission="0.01", realized_pnl="0.5"):
    return {
        "e": "ORDER_TRADE_UPDATE", "E": 1700000000000,
        "o": {
            "s": symbol, "S": side, "x": exec_type, "X": status,
            "ap": avg_price, "n": commission, "N": "USDT", "rp": realized_pnl,
            "q": "12.0", "l": "12.0", "i": 12345, "c": "client-1",
        },
    }


class ParseOrderTradeUpdateTests(unittest.TestCase):
    def test_parses_actual_trade_execution(self):
        fill = parse_order_trade_update(order_trade_update_msg())
        self.assertIsNotNone(fill)
        self.assertEqual(fill.symbol, "TESTUSDT")
        self.assertEqual(fill.avg_price, 1.2345)
        self.assertEqual(fill.commission, 0.01)
        self.assertEqual(fill.commission_asset, "USDT")
        self.assertEqual(fill.realized_pnl, 0.5)

    def test_non_trade_execution_type_is_ignored(self):
        """x != "TRADE"(예: 신규접수/취소 등 실제 체결이 아닌 상태변경)는 무시해야 한다 —
        체결 안 된 이벤트에서 avg_price=0 같은 값을 잘못 채워 넣으면 원장이 오염된다."""
        msg = order_trade_update_msg(exec_type="NEW")
        self.assertIsNone(parse_order_trade_update(msg))

    def test_non_order_trade_update_event_is_ignored(self):
        self.assertIsNone(parse_order_trade_update({"e": "ACCOUNT_UPDATE"}))

    def test_malformed_message_returns_none_without_raising(self):
        self.assertIsNone(parse_order_trade_update({"e": "ORDER_TRADE_UPDATE"}))  # "o" 없음
        self.assertIsNone(parse_order_trade_update({"e": "ORDER_TRADE_UPDATE", "o": {}}))


class FillTrackerTests(unittest.TestCase):
    def test_records_and_retrieves_latest_fill_per_symbol(self):
        tracker = FillTracker()
        tracker.on_message(order_trade_update_msg("AUSDT", avg_price="10.0"))
        tracker.on_message(order_trade_update_msg("BUSDT", avg_price="20.0"))
        self.assertEqual(tracker.get("AUSDT").avg_price, 10.0)
        self.assertEqual(tracker.get("BUSDT").avg_price, 20.0)

    def test_later_fill_overwrites_earlier_for_same_symbol(self):
        tracker = FillTracker()
        tracker.on_message(order_trade_update_msg("AUSDT", avg_price="10.0"))
        tracker.on_message(order_trade_update_msg("AUSDT", avg_price="11.0"))
        self.assertEqual(tracker.get("AUSDT").avg_price, 11.0)

    def test_non_trade_message_does_not_overwrite_existing_fill(self):
        tracker = FillTracker()
        tracker.on_message(order_trade_update_msg("AUSDT", avg_price="10.0"))
        tracker.on_message(order_trade_update_msg("AUSDT", exec_type="NEW", avg_price="99.0"))
        self.assertEqual(tracker.get("AUSDT").avg_price, 10.0)

    def test_get_missing_symbol_returns_none(self):
        tracker = FillTracker()
        self.assertIsNone(tracker.get("NOPEUSDT"))

    def test_clear_removes_stored_fill(self):
        tracker = FillTracker()
        tracker.on_message(order_trade_update_msg("AUSDT"))
        tracker.clear("AUSDT")
        self.assertIsNone(tracker.get("AUSDT"))

    def test_malformed_message_does_not_raise(self):
        tracker = FillTracker()
        try:
            tracker.on_message({"garbage": True})
        except Exception as e:
            self.fail(f"on_message이 예외를 던지면 안 됨: {e}")

    def test_optional_jsonl_event_log_records_normalized_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = FillTracker(log_events=True, event_log_dir=Path(tmp))
            tracker.on_message(order_trade_update_msg("AUSDT", side="SELL", avg_price="10.5"))
            files = list(Path(tmp).glob("fill_events_*.jsonl"))
            self.assertEqual(len(files), 1)
            rows = files[0].read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(rows), 1)
            row = json.loads(rows[0])
            self.assertEqual(row["symbol"], "AUSDT")
            self.assertEqual(row["side"], "SELL")
            self.assertEqual(row["avg_price"], 10.5)
            self.assertEqual(row["quantity"], 12.0)
            self.assertEqual(row["order_id"], 12345)


def rest_shaped_df(n=5, start_price=100.0):
    import pandas as pd
    rows = []
    for i in range(n):
        rows.append({
            "open_time": pd.Timestamp(1700000000000 + i * 60000, unit="ms"), "open": start_price + i,
            "high": start_price + i + 1, "low": start_price + i - 1, "close": start_price + i + 0.5,
            "volume": 10.0 + i, "close_time": 1700000059999 + i * 60000,
            "quote_asset_volume": 1000.0, "num_trades": 5, "taker_buy_base": 3.0,
            "taker_buy_quote": 300.0, "ignore": 0,
        })
    return pd.DataFrame(rows)


class RollingKlineCacheTests(unittest.TestCase):
    def test_seed_then_to_dataframe_roundtrip(self):
        cache = RollingKlineCache(max_len=200)
        cache.seed("AUSDT", rest_shaped_df(5))
        df = cache.to_dataframe("AUSDT")
        self.assertEqual(len(df), 5)
        self.assertEqual(list(df.columns), RollingKlineCache._COLUMNS)

    def test_to_dataframe_open_time_is_correct_calendar_date_not_1970(self):
        """[2026-08-10 테스트넷 실증 회귀테스트] to_dataframe()이 open_time을 ms 대신
        ns로 잘못 해석하면 1970년대 날짜가 나온다 — 실제 이 버그가 있었음."""
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(3))  # open_time 기준시각 2023-11-14 근처(ms epoch)
        df = cache.to_dataframe("AUSDT")
        self.assertGreaterEqual(df.iloc[0]["open_time"].year, 2020)

    def test_to_dataframe_open_time_matches_seeded_source_value(self):
        cache = RollingKlineCache()
        src = rest_shaped_df(3)
        cache.seed("AUSDT", src)
        df = cache.to_dataframe("AUSDT")
        self.assertEqual(df.iloc[0]["open_time"], src.iloc[0]["open_time"])

    def test_missing_symbol_returns_none(self):
        cache = RollingKlineCache()
        self.assertIsNone(cache.to_dataframe("NOPEUSDT"))

    def test_has_sufficient_history_respects_min_len(self):
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(5))
        self.assertTrue(cache.has_sufficient_history("AUSDT", 5))
        self.assertFalse(cache.has_sufficient_history("AUSDT", 6))

    def test_append_closed_appends_new_candle(self):
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(3))
        k = KlineUpdate(symbol="AUSDT", open_time=999999999999, open=1, high=2, low=0.5, close=1.5,
                         volume=10, is_closed=True, close_time=999999999999 + 59999)
        cache.append_closed(k)
        df = cache.to_dataframe("AUSDT")
        self.assertEqual(len(df), 4)
        self.assertEqual(df.iloc[-1]["close"], 1.5)

    def test_append_open_candle_is_ignored(self):
        """확정 안 된(is_closed=False) 캔들은 REST get_klines()의 "마지막 미완성 캔들 제외"
        관례와 맞추기 위해 절대 캐시에 들어가면 안 된다."""
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(3))
        k = KlineUpdate(symbol="AUSDT", open_time=999999999999, open=1, high=2, low=0.5, close=1.5,
                         volume=10, is_closed=False, close_time=999999999999 + 59999)
        cache.append_closed(k)
        self.assertEqual(len(cache.to_dataframe("AUSDT")), 3)

    def test_is_fresh_true_right_after_seed(self):
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(3))
        self.assertTrue(cache.is_fresh("AUSDT", max_age_sec=60))

    def test_is_fresh_true_right_after_append(self):
        cache = RollingKlineCache()
        k = KlineUpdate(symbol="AUSDT", open_time=1, open=1, high=1, low=1, close=1, volume=1,
                         is_closed=True, close_time=60000)
        cache.append_closed(k)
        self.assertTrue(cache.is_fresh("AUSDT", max_age_sec=60))

    def test_is_fresh_false_for_never_updated_symbol(self):
        cache = RollingKlineCache()
        self.assertFalse(cache.is_fresh("NEVERUSDT", max_age_sec=999))

    def test_is_fresh_false_once_max_age_exceeded(self):
        """[2026-08-10 실거래 사고 회귀테스트] WS 연결이 죽어도 캐시엔 예전 데이터가 남아있어
        "개수는 충분함"만으로는 판단하면 안 된다 — 갱신된 지 오래됐으면 반드시 False가 나와야
        get_klines()가 REST로 안전하게 폴백한다."""
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(3))
        with patch("bot.ws_client.time.time", return_value=cache._last_update_ts["AUSDT"] + 999):
            self.assertFalse(cache.is_fresh("AUSDT", max_age_sec=150))

    def test_duplicate_open_time_overwrites_last_row_instead_of_duplicating(self):
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(3))
        last_open_time = int(rest_shaped_df(3).iloc[-1]["open_time"].value // 10 ** 6)
        k = KlineUpdate(symbol="AUSDT", open_time=last_open_time, open=1, high=2, low=0.5, close=42.0,
                         volume=10, is_closed=True, close_time=last_open_time + 59999)
        cache.append_closed(k)
        df = cache.to_dataframe("AUSDT")
        self.assertEqual(len(df), 3)  # 늘어나지 않아야 함
        self.assertEqual(df.iloc[-1]["close"], 42.0)

    def test_max_len_trims_oldest_rows(self):
        cache = RollingKlineCache(max_len=3)
        cache.seed("AUSDT", rest_shaped_df(5))
        self.assertEqual(len(cache.to_dataframe("AUSDT")), 3)


class SharedManagerWiringTests(unittest.TestCase):
    """[2026-08-10 두 번째 실거래 사고 회귀테스트] MarketDataWebSocket과 UserDataWebSocket이
    각자 별도의 ThreadedWebsocketManager를 만들어 "동시에" 띄우면 내부 이벤트루프가 충돌하는
    실제 사고가 있었다 — twm을 외부에서 공유 주입하면 새 매니저를 만들지 않아야 하고,
    stop()도 공유 매니저는 건드리면 안 된다(다른 스트림이 아직 쓰고 있을 수 있으므로)."""

    def test_market_data_uses_injected_twm_without_creating_new_one(self):
        fake_twm = MagicMock()
        fake_twm.start_futures_multiplex_socket.return_value = "key1"
        # binance.ThreadedWebsocketManager 자체를 부르면(=새로 만들면) 즉시 실패하게 만들어서,
        # twm이 주입됐을 때 정말 새로 안 만드는지 검증한다.
        with patch("binance.ThreadedWebsocketManager", side_effect=AssertionError("새 매니저를 만들면 안 됨")):
            ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
            ws.start(["AUSDT"], interval="1m", twm=fake_twm)

        fake_twm.start.assert_not_called()  # 주입된 매니저는 이미 시작된 것으로 취급, 다시 start() 안 함
        fake_twm.start_futures_multiplex_socket.assert_called_once()
        self.assertTrue(ws._started)

    def test_market_data_stop_does_not_stop_injected_shared_twm(self):
        fake_twm = MagicMock()
        fake_twm.start_futures_multiplex_socket.return_value = "key1"
        with patch("binance.ThreadedWebsocketManager", side_effect=AssertionError("새 매니저를 만들면 안 됨")):
            ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
            ws.start(["AUSDT"], twm=fake_twm)
            ws.stop()
        fake_twm.stop.assert_not_called()  # 공유 매니저는 여기서 stop하면 안 됨(다른 스트림이 쓸 수 있음)

    def test_user_data_uses_injected_twm_without_creating_new_one(self):
        fake_twm = MagicMock()
        with patch("binance.ThreadedWebsocketManager", side_effect=AssertionError("새 매니저를 만들면 안 됨")):
            ws = UserDataWebSocket(api_key="fake", api_secret="fake")
            ws.start(twm=fake_twm)

        fake_twm.start.assert_not_called()
        fake_twm.start_futures_user_socket.assert_called_once()
        self.assertTrue(ws._started)

    def test_market_data_and_user_data_can_share_same_twm_simultaneously(self):
        """실제 사고 재현 시나리오: 시장데이터+계정스트림을 "같은" 매니저 하나로 동시에 띄운다.
        각자 독립 매니저를 만들던 예전 방식이면 여기서 두 번째 ThreadedWebsocketManager() 생성이
        일어나야 하는데, side_effect로 그걸 막아뒀으니 만약 코드가 여전히 새 매니저를 만들려 하면
        이 테스트가 실패한다."""
        fake_twm = MagicMock()
        fake_twm.start_futures_multiplex_socket.return_value = "key1"
        with patch("binance.ThreadedWebsocketManager", side_effect=AssertionError("새 매니저를 만들면 안 됨")):
            market_ws = MarketDataWebSocket(api_key="fake", api_secret="fake")
            market_ws.start(["AUSDT", "BUSDT"], twm=fake_twm)
            user_ws = UserDataWebSocket(api_key="fake", api_secret="fake")
            user_ws.start(twm=fake_twm)

        self.assertTrue(market_ws._started)
        self.assertTrue(user_ws._started)
        fake_twm.start_futures_multiplex_socket.assert_called_once()
        fake_twm.start_futures_user_socket.assert_called_once()


if __name__ == "__main__":
    unittest.main()
