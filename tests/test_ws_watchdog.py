"""[2026-08-10] WS 워커 프로세스 관리(start_ws_layer/stop_ws_layer/ws_layer_needs_restart)
단위테스트. 2026-08-10 세 번째 실거래 사고 이후, WS 연결을 완전히 별도 OS 프로세스로
격리하는 방식으로 재설계됐다 — python-binance 라이브러리 버그가 프로세스 자체를 응답불능에
빠뜨리는 걸 실측했기 때문에, 같은 프로세스 안에서 아무리 재시작 로직을 잘 짜도 "재시작
판단을 내리는 코드"까지 같이 얼어붙으면 무의미하다는 게 배경.

[2026-08-10, Codex 제안 반영] 시장데이터를 여러 샤드(독립 프로세스)로 나눌 수 있게
되면서 handles 구조가 단일 dict{"process","started_at"}에서 리스트 dict{"workers":[...]}
로 바뀌었고, ws_layer_needs_restart()의 반환값도 bool에서 "재시작이 필요한 워커 인덱스
리스트"로 바뀌었다(문제가 생긴 샤드만 부분 재시작하기 위함). 실제 서브프로세스를 띄우거나
바이낸스 API를 호출하지 않는다 — subprocess.Popen 자체를 가짜로 바꿔치기한다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import start_ws_layer, stop_ws_layer, ws_layer_needs_restart, WS_WORKER_STARTUP_GRACE_SEC


def make_cfg(**overrides):
    cfg = Config()
    cfg.ws_market_data_enabled = False
    cfg.ws_user_data_enabled = False
    cfg.ws_kline_max_staleness_sec = 150.0
    cfg.ws_market_shard_count = 1
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class FakeExchange:
    def __init__(self):
        self._ws_kline_cache = None
        self._fill_tracker = None

    def set_ws_kline_cache(self, cache):
        self._ws_kline_cache = cache

    def set_fill_tracker(self, tracker):
        self._fill_tracker = tracker


def make_worker(role="market", shard_index=0, shard_count=1, symbols=None, started_at=0.0, process=None):
    return {
        "process": process or MagicMock(), "started_at": started_at, "role": role,
        "shard_index": shard_index, "shard_count": shard_count, "symbols": symbols or [],
        "cache_path": Path("dummy_cache.json"), "heartbeat_path": Path("dummy_heartbeat.txt"),
    }


class StartWsLayerTests(unittest.TestCase):
    def test_does_not_spawn_process_when_both_flags_disabled(self):
        cfg = make_cfg()
        ex = FakeExchange()
        with patch("subprocess.Popen") as fake_popen:
            handles = start_ws_layer(ex, cfg, ["BTCUSDT"])
        fake_popen.assert_not_called()
        self.assertEqual(handles["workers"], [])

    def test_spawns_single_market_worker_when_shard_count_is_one(self):
        cfg = make_cfg(ws_market_data_enabled=True, ws_market_shard_count=1)
        ex = FakeExchange()
        fake_process = MagicMock()
        with patch("subprocess.Popen", return_value=fake_process) as fake_popen:
            handles = start_ws_layer(ex, cfg, ["BTCUSDT", "ETHUSDT"])

        fake_popen.assert_called_once()
        args = fake_popen.call_args[0][0]
        self.assertIn("bot.ws_worker", args)
        self.assertEqual(len(handles["workers"]), 1)
        self.assertEqual(handles["workers"][0]["role"], "market")
        self.assertIsNotNone(ex._ws_kline_cache)

    def test_spawns_two_market_shards_when_shard_count_is_two(self):
        """[핵심] 샤드 개수만큼 독립 프로세스가 뜨고, 심볼이 인터리브 방식으로 나뉘어야 한다."""
        cfg = make_cfg(ws_market_data_enabled=True, ws_market_shard_count=2)
        ex = FakeExchange()
        with patch("subprocess.Popen", return_value=MagicMock()) as fake_popen:
            handles = start_ws_layer(ex, cfg, ["S0", "S1", "S2", "S3"])

        self.assertEqual(fake_popen.call_count, 2)
        market_workers = [w for w in handles["workers"] if w["role"] == "market"]
        self.assertEqual(len(market_workers), 2)
        self.assertEqual(market_workers[0]["symbols"], ["S0", "S2"])  # 인덱스 0::2
        self.assertEqual(market_workers[1]["symbols"], ["S1", "S3"])  # 인덱스 1::2
        self.assertIsNotNone(ex._ws_kline_cache)  # MultiFileBackedKlineCache로 합쳐 연결됨

    def test_user_data_stream_is_a_separate_worker_from_market_data(self):
        """[핵심] User Data Stream은 시장데이터 워커와 항상 별도 프로세스여야 한다."""
        cfg = make_cfg(ws_market_data_enabled=True, ws_user_data_enabled=True, ws_market_shard_count=2)
        ex = FakeExchange()
        with patch("subprocess.Popen", return_value=MagicMock()) as fake_popen:
            handles = start_ws_layer(ex, cfg, ["S0", "S1"])

        self.assertEqual(fake_popen.call_count, 3)  # 시장데이터 샤드 2개 + 계정스트림 1개
        roles = [w["role"] for w in handles["workers"]]
        self.assertEqual(roles.count("market"), 2)
        self.assertEqual(roles.count("user"), 1)
        self.assertIsNotNone(ex._fill_tracker)

    def test_popen_failure_returns_empty_handles_without_raising(self):
        cfg = make_cfg(ws_market_data_enabled=True)
        ex = FakeExchange()
        with patch("subprocess.Popen", side_effect=OSError("실행 실패(테스트 모의)")):
            try:
                handles = start_ws_layer(ex, cfg, ["BTCUSDT"])
            except Exception as e:
                self.fail(f"start_ws_layer이 예외를 던지면 안 됨: {e}")
        self.assertEqual(handles["workers"], [])


class StopWsLayerTests(unittest.TestCase):
    def test_terminates_process_gracefully_first(self):
        ex = FakeExchange()
        fake_process = MagicMock()
        fake_process.wait.return_value = 0
        handles = {"workers": [make_worker(process=fake_process)]}

        stop_ws_layer(ex, handles)

        fake_process.terminate.assert_called_once()
        fake_process.kill.assert_not_called()  # 정상 종료됐으면 kill까지 갈 필요 없음
        self.assertIsNone(ex._ws_kline_cache)
        self.assertIsNone(ex._fill_tracker)

    def test_force_kills_when_terminate_does_not_finish_in_time(self):
        """[2026-08-10 실거래 사고 회귀테스트] terminate()로 안 죽으면(예: tight loop에
        빠져 시그널을 못 받는 상태) 반드시 kill()까지 가야 한다 — 이게 이번 재설계의
        핵심 안전장치다."""
        ex = FakeExchange()
        fake_process = MagicMock()
        fake_process.wait.side_effect = [Exception("타임아웃(테스트 모의)"), 0]
        handles = {"workers": [make_worker(process=fake_process)]}

        stop_ws_layer(ex, handles)

        fake_process.terminate.assert_called_once()
        fake_process.kill.assert_called_once()

    def test_handles_empty_worker_list_without_raising(self):
        ex = FakeExchange()
        handles = {"workers": []}
        try:
            stop_ws_layer(ex, handles)
        except Exception as e:
            self.fail(f"stop_ws_layer이 예외를 던지면 안 됨: {e}")
        self.assertIsNone(ex._ws_kline_cache)

    def test_only_indices_stops_just_the_specified_workers(self):
        """[핵심] 부분 재시작 지원 — only_indices를 주면 그 워커만 정리하고, 나머지 정상
        워커는 건드리지 않아야 한다(전체 재시작보다 다운타임이 훨씬 짧다)."""
        ex = FakeExchange()
        p0, p1 = MagicMock(), MagicMock()
        p0.wait.return_value = 0
        handles = {"workers": [make_worker(shard_index=0, process=p0), make_worker(shard_index=1, process=p1)]}

        stop_ws_layer(ex, handles, only_indices=[0])

        p0.terminate.assert_called_once()
        p1.terminate.assert_not_called()
        # only_indices를 준 부분 재시작에서는 Exchange 참조를 전체 초기화하지 않는다
        # (호출부가 남은/재시작된 워커로 다시 구성하는 걸 전제로 함).


class WsLayerNeedsRestartTests(unittest.TestCase):
    def test_empty_list_when_both_flags_disabled(self):
        cfg = make_cfg()
        handles = {"workers": []}
        self.assertEqual(ws_layer_needs_restart(cfg, handles, "BTCUSDT"), [])

    def test_empty_list_when_no_workers_started(self):
        cfg = make_cfg(ws_market_data_enabled=True)
        handles = {"workers": []}
        self.assertEqual(ws_layer_needs_restart(cfg, handles, "BTCUSDT"), [])

    def test_includes_index_when_process_already_exited(self):
        """워커 프로세스 자체가 죽어있으면(크래시 등) 두 말할 것 없이 재시작이 필요하다."""
        cfg = make_cfg(ws_market_data_enabled=True)
        fake_process = MagicMock()
        fake_process.poll.return_value = 1  # 종료됨(0이 아닌 코드)
        handles = {"workers": [make_worker(process=fake_process, symbols=["BTCUSDT"])]}
        self.assertEqual(ws_layer_needs_restart(cfg, handles, "BTCUSDT"), [0])

    def test_empty_within_startup_grace_period_even_if_process_alive_with_no_data_yet(self):
        """[2026-08-10] 워커가 막 시작돼 아직 REST 시딩 중일 때는 당연히 캐시가 비어있다 —
        이때 바로 재시작 판단을 내리면 무한 재시작 루프에 빠진다. 유예시간 안에는 프로세스가
        살아있는 한(poll()이 None) 판단을 보류해야 한다."""
        cfg = make_cfg(ws_market_data_enabled=True)
        fake_process = MagicMock()
        fake_process.poll.return_value = None  # 아직 실행 중
        with patch("bot.main.time.time", return_value=1000.0 + WS_WORKER_STARTUP_GRACE_SEC / 2):
            handles = {"workers": [make_worker(process=fake_process, started_at=1000.0, symbols=["BTCUSDT"])]}
            self.assertEqual(ws_layer_needs_restart(cfg, handles, "BTCUSDT"), [])

    def test_checks_cache_freshness_after_grace_period_expires(self):
        cfg = make_cfg(ws_market_data_enabled=True, ws_kline_max_staleness_sec=150.0)
        fake_process = MagicMock()
        fake_process.poll.return_value = None
        with patch("bot.main.time.time", return_value=1000.0 + WS_WORKER_STARTUP_GRACE_SEC + 1), \
             patch("bot.main.FileBackedKlineCache") as fake_cache_cls:
            fake_cache_cls.return_value.is_fresh.return_value = False
            fake_cache_cls.return_value.health.return_value = None
            handles = {"workers": [make_worker(process=fake_process, started_at=1000.0, symbols=["BTCUSDT"])]}
            self.assertEqual(ws_layer_needs_restart(cfg, handles, "BTCUSDT"), [0])

    def test_only_broken_shard_flagged_when_other_shard_is_healthy(self):
        """[핵심, 샤딩] 샤드 2개 중 하나만 문제가 있으면 그 인덱스만 반환해야 한다 —
        정상인 샤드까지 같이 재시작 대상에 넣으면 안 된다."""
        cfg = make_cfg(ws_market_data_enabled=True, ws_market_shard_count=2, ws_message_max_staleness_sec=45.0)
        now = 1000.0 + WS_WORKER_STARTUP_GRACE_SEC + 100
        p0, p1 = MagicMock(), MagicMock()
        p0.poll.return_value = None
        p1.poll.return_value = None
        w0 = make_worker(shard_index=0, shard_count=2, process=p0, started_at=1000.0, symbols=["BTCUSDT"])
        w1 = make_worker(shard_index=1, shard_count=2, process=p1, started_at=1000.0, symbols=["ETHUSDT"])
        w0["cache_path"], w1["cache_path"] = Path("shard0.json"), Path("shard1.json")
        handles = {"workers": [w0, w1]}

        def fake_cache_factory(cache_path, heartbeat_path, **kwargs):
            m = MagicMock()
            if cache_path == w0["cache_path"]:
                m.health.return_value = {"last_market_message_ts": now - 60, "consecutive_read_loop_errors": 0}  # 죽음
                m.is_fresh.return_value = True
            else:
                m.health.return_value = {"last_market_message_ts": now - 1, "consecutive_read_loop_errors": 0}  # 정상
                m.is_fresh.return_value = True
            return m

        with patch("bot.main.time.time", return_value=now), \
             patch("bot.main.FileBackedKlineCache", side_effect=fake_cache_factory):
            self.assertEqual(ws_layer_needs_restart(cfg, handles, "ETHUSDT"), [0])


class WsLayerNeedsRestartHealthBasedTests(unittest.TestCase):
    """[2026-08-10, Codex 제안 반영] 테스트넷 실측: canary kline freshness(150초, 캔들
    "마감" 기준)만으로는 read loop 프리징(약 90초 주기 재발)을 너무 늦게 잡는다 — 실시간
    메시지 카운터와 연속 에러 횟수 기반 조건을 추가로 검증한다."""

    def _handles_past_grace(self, symbols=None):
        fake_process = MagicMock()
        fake_process.poll.return_value = None
        return {"workers": [make_worker(process=fake_process, started_at=1000.0, symbols=symbols or ["BTCUSDT"])]}

    def test_empty_when_health_data_missing(self):
        """구버전 워커 등으로 health 필드 자체가 없으면(None) 기존 캔들신선도 판단만
        신뢰하고 섣불리 재시작 판단을 내리면 안 된다."""
        cfg = make_cfg(ws_market_data_enabled=True)
        with patch("bot.main.time.time", return_value=1000.0 + WS_WORKER_STARTUP_GRACE_SEC + 1), \
             patch("bot.main.FileBackedKlineCache") as fake_cache_cls:
            fake_cache_cls.return_value.is_fresh.return_value = True
            fake_cache_cls.return_value.health.return_value = None
            self.assertEqual(ws_layer_needs_restart(cfg, self._handles_past_grace(), "BTCUSDT"), [])

    def test_flagged_when_message_stream_stale_even_if_canary_kline_still_fresh(self):
        """[핵심] 캔들은 아직 신선해 보여도(마지막 마감 캔들 기준), 실시간 메시지 자체가
        message_max_staleness_sec 넘게 끊겼으면 재시작해야 한다 — 이게 canary freshness
        보다 훨씬 빨리 프리징을 잡아내는 이유다."""
        cfg = make_cfg(ws_market_data_enabled=True, ws_message_max_staleness_sec=45.0)
        now = 1000.0 + WS_WORKER_STARTUP_GRACE_SEC + 100
        with patch("bot.main.time.time", return_value=now), \
             patch("bot.main.FileBackedKlineCache") as fake_cache_cls:
            fake_cache_cls.return_value.is_fresh.return_value = True  # 캔들은 아직 신선
            fake_cache_cls.return_value.health.return_value = {
                "last_market_message_ts": now - 60,  # 45초 문턱을 넘겨 끊김
                "consecutive_read_loop_errors": 0,
            }
            self.assertEqual(ws_layer_needs_restart(cfg, self._handles_past_grace(), "BTCUSDT"), [0])

    def test_flagged_when_consecutive_read_loop_errors_exceed_threshold(self):
        cfg = make_cfg(ws_market_data_enabled=True, ws_max_consecutive_read_loop_errors=3)
        now = 1000.0 + WS_WORKER_STARTUP_GRACE_SEC + 100
        with patch("bot.main.time.time", return_value=now), \
             patch("bot.main.FileBackedKlineCache") as fake_cache_cls:
            fake_cache_cls.return_value.is_fresh.return_value = True
            fake_cache_cls.return_value.health.return_value = {
                "last_market_message_ts": now - 1,  # 메시지는 방금 옴(정상)
                "consecutive_read_loop_errors": 3,
            }
            self.assertEqual(ws_layer_needs_restart(cfg, self._handles_past_grace(), "BTCUSDT"), [0])

    def test_empty_when_all_health_signals_healthy(self):
        cfg = make_cfg(ws_market_data_enabled=True)
        now = 1000.0 + WS_WORKER_STARTUP_GRACE_SEC + 100
        with patch("bot.main.time.time", return_value=now), \
             patch("bot.main.FileBackedKlineCache") as fake_cache_cls:
            fake_cache_cls.return_value.is_fresh.return_value = True
            fake_cache_cls.return_value.health.return_value = {
                "last_market_message_ts": now - 1,
                "consecutive_read_loop_errors": 0,
            }
            self.assertEqual(ws_layer_needs_restart(cfg, self._handles_past_grace(), "BTCUSDT"), [])


if __name__ == "__main__":
    unittest.main()
