"""[2026-08-15] 체결(aggTrade) 스트림 기반 조기진입 신호 연결코드 단위테스트.

실 네트워크/API 호출 없이 순수 로직만 검증한다:
- bot.strategy.spike_based_entry_signal()
- cfg.spike_entry_enabled 플래그가 기본 false이고, 꺼져있을 때 main.scan_entry_candidate
  경로가 회귀 없이 동작하는지(모듈 임포트 및 기본 상태 확인 수준).
"""
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.strategy import spike_based_entry_signal
from bot.ws_trade_client import TradeTick, TradeTickCache


@dataclass
class _FakeCfg:
    spike_entry_multiplier: float = 3.0
    spike_entry_window_sec: float = 10.0
    spike_entry_baseline_sec: float = 300.0


def _make_tick(symbol, price, qty, trade_time_ms, is_buyer_maker=False):
    return TradeTick(
        symbol=symbol, price=price, quantity=qty,
        event_time_ms=trade_time_ms, trade_time_ms=trade_time_ms,
        is_buyer_maker=is_buyer_maker,
    )


class SpikeBasedEntrySignalTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _FakeCfg()

    def test_cache_none_returns_false(self):
        self.assertFalse(spike_based_entry_signal(None, "BTCUSDT", "LONG", self.cfg))

    def test_empty_cache_returns_false(self):
        cache = TradeTickCache()
        self.assertFalse(spike_based_entry_signal(cache, "BTCUSDT", "LONG", self.cfg, now_ms=10_000_000))

    def test_spike_detected_returns_true(self):
        cache = TradeTickCache()
        now_ms = 10_000_000
        # baseline: steady small trades over the last 300s (before the last 10s window)
        for i in range(60):
            t = now_ms - 300_000 + i * 4000
            cache.append(_make_tick("BTCUSDT", 100.0, 1.0, t))
        # spike window: last 10s, much larger quote volume
        for i in range(5):
            t = now_ms - 5000 + i * 1000
            cache.append(_make_tick("BTCUSDT", 100.0, 50.0, t))
        self.assertTrue(spike_based_entry_signal(cache, "BTCUSDT", "LONG", self.cfg, now_ms=now_ms))

    def test_no_spike_returns_false(self):
        cache = TradeTickCache()
        now_ms = 10_000_000
        for i in range(80):
            t = now_ms - 300_000 + i * 3500
            cache.append(_make_tick("BTCUSDT", 100.0, 1.0, t))
        self.assertFalse(spike_based_entry_signal(cache, "BTCUSDT", "LONG", self.cfg, now_ms=now_ms))

    def test_side_agnostic_quote_volume_only(self):
        """설계상 방향은 보지 않고 순수 거래량 급증만 본다 - LONG/SHORT 모두 같은 결과."""
        cache = TradeTickCache()
        now_ms = 10_000_000
        for i in range(60):
            t = now_ms - 300_000 + i * 4000
            cache.append(_make_tick("ETHUSDT", 100.0, 1.0, t))
        for i in range(5):
            t = now_ms - 5000 + i * 1000
            cache.append(_make_tick("ETHUSDT", 100.0, 50.0, t))
        long_result = spike_based_entry_signal(cache, "ETHUSDT", "LONG", self.cfg, now_ms=now_ms)
        short_result = spike_based_entry_signal(cache, "ETHUSDT", "SHORT", self.cfg, now_ms=now_ms)
        self.assertEqual(long_result, short_result)


class ConfigDefaultFlagTests(unittest.TestCase):
    def test_code_default_is_false_when_env_unset(self):
        """[2026-08-15] .env의 SPIKE_ENTRY_ENABLED는 라이브 배선 후 true로 바뀌었지만
        (사용자 승인, 3단계 백테스트 검증 완료), 환경변수가 아예 없을 때의 dataclass
        기본값은 여전히 false여야 한다(신규 배포 환경에서 실수로 켜진 채 시작되지 않도록) —
        Config()로 .env 값을 그대로 읽는 대신 dataclass field 기본값 자체를 검사한다."""
        from bot.config import Config
        import dataclasses
        field = next(f for f in dataclasses.fields(Config) if f.name == "spike_entry_enabled")
        # _bool("SPIKE_ENTRY_ENABLED", "false")의 "false" 인자가 코드상 기본값임을 소스로 확인.
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('spike_entry_enabled: bool = _bool("SPIKE_ENTRY_ENABLED", "false")', src)

    def test_env_reenabled_2026_08_17_with_v2_actually_receiving(self):
        """[09:20 롤백 -> 09:2x 워치독보강 -> 09:40 재롤백 -> 09:4x 샤딩(3번째) -> 09:49
        재롤백 -> 10:1x 대안3(예방적 재시작, 4번째 시도) -> 10:2x 재롤백 -> 11:2x 대안1(저수준
        websockets 재구현, 5번째 시도) -> 23:0x 5번째도 롤백]

        5번째 시도의 실패 양상은 이전 4번(read loop tight loop)과 완전히 다르다: 11:48 라이브
        연결 후 11시간 동안 에러 0건인 채로 체결 데이터를 단 1건도 못 받았다(전 심볼
        tick_count_recent=0). 직접 재현 결과 raw websockets(신규/legacy 구현 둘 다, aggTrade/
        kline 둘 다)로 메인넷 fstream에 새로 여는 연결이 전부 0건인 반면, 이미 오래 유지 중인
        시장데이터 워커(python-binance)는 같은 호스트에서 정상 수신 — 재연결 폭풍이 바이낸스
        WS 연결 레이트리밋을 상시 초과해 스스로를 굶긴 자기증폭 장애로 추정.

        [23:2x 대안2로 재활성화 — 사용자 승인] V2 무음 원인 조사는 장기과제로 분리하고(V2
        클래스는 bot/ws_trade_client.py에 보존), 지금 확실히 동작하는 python-binance 경로
        (TradeStreamWebSocket)로 되돌려 다시 켠다. read-loop 버그는 샤딩2+예방적재시작300초+
        워치독 에러카운트 안전망으로 관리. 테스트넷 소크는 이 버그 검증에 무의미하므로
        생략했다(방아쇠가 메시지 폭주량이라 테스트넷에선 조건 자체가 재현 불가).

        [2026-08-16 00:1x V0도 롤백 — 기준D 발동] 배포 후 53분간 반응형 재시작 56건(기준 6건),
        사유 전부 error_count_60s이고 값이 8만~47만대, 워커 로그에 "Read loop has been closed"
        그대로 — 4번 실패했던 그 버그가 동일 재현. 데이터 수신 자체는 정상이었다는 점(25,520
        메시지/60초, 35/35심볼)에서 V2의 "무음" 실패와는 정반대 양상이다. 즉 이 기능은 현재
        python-binance 경로든 raw websockets 경로든 양쪽 다 막혀 있다.
        [2026-08-16 00:2x 재활성화 — 사용자 요청으로 3종 대책 적용 후 6번째 시도]
        롤백 직후 확인된 사실이 방향을 바꿨다: V0는 에러가 많았을 뿐 데이터는 정상 수신
        중이었다(25,520메시지/60초, 35/35심볼). 즉 error_count를 장애로 오판해 2~3분마다
        재시작하느라 오히려 연결을 계속 끊고 있었다. 대책:
          ① SPIKE_ENTRY_MAX_SYMBOLS=20 — 유동성 상위 20개만 구독(70개 대비 메시지량 약
             1/3.5)해 read-loop 버그의 방아쇠인 메시지 폭주 자체를 낮춘다.
          ② WS_TRADE_HEALTH_BY_DATA_FLOW=true — 건강판정을 error_count가 아니라 데이터
             유입량(message_count_60s>0)으로 전환. V0는 에러多/데이터正常, V2는 에러0/
             데이터0이었으므로 에러 수는 양방향 모두에서 건강 지표로 부적합.
          ③ read-loop 에러 로거의 propagate를 끊어 로그 폭주 I/O 부하만 제거(카운팅은 유지).
        예방적 재시작(300초)/샤딩(2)은 안전망으로 계속 유지. 재발 판정 기준도 "데이터 유입이
        끊기는가"로 바뀌었으므로, 다음 모니터링은 no_data 재시작 발생 여부를 봐야 한다.

        [주의] 이 기능의 성과 자체는 아직 근거가 없다 — 첫 실측(V0 가동 24건 중 spike 태그
        5건)에서 spike 거래 승률 40.0%/-0.18U vs 비spike 78.9%/+1.08U로 오히려 나빴다.
        인프라가 안정되면 표본을 더 쌓아 효과를 판정할 것.
        [2026-08-16 00:2x 3종대책도 즉시 롤백 — 90초 만에 오히려 악화]
        trade0 err60s=1,338,453(초당 22,000건)/trade1 627,300인데 실제 체결 틱은 0/10 심볼.
        심볼을 20개로 줄여도 완화되기는커녕 더 심해졌다 — "메시지 폭주량이 방아쇠"라는 가설
        자체가 틀렸다. 더 위험했던 건 데이터유입 기준 워치독이 message_count_60s를 보는데
        그 카운터가 에러까지 세고 있어(1,338,714 중 1,338,453이 에러) "정상"으로 오판, 안전망이
        통째로 무력화됐다는 점이다. 판정 지표로는 spikes[].tick_count_recent(실제 파싱된 체결
        틱)를 써야 한다 — 다음 시도 시 반드시 이 지표로 바꾸고 검증할 것.
        [2026-08-17 재활성화] 이번엔 V2가 실제로 데이터를 받는 것이 실측으로 확인됐다:
        trade0 msg60s=9,800 / 틱수신 10/10 심볼, trade1 msg60s=2,114 / 10/10 (err=0).
        8/16의 "에러 0인데 데이터도 0"인 무음 실패와는 정반대 상태다. 구독 심볼을 샤드당
        10개로 줄인 것이 주효한 것으로 보인다.
        끌 때는 이 단언을 반대로 갱신하고 사유를 남길 것.
        [주의] .env에 SPIKE_ENTRY_ENABLED가 752행(false)/810행(true) 두 곳에 있고 나중 값이
        이긴다 — 값을 바꿀 땐 두 줄 다 확인할 것."""
        from bot.config import Config
        cfg = Config()
        self.assertTrue(cfg.spike_entry_enabled)
        self.assertFalse(cfg.ws_trade_health_by_data_flow)

    def test_worker_defaults_to_python_binance_but_keeps_v2_opt_in(self):
        """기본값은 기존 python-binance 경로를 유지하되, 안전 스위치로 V2 복귀 경로는
        보존돼 있어야 한다."""
        import inspect
        from bot.config import Config
        from bot import ws_trade_worker
        # [2026-08-21] V2 를 껐다가(그리드에서 체결흐름 무용, 수익상관 -0.117)
        # 같은 날 사용자 요청으로 다시 켰다 — "WS와 V2는 다시 재실행해주고".
        # 아래 단언들이 'V2 실행 경로가 살아 있음' 을 지킨다.
        self.assertTrue(Config().ws_trade_use_v2)
        src = inspect.getsource(ws_trade_worker.run)
        self.assertIn("ws = _build_trade_stream(cfg, health)", src)
        self.assertIn('"raw-v2" if cfg.ws_trade_use_v2 else "python-binance"', src)


class MainModuleWiringNoRegressionTests(unittest.TestCase):
    def test_module_imports_and_cache_defaults_none(self):
        """bot.main이 정상 임포트되고, 스파이크 캐시 연결지점은 기본 None(no-op)이어야 한다."""
        from bot import main as bot_main
        self.assertIsNone(bot_main._SPIKE_ENTRY_CACHE)
        self.assertTrue(callable(bot_main.set_spike_entry_cache))

    def test_set_spike_entry_cache_updates_module_state(self):
        from bot import main as bot_main
        sentinel = object()
        try:
            bot_main.set_spike_entry_cache(sentinel)
            self.assertIs(bot_main._SPIKE_ENTRY_CACHE, sentinel)
        finally:
            bot_main.set_spike_entry_cache(None)


class ScanEntryCandidateNonBlockingTriggerTests(unittest.TestCase):
    """[2026-08-16 v3] 게이트 -> 조기진입 트리거 재설계 검증. scan_entry_candidate가
    spike_entry_enabled=True에서도 spike_based_entry_signal() 결과로 절대 None을
    반환(진입 차단)하지 않고, 후보 dict에 early_entry_spike 정보만 얹는지 소스 레벨로
    확인한다 (전체 흐름을 mock하기엔 df/exchange 의존이 너무 무거워, 회귀의 핵심인
    "차단 여부"만 정적으로 검증)."""

    def test_source_never_blocks_on_spike_result(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.scan_entry_candidate)
        self.assertIn("early_entry_spike", src)
        self.assertNotIn("not spike_based_entry_signal", src)
        # 스파이크 판정 직후 곧바로 return None으로 이어지는 게이트 패턴이 없어야 한다.
        idx = src.index("early_entry_spike = bool(")
        tail = src[idx: idx + 400]
        self.assertNotIn("return None", tail)

    def test_returned_dict_carries_early_entry_flag_key(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.scan_entry_candidate)
        self.assertIn('"early_entry_spike": early_entry_spike,', src)


class FileBackedSpikeCacheTests(unittest.TestCase):
    """[2026-08-15 라이브 배선] ws_trade_worker.py(들)의 상태파일을 읽기만 하는
    FileBackedSpikeCache 어댑터 검증. 실 프로세스/네트워크 없이 tempfile로만 검증."""

    def _write(self, status_path, heartbeat_path, spikes, dumped_at=None):
        import json
        import time
        status_path.write_text(json.dumps({"dumped_at": dumped_at or time.time(), "spikes": spikes}), encoding="utf-8")
        heartbeat_path.write_text(str(time.time()), encoding="utf-8")

    def test_missing_files_return_false(self):
        import tempfile
        from pathlib import Path
        from bot.ws_trade_client import FileBackedSpikeCache
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileBackedSpikeCache([(Path(tmp) / "status.json", Path(tmp) / "hb.txt")])
            self.assertFalse(cache.is_spike("BTCUSDT"))

    def test_fresh_true_spike_is_reported(self):
        import tempfile
        from pathlib import Path
        from bot.ws_trade_client import FileBackedSpikeCache
        with tempfile.TemporaryDirectory() as tmp:
            status_path, hb_path = Path(tmp) / "status.json", Path(tmp) / "hb.txt"
            self._write(status_path, hb_path, {"BTCUSDT": {"is_spike": True}})
            cache = FileBackedSpikeCache([(status_path, hb_path)])
            self.assertTrue(cache.is_spike("BTCUSDT"))
            self.assertFalse(cache.is_spike("ETHUSDT"))  # 데이터 없는 심볼은 보수적으로 False

    def test_stale_heartbeat_ignored_even_if_file_says_spike(self):
        import tempfile
        import time
        from pathlib import Path
        from bot.ws_trade_client import FileBackedSpikeCache
        with tempfile.TemporaryDirectory() as tmp:
            status_path, hb_path = Path(tmp) / "status.json", Path(tmp) / "hb.txt"
            self._write(status_path, hb_path, {"BTCUSDT": {"is_spike": True}})
            hb_path.write_text(str(time.time() - 999), encoding="utf-8")  # 워커가 죽은 것처럼 오래된 하트비트
            cache = FileBackedSpikeCache([(status_path, hb_path)], worker_max_staleness_sec=30.0)
            self.assertFalse(cache.is_spike("BTCUSDT"))

    def test_spike_based_entry_signal_duck_types_file_backed_cache(self):
        """spike_based_entry_signal이 get_recent 없는 FileBackedSpikeCache를 받으면
        detect_volume_spike()로 안 가고 곧바로 is_spike()를 호출해야 한다."""
        import tempfile
        from pathlib import Path
        from bot.ws_trade_client import FileBackedSpikeCache
        with tempfile.TemporaryDirectory() as tmp:
            status_path, hb_path = Path(tmp) / "status.json", Path(tmp) / "hb.txt"
            self._write(status_path, hb_path, {"BTCUSDT": {"is_spike": True}})
            cache = FileBackedSpikeCache([(status_path, hb_path)])
            self.assertTrue(spike_based_entry_signal(cache, "BTCUSDT", "LONG", _FakeCfg()))


class ReadTradeWorkerHealthTests(unittest.TestCase):
    """[2026-08-15 09:20 사고 이후 보강] _read_trade_worker_health()가 status 파일의
    health 딕셔너리를 제대로 읽어오는지, 파일이 없거나 깨져도 안전하게 None을 반환하는지."""

    def test_missing_file_returns_none(self):
        from bot.main import _read_trade_worker_health
        self.assertIsNone(_read_trade_worker_health("/no/such/path.json"))

    def test_none_path_returns_none(self):
        from bot.main import _read_trade_worker_health
        self.assertIsNone(_read_trade_worker_health(None))

    def test_reads_health_dict(self):
        import json
        import tempfile
        from pathlib import Path
        from bot.main import _read_trade_worker_health
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "status.json"
            p.write_text(json.dumps({"health": {"error_count_60s": 390984, "consecutive_read_loop_errors": 1}}), encoding="utf-8")
            health = _read_trade_worker_health(p)
        self.assertEqual(health["error_count_60s"], 390984)

    def test_malformed_json_returns_none(self):
        import tempfile
        from pathlib import Path
        from bot.main import _read_trade_worker_health
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "status.json"
            p.write_text("not json", encoding="utf-8")
            self.assertIsNone(_read_trade_worker_health(p))


class TradeWorkerWatchdogErrorCountSourceTests(unittest.TestCase):
    """[2026-08-15 09:20 실사고: read-loop 무한에러인데 하트비트는 안 죽어서 못 잡음]
    main() 루프의 체결워커 워치독이 market 워커와 동일한 error_count_60s /
    consecutive_read_loop_errors 기준을 실제로 검사하는지 소스 레벨로 확인한다(전체
    main() 루프를 실행하며 검증하기엔 의존성이 너무 무거움 — 이 저장소의 기존 관례인
    소스 스캔 방식을 그대로 따름, ScanEntryCandidateNonBlockingTriggerTests 참고)."""

    def test_watchdog_checks_error_count_and_consecutive_errors(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn("error_count_60s", src)
        self.assertIn("consecutive_read_loop_errors", src)
        self.assertIn("cfg.ws_max_error_count_60s", src)
        self.assertIn("cfg.ws_max_consecutive_read_loop_errors", src)


class PreemptiveRestartConfigTests(unittest.TestCase):
    """[2026-08-15 대안3] 예방적 재시작 — 문제가 나길 기다리지 않고 주기적으로 워커를
    갈아치워서 tight loop 지속시간의 상한을 강제한다. 기본값(0)이면 완전 no-op."""

    def test_code_default_is_disabled_when_env_unset(self):
        """[2026-08-15] .env는 대안3 테스트를 위해 300으로 켜져 있지만(라이브 현재 상태),
        환경변수가 아예 없는 신규 배포 환경에서의 dataclass 기본값은 0(비활성화)이어야
        한다 — Config()로 .env를 그대로 읽는 대신 소스의 리터럴 기본값을 확인한다."""
        import inspect
        from bot.config import Config
        src = inspect.getsource(Config)
        self.assertIn('ws_trade_preemptive_restart_sec: float = _float("WS_TRADE_PREEMPTIVE_RESTART_SEC", 0.0)', src)

    def test_watchdog_source_has_preemptive_branch(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn("ws_trade_preemptive_restart_sec", src)
        self.assertIn('"preemptive:', src)

    def test_preemptive_restart_does_not_accumulate_backoff(self):
        """예방적 재시작은 exponential backoff용 consecutive_restart_count를 건드리면
        안 된다 — 소스에서 preemptive 분기가 반드시 counter를 0으로 리셋하는지 확인."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        idx = src.index("tw_is_preemptive")
        branch = src[idx: idx + 1600]
        self.assertIn('trade_worker["consecutive_restart_count"] = 0', branch)


if __name__ == "__main__":
    unittest.main()
