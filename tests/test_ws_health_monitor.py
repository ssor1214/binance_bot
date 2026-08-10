"""[2026-08-10, Codex 제안 반영] WsHealthMonitor / FileBackedKlineCache.health() 단위테스트.
"프로세스가 살아서 하트비트 파일을 쓰고 있다"와 "실제로 시장데이터를 수신하고 있다"는
다르다는 문제의식에서 추가됨 — 실 API/실 소켓을 절대 열지 않고, 로거에 직접 레코드를
주입하는 방식으로 "Read loop has been closed" 감지 로직을 검증한다."""
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ws_client import FileBackedKlineCache, MarketDataWebSocket, WsHealthMonitor


class WsHealthMonitorTests(unittest.TestCase):
    def test_snapshot_defaults_to_zero_when_nothing_happened(self):
        health = WsHealthMonitor()
        snap = health.snapshot()
        self.assertEqual(snap["last_market_message_ts"], 0.0)
        self.assertEqual(snap["message_count_60s"], 0)
        self.assertEqual(snap["error_count_60s"], 0)
        self.assertEqual(snap["consecutive_read_loop_errors"], 0)

    def test_note_message_updates_last_ts_and_count(self):
        health = WsHealthMonitor()
        before = time.time()
        health.note_message()
        health.note_message()
        snap = health.snapshot()
        self.assertGreaterEqual(snap["last_market_message_ts"], before)
        self.assertEqual(snap["message_count_60s"], 2)

    def test_note_latency_tracked_in_snapshot(self):
        """[2026-08-10 사용자요청] "웹소켓=실시간"이라는 착각 경계 — 메시지 이벤트시각과
        수신시각의 차이를 조기경보 지표로 기록한다(데이터 자체는 버리지 않음)."""
        health = WsHealthMonitor()
        health.note_latency(50.0)
        health.note_latency(300.0)
        health.note_latency(100.0)
        snap = health.snapshot()
        self.assertEqual(snap["max_latency_ms_recent"], 300.0)
        self.assertAlmostEqual(snap["avg_latency_ms_recent"], 150.0)

    def test_latency_defaults_to_zero_when_no_samples(self):
        health = WsHealthMonitor()
        snap = health.snapshot()
        self.assertEqual(snap["max_latency_ms_recent"], 0.0)
        self.assertEqual(snap["avg_latency_ms_recent"], 0.0)

    def test_read_loop_closed_log_increments_consecutive_error_count(self):
        """[핵심] python-binance 내부 로거(binance.ws.threaded_stream)가 "Read loop has
        been closed" 에러를 로깅하면, 우리 콜백은 전혀 호출 안 되지만 이 카운터는 반드시
        올라가야 한다 — 실제 라이브러리를 실행하지 않고 로거에 직접 레코드를 만들어 검증."""
        health = WsHealthMonitor()
        logger = logging.getLogger("binance.ws.threaded_stream")
        logger.error("Error receiving message: Read loop has been closed, please reset the websocket connection.")
        logger.error("Error receiving message: Read loop has been closed, please reset the websocket connection.")
        snap = health.snapshot()
        self.assertEqual(snap["consecutive_read_loop_errors"], 2)
        self.assertEqual(snap["error_count_60s"], 2)

    def test_queue_overflow_log_also_counts_as_error(self):
        health = WsHealthMonitor()
        logger = logging.getLogger("binance.ws.reconnecting_websocket")
        logger.error("Unknown exception: BinanceWebsocketQueueOverflow (Message queue size 100 exceeded maximum 100)")
        self.assertEqual(health.snapshot()["consecutive_read_loop_errors"], 1)

    def test_successful_message_resets_consecutive_error_count(self):
        """read loop가 회복돼서(재연결 성공) 정상 메시지가 다시 들어오면, 연속 에러
        카운트는 0으로 리셋돼야 한다 — 그래야 재시작 판단이 "지금 살아있는지"를 반영한다."""
        health = WsHealthMonitor()
        logger = logging.getLogger("binance.ws.threaded_stream")
        logger.error("Error receiving message: Read loop has been closed")
        logger.error("Error receiving message: Read loop has been closed")
        self.assertEqual(health.snapshot()["consecutive_read_loop_errors"], 2)

        health.note_message()  # 정상 메시지 수신 = 회복
        self.assertEqual(health.snapshot()["consecutive_read_loop_errors"], 0)

    def test_unrelated_error_logs_are_ignored(self):
        health = WsHealthMonitor()
        logging.getLogger("binance.ws.threaded_stream").error("어떤 상관없는 다른 에러 메시지")
        self.assertEqual(health.snapshot()["consecutive_read_loop_errors"], 0)


class MarketDataWebSocketHealthWiringTests(unittest.TestCase):
    def test_handle_message_notes_health_when_injected(self):
        health = WsHealthMonitor()
        ws = MarketDataWebSocket(api_key="x", api_secret="y", health=health)
        # parse_futures_kline_message가 이해 못 하는 형식이라도(리턴 None) health는 반드시
        # 기록돼야 한다 — "메시지가 도착했다"는 사실 자체는 파싱 성공 여부와 무관하다.
        ws._handle_message({"not": "a valid kline payload"})
        self.assertGreater(health.snapshot()["last_market_message_ts"], 0.0)

    def test_no_health_injected_does_not_raise(self):
        ws = MarketDataWebSocket(api_key="x", api_secret="y")  # health=None(기본값)
        try:
            ws._handle_message({"not": "a valid kline payload"})
        except Exception as e:
            self.fail(f"health 미주입 상태에서 예외가 나면 안 됨: {e}")

    def test_handle_message_records_latency_from_event_time_field(self):
        health = WsHealthMonitor()
        ws = MarketDataWebSocket(api_key="x", api_secret="y", health=health)
        old_event_time_ms = int(time.time() * 1000) - 250  # 250ms 전에 발생한 이벤트
        ws._handle_message({"data": {"E": old_event_time_ms, "k": {}}})
        snap = health.snapshot()
        self.assertGreater(snap["max_latency_ms_recent"], 200.0)  # 최소 200ms 이상 지연 기록돼야 함

    def test_handle_message_missing_event_time_does_not_raise(self):
        health = WsHealthMonitor()
        ws = MarketDataWebSocket(api_key="x", api_secret="y", health=health)
        try:
            ws._handle_message({"not": "a valid kline payload"})  # "E" 필드 자체가 없음
        except Exception as e:
            self.fail(f"E 필드 누락시 예외가 나면 안 됨: {e}")
        self.assertEqual(health.snapshot()["max_latency_ms_recent"], 0.0)


class FileBackedKlineCacheHealthTests(unittest.TestCase):
    def test_returns_health_dict_when_present_in_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            cache_path.write_text(json.dumps({
                "dumped_at": time.time(), "rows_by_symbol": {}, "last_update_ts": {}, "fills": {},
                "health": {"last_market_message_ts": 123.0, "message_count_60s": 5,
                           "error_count_60s": 0, "consecutive_read_loop_errors": 0},
            }), encoding="utf-8")
            hb_path.write_text(str(time.time()), encoding="utf-8")
            cache = FileBackedKlineCache(cache_path, hb_path)
            self.assertEqual(cache.health()["message_count_60s"], 5)

    def test_returns_none_when_health_field_missing(self):
        """구버전 워커가 남긴 캐시 파일(health 키 없음)을 새 코드가 읽어도 죽지 않고
        None을 반환해야 한다 — 하위호환."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            cache_path.write_text(json.dumps({
                "dumped_at": time.time(), "rows_by_symbol": {}, "last_update_ts": {}, "fills": {},
            }), encoding="utf-8")
            hb_path.write_text(str(time.time()), encoding="utf-8")
            cache = FileBackedKlineCache(cache_path, hb_path)
            self.assertIsNone(cache.health())

    def test_returns_none_when_worker_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            cache_path.write_text(json.dumps({
                "dumped_at": time.time(), "rows_by_symbol": {}, "last_update_ts": {}, "fills": {},
                "health": {"last_market_message_ts": 123.0},
            }), encoding="utf-8")
            hb_path.write_text(str(time.time() - 999), encoding="utf-8")  # 하트비트 오래됨
            cache = FileBackedKlineCache(cache_path, hb_path)
            self.assertIsNone(cache.health())


if __name__ == "__main__":
    unittest.main()
