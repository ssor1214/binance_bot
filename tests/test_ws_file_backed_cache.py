"""[2026-08-10] FileBackedKlineCache/FileBackedFillTracker 단위테스트 — bot/ws_worker.py가
남기는 캐시 파일을 메인 프로세스가 읽는 쪽(read-only 어댑터). 실제 서브프로세스를 띄우지
않고, 임시 디렉터리에 ws_worker.py와 동일한 형식의 파일을 직접 써서 검증한다."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ws_client import FileBackedFillTracker, FileBackedKlineCache


def write_cache_file(cache_path, heartbeat_path, rows_by_symbol=None, last_update_ts=None, fills=None, heartbeat_ts=None):
    cache_path.write_text(json.dumps({
        "dumped_at": time.time(),
        "rows_by_symbol": rows_by_symbol or {},
        "last_update_ts": last_update_ts or {},
        "fills": fills or {},
    }), encoding="utf-8")
    heartbeat_path.write_text(str(heartbeat_ts if heartbeat_ts is not None else time.time()), encoding="utf-8")


SAMPLE_ROW = {
    "open_time": 1700000000000, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
    "volume": 10.0, "close_time": 1700000059999, "quote_asset_volume": 100.0,
    "num_trades": 5, "taker_buy_base": 3.0, "taker_buy_quote": 30.0, "ignore": 0,
}


class FileBackedKlineCacheTests(unittest.TestCase):
    def test_returns_false_when_heartbeat_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileBackedKlineCache(Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt")
            self.assertFalse(cache.has_sufficient_history("BTCUSDT", 1))
            self.assertFalse(cache.is_fresh("BTCUSDT", 999))
            self.assertIsNone(cache.to_dataframe("BTCUSDT"))

    def test_reads_valid_cache_file_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path,
                              rows_by_symbol={"BTCUSDT": [SAMPLE_ROW, SAMPLE_ROW]},
                              last_update_ts={"BTCUSDT": time.time()})
            cache = FileBackedKlineCache(cache_path, hb_path)
            self.assertTrue(cache.has_sufficient_history("BTCUSDT", 2))
            self.assertFalse(cache.has_sufficient_history("BTCUSDT", 3))
            self.assertTrue(cache.is_fresh("BTCUSDT", 60))
            df = cache.to_dataframe("BTCUSDT")
            self.assertEqual(len(df), 2)

    def test_worker_heartbeat_stale_makes_everything_unsafe_even_with_fresh_looking_data(self):
        """[2026-08-10 실거래 사고 회귀테스트] 핵심 안전장치 — 워커 프로세스 자체가
        죽어있으면(하트비트가 오래됨), 캐시 파일에 남은 last_update_ts가 최근처럼 보여도
        절대 신뢰하면 안 된다. 워커가 죽은 시점의 마지막 값이 우연히 최근이었을 수 있기
        때문에, 반드시 "워커가 지금 살아있는가"를 하트비트로 별도 확인해야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path,
                              rows_by_symbol={"BTCUSDT": [SAMPLE_ROW]},
                              last_update_ts={"BTCUSDT": time.time()},  # 데이터상으론 방금 갱신된 것처럼 보임
                              heartbeat_ts=time.time() - 999)  # 하지만 워커 하트비트는 오래전에 멈춤
            cache = FileBackedKlineCache(cache_path, hb_path, worker_max_staleness_sec=30.0)
            self.assertFalse(cache.is_fresh("BTCUSDT", 999))
            self.assertFalse(cache.has_sufficient_history("BTCUSDT", 1))
            self.assertIsNone(cache.to_dataframe("BTCUSDT"))

    def test_missing_symbol_in_cache_file_returns_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path, rows_by_symbol={"ETHUSDT": [SAMPLE_ROW]})
            cache = FileBackedKlineCache(cache_path, hb_path)
            self.assertFalse(cache.has_sufficient_history("BTCUSDT", 1))
            self.assertIsNone(cache.to_dataframe("BTCUSDT"))

    def test_corrupted_json_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            cache_path.write_text("not valid json{{{", encoding="utf-8")
            hb_path.write_text(str(time.time()), encoding="utf-8")
            cache = FileBackedKlineCache(cache_path, hb_path)
            try:
                self.assertFalse(cache.has_sufficient_history("BTCUSDT", 1))
                self.assertIsNone(cache.to_dataframe("BTCUSDT"))
            except Exception as e:
                self.fail(f"손상된 파일에도 예외를 던지면 안 됨: {e}")


class FileBackedFillTrackerTests(unittest.TestCase):
    def test_returns_none_when_no_fill_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path)
            tracker = FileBackedFillTracker(cache_path, hb_path)
            self.assertIsNone(tracker.get("BTCUSDT"))

    def test_returns_fill_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path, fills={"BTCUSDT": {
                "symbol": "BTCUSDT", "side": "SELL", "avg_price": 65000.0, "commission": 0.5,
                "commission_asset": "USDT", "realized_pnl": 10.0, "order_status": "FILLED",
                "event_time_ms": 1700000000000,
            }})
            tracker = FileBackedFillTracker(cache_path, hb_path)
            fill = tracker.get("BTCUSDT")
            self.assertEqual(fill.avg_price, 65000.0)

    def test_stale_worker_heartbeat_hides_fill_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path, fills={"BTCUSDT": {
                "symbol": "BTCUSDT", "side": "SELL", "avg_price": 65000.0, "commission": 0.5,
                "commission_asset": "USDT", "realized_pnl": 10.0, "order_status": "FILLED",
                "event_time_ms": 1700000000000,
            }}, heartbeat_ts=time.time() - 999)
            tracker = FileBackedFillTracker(cache_path, hb_path, worker_max_staleness_sec=30.0)
            self.assertIsNone(tracker.get("BTCUSDT"))


class FileBackedKlineCacheMtimeCachingTests(unittest.TestCase):
    """[2026-08-10 실측 발견] get_klines() 한 번에 has_sufficient_history/is_fresh/
    to_dataframe이 각각 _read()를 호출해, 심볼 1개당 캐시파일을 3번씩 디스크에서 다시
    읽고 JSON 파싱했다(250심볼 스캔이면 사이클당 750번의 파일 I/O) — 테스트넷 실측 결과
    REST 대비 속도 이점이 1.2배에 불과했던 원인. mtime이 그대로면 디스크를 다시 읽지 않고
    메모리 캐시를 재사용해야 한다."""

    def test_does_not_reread_file_when_mtime_unchanged(self):
        """캐싱의 핵심 효과(디스크 재파싱 회피)를 json.loads 호출 횟수로 검증한다 —
        has_sufficient_history/is_fresh/to_dataframe 세 번을 호출해도 실제 JSON 파싱은
        1회만 일어나야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path,
                              rows_by_symbol={"BTCUSDT": [SAMPLE_ROW, SAMPLE_ROW]},
                              last_update_ts={"BTCUSDT": time.time()})
            cache = FileBackedKlineCache(cache_path, hb_path)
            with patch("json.loads", wraps=json.loads) as spy:
                cache.has_sufficient_history("BTCUSDT", 2)
                cache.is_fresh("BTCUSDT", 60)
                cache.to_dataframe("BTCUSDT")
                self.assertEqual(spy.call_count, 1)

    def test_rereads_file_when_mtime_changes(self):
        """워커가 파일을 새로 갱신하면(mtime 변경) 다음 조회 때 새 내용을 반영해야 한다 —
        캐싱이 오래된 데이터를 영구히 고정시키면 안 된다."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_path, hb_path = Path(tmp) / "cache.json", Path(tmp) / "heartbeat.txt"
            write_cache_file(cache_path, hb_path,
                              rows_by_symbol={"BTCUSDT": [SAMPLE_ROW]},
                              last_update_ts={"BTCUSDT": time.time()})
            cache = FileBackedKlineCache(cache_path, hb_path)
            self.assertTrue(cache.has_sufficient_history("BTCUSDT", 1))
            self.assertFalse(cache.has_sufficient_history("BTCUSDT", 2))

            time.sleep(0.05)  # 일부 파일시스템은 mtime 해상도가 낮아 약간의 간격을 둠
            write_cache_file(cache_path, hb_path,
                              rows_by_symbol={"BTCUSDT": [SAMPLE_ROW, SAMPLE_ROW]},
                              last_update_ts={"BTCUSDT": time.time()})
            self.assertTrue(cache.has_sufficient_history("BTCUSDT", 2))  # 갱신된 내용 반영됨


if __name__ == "__main__":
    unittest.main()
