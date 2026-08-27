"""bot/ws_trade_worker.py 단위테스트 — 실제 소켓 연결 없이 파일 IO/상태 스냅샷 로직만 검증.
run()은 네트워크에 의존하므로 여기서 실행하지 않는다(soak/qa 스크립트에서 별도 검증).

[2026-08-19 시그니처 변경 반영] dump_status()가 cfg를 인자로 받게 바뀌었다. 이전에는
detect_volume_spike()를 **기본값으로** 호출해서 .env의 SPIKE_ENTRY_MULTIPLIER/WINDOW/
BASELINE이 워커에 전혀 반영되지 않고 있었다(현재 설정이 마침 기본값과 같아 실해는 없었음).
같은 날 발견한 "부분충전 baseline 오탐"(tests/test_spike_partial_baseline.py 참고)을 고치면서
설정 경로도 함께 정리했다. 그래서 이 테스트도 cfg를 넘기도록 갱신한다."""
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import ws_trade_worker as w
from bot.config import Config
from bot.ws_trade_client import TradeTick


class DumpStatusTest(unittest.TestCase):
    def setUp(self):
        self._orig_status = w.WS_STATUS_PATH
        self._orig_hb = w.WS_HEARTBEAT_PATH
        self.tmp_status = Path(w.LOG_DIR) / "__test_ws_trade_worker_status.json"
        self.tmp_hb = Path(w.LOG_DIR) / "__test_ws_trade_worker_heartbeat.txt"
        w.WS_STATUS_PATH = self.tmp_status
        w.WS_HEARTBEAT_PATH = self.tmp_hb

    def tearDown(self):
        w.WS_STATUS_PATH = self._orig_status
        w.WS_HEARTBEAT_PATH = self._orig_hb
        for p in (self.tmp_status, self.tmp_hb, self.tmp_status.with_suffix(".tmp")):
            if p.exists():
                p.unlink()

    def test_dump_status_writes_status_and_heartbeat(self):
        ws = MagicMock()
        ws.cache.get_recent.return_value = [
            TradeTick("BTCUSDT", 100.0, 1.0, 1000, 1000, False),
        ]
        health = MagicMock()
        health.snapshot.return_value = {"last_market_message_ts": 123.0, "error_count_60s": 0}

        w.dump_status(["BTCUSDT"], ws, health, Config())

        self.assertTrue(self.tmp_status.exists())
        self.assertTrue(self.tmp_hb.exists())
        payload = json.loads(self.tmp_status.read_text(encoding="utf-8"))
        self.assertEqual(payload["role"], "trade")
        self.assertEqual(payload["symbol_count"], 1)
        self.assertIn("BTCUSDT", payload["spikes"])
        self.assertIn("health", payload)

    def test_dump_status_swallows_exceptions(self):
        ws = MagicMock()
        ws.cache.get_recent.side_effect = RuntimeError("boom")
        health = MagicMock()
        health.snapshot.side_effect = RuntimeError("boom")
        try:
            w.dump_status(["BTCUSDT"], ws, health, Config())
        except Exception as e:
            self.fail(f"dump_status must never raise, got {e!r}")


class AtomicWriteJsonTest(unittest.TestCase):
    def test_writes_valid_json_and_replaces_atomically(self):
        path = Path(w.LOG_DIR) / "__test_ws_trade_worker_atomic.json"
        try:
            w._atomic_write_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
            w._atomic_write_json(path, {"a": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2})
        finally:
            if path.exists():
                path.unlink()
            tmp = path.with_suffix(".tmp")
            if tmp.exists():
                tmp.unlink()


class BuildTradeStreamTest(unittest.TestCase):
    def _cfg(self, use_v2: bool):
        cfg = MagicMock()
        cfg.ws_trade_use_v2 = use_v2
        cfg.api_key = "k"
        cfg.api_secret = "s"
        cfg.use_testnet = True
        return cfg

    def test_build_trade_stream_defaults_to_python_binance_path(self):
        ws = w._build_trade_stream(self._cfg(False), MagicMock())
        self.assertIsInstance(ws, w.TradeStreamWebSocket)

    def test_build_trade_stream_can_opt_in_to_v2(self):
        ws = w._build_trade_stream(self._cfg(True), MagicMock())
        self.assertIsInstance(ws, w.TradeStreamWebSocketV2)


if __name__ == "__main__":
    unittest.main()


# ===================================== V2 무음 원인 (실사고 회귀)
class V2SilentDeathRegressionTests(unittest.TestCase):
    """[2026-08-19 21:11~21:34] V2 가 23분간 데이터 0건이 됐다.

    로그 실측 패턴: 연결 성립 -> **매번 40~45초 뒤** ConnectionClosedError
    (1011 keepalive ping timeout) -> 재접속마저 handshake timeout 으로 밀림.
    ping_interval=20 / ping_timeout=20 이면 20초에 ping, 20초 내 pong 없으면
    끊는다 — 40~45초와 정확히 일치한다. 바이낸스가 클라이언트발 ping 에
    pong 을 주지 않은 것이다.

    URL(/market/stream)은 정상이었다. 2026-08-21 프로브 실측:
      /market/stream -> 12초에 509~681건 O
      /stream, /ws   -> 연결은 되나 0건 (이쪽이 진짜 '무음' 라우팅)
    """

    def test_클라이언트_ping을_보내지_않는다(self):
        import inspect
        from bot.ws_trade_client import TradeStreamWebSocketV2
        src = inspect.getsource(TradeStreamWebSocketV2._run_connection_loop)
        # 주석에 원인 설명으로 옛 값이 적혀 있으므로 실제 코드 줄만 본다
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        code = chr(10).join(code_lines)
        self.assertIn("ping_interval=None", code,
                      "클라이언트 ping 이 살아 있으면 40초마다 끊긴다")
        self.assertNotIn("ping_timeout=", code,
                         "ping_timeout 이 남아 있으면 1011 사망이 재발한다")

    def test_무수신_감지는_recv_timeout이_담당한다(self):
        """ping 을 끈 대신 생존 감지가 사라지면 안 된다."""
        from bot.ws_trade_client import TradeStreamWebSocketV2
        import inspect
        self.assertGreater(TradeStreamWebSocketV2.RECV_TIMEOUT_SEC, 0)
        self.assertLessEqual(TradeStreamWebSocketV2.RECV_TIMEOUT_SEC, 120,
                             "무수신 감지가 너무 느리면 무음을 오래 못 잡는다")
        src = inspect.getsource(TradeStreamWebSocketV2._run_connection_loop)
        self.assertIn("RECV_TIMEOUT_SEC", src)

    def test_메인넷_url은_market_stream_라우팅이다(self):
        """/stream, /ws 는 연결은 되지만 0건이 온다(실측). 되돌리면 무음이 재발한다."""
        from bot.ws_trade_client import TradeStreamWebSocketV2
        ws = TradeStreamWebSocketV2.__new__(TradeStreamWebSocketV2)
        ws.testnet = False
        url = ws._stream_url(["btcusdt@aggTrade"])
        self.assertIn("/market/stream?streams=", url)
        self.assertTrue(url.startswith("wss://fstream.binance.com/"))

    def test_테스트넷_url은_그대로다(self):
        from bot.ws_trade_client import TradeStreamWebSocketV2
        ws = TradeStreamWebSocketV2.__new__(TradeStreamWebSocketV2)
        ws.testnet = True
        self.assertIn("stream.binancefuture.com/stream?streams=",
                      ws._stream_url(["btcusdt@aggTrade"]))


class TradeWorkerDuplicateGuardTests(unittest.TestCase):
    """[2026-08-21] 체결 워커에는 중복 기동 방지가 없었다.

    같은 샤드가 여러 개 뜨면 같은 스트림에 커넥션을 중복으로 열어
    바이낸스 커넥션 제한에 걸리고 전부 handshake timeout 으로 밀린다.
    실수로 3개를 띄웠을 때 로그에 connected=3 이 찍혀 확인됐다.
    """

    def setUp(self):
        from bot import ws_trade_worker as w
        self.w = w
        self._orig = w.WORKER_PID_FILE
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        w.WORKER_PID_FILE = Path(self._tmp.name) / "pid.json"
        self.addCleanup(lambda: setattr(w, "WORKER_PID_FILE", self._orig))

    def test_첫_기동은_락을_얻는다(self):
        self.assertTrue(self.w._acquire_worker_lock())
        self.assertTrue(self.w.WORKER_PID_FILE.exists())

    def test_살아있는_선행_인스턴스가_있으면_물러난다(self):
        import json as _j, os as _os
        # 확실히 살아 있는 PID = 자기 자신이 아닌 값이어야 하므로 부모를 흉내낸다
        self.w.WORKER_PID_FILE.write_text(
            _j.dumps({"pid": _os.getpid() + 10 ** 7, "shard": 0}), encoding="utf-8")
        with patch.object(self.w, "_pid_alive", return_value=True):
            self.assertFalse(self.w._acquire_worker_lock())

    def test_죽은_인스턴스의_pid파일은_무시한다(self):
        import json as _j
        self.w.WORKER_PID_FILE.write_text(
            _j.dumps({"pid": 999999, "shard": 0}), encoding="utf-8")
        with patch.object(self.w, "_pid_alive", return_value=False):
            self.assertTrue(self.w._acquire_worker_lock())

    def test_자기자신의_pid는_막지_않는다(self):
        import json as _j, os as _os
        self.w.WORKER_PID_FILE.write_text(
            _j.dumps({"pid": _os.getpid(), "shard": 0}), encoding="utf-8")
        self.assertTrue(self.w._acquire_worker_lock())

    def test_깨진_pid파일이어도_기동을_막지_않는다(self):
        self.w.WORKER_PID_FILE.write_text("not json", encoding="utf-8")
        self.assertTrue(self.w._acquire_worker_lock())

    def test_pid파일은_샤드별로_분리된다(self):
        import inspect
        src = inspect.getsource(self.w)
        self.assertIn('WORKER_PID_FILE = WS_STATUS_PATH.parent / f"ws_trade_worker{_suffix}_pid.json"', src)

    def test_run이_락_실패시_스트림을_열지_않는다(self):
        import inspect
        src = inspect.getsource(self.w.run)
        i = src.index("_acquire_worker_lock")
        self.assertIn("return", src[i:i + 120])
        self.assertLess(i, src.index("ws.start(symbols)"),
                        "락 확인이 스트림 기동보다 뒤에 있다")


class MainTradeWorkerStartupCleanupTests(unittest.TestCase):
    def test_기동시_이전세대_trade_worker를_정리한다(self):
        import inspect
        from bot import main as m
        src = inspect.getsource(m.start_ws_layer)
        self.assertIn("_cleanup_stale_ws_trade_workers_on_start(trade_shard_count)", src)

    def test_정리로직이_windows에서_taskkill_tree를_쓴다(self):
        import inspect
        from bot import main as m
        src = inspect.getsource(m._terminate_pid_tree)
        self.assertIn('"taskkill"', src)
        self.assertIn('"/T"', src)
        self.assertIn('"/F"', src)
