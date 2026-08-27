"""[2026-08-16 사용자요청] 스파이크 조기진입을 살리기 위한 3종 대책.

계기: V0(python-binance 경로) 배포 53분간 반응형 재시작 56건, 사유 전부 error_count
(8만~47만). 그런데 정작 데이터 수신은 정상이었다(25,520메시지/60초, 35/35심볼).
반대로 V2(raw websockets)는 error_count 0인데 데이터가 0이었다.
→ 에러 수는 양방향 모두에서 건강 지표로 부적합함이 실증됐다.

대책: ①구독 심볼을 유동성 상위 20개로 제한 ②건강판정을 데이터 유입량 기준으로 전환
③read-loop 에러 로거를 CRITICAL로 올려 로그 폭주 I/O 부하 제거.
"""
import logging
import unittest

from bot.config import Config


class SpikeSymbolLimitConfigTests(unittest.TestCase):
    def test_env_set_to_20_per_user_request(self):
        self.assertEqual(Config().spike_entry_max_symbols, 20)

    def test_code_default_is_unlimited(self):
        """환경변수가 없는 신규 배포 환경에서는 제한 없음(0)이 기본이어야 한다."""
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('spike_entry_max_symbols: int = _int("SPIKE_ENTRY_MAX_SYMBOLS", 0)', src)

    def test_health_by_data_flow_rolled_back(self):
        """[2026-08-16 00:2x 롤백] message_count_60s가 에러까지 세고 있어(1,338,714 중
        1,338,453이 에러) 워치독이 "정상"으로 오판, 안전망이 무력화됐다. 실제 파싱된 체결
        틱(spikes[].tick_count_recent) 기준으로 다시 만들기 전까지는 끈다."""
        self.assertFalse(Config().ws_trade_health_by_data_flow)

    def test_health_by_data_flow_code_default_is_off(self):
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('ws_trade_health_by_data_flow: bool = _bool("WS_TRADE_HEALTH_BY_DATA_FLOW", "false")', src)

    def test_ws_trade_use_v2_enabled_2026_08_21(self):
        """[2026-08-21] V2 실행 플래그 상태.

        같은 날 껐다가(그리드 검증에서 체결흐름이 무용 — 수익상관 -0.117)
        사용자 요청으로 다시 켰다 — "WS와 V2는 다시 재실행해주고".

        주의: V2 의 알려진 실패 모드는 '에러 0 / 데이터 0' 무음이다.
        켠 뒤에는 message_count_60s 가 0 이 아닌지 반드시 확인해야 한다.
        다시 끌 때는 이 단언을 반대로 갱신할 것."""
        self.assertTrue(Config().ws_trade_use_v2)

    def test_ws_trade_use_v2_code_default_is_off(self):
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('ws_trade_use_v2: bool = _bool("WS_TRADE_USE_V2", "false")', src)

    def test_safety_nets_still_on(self):
        """심볼 제한/판정기준 변경과 무관하게 샤딩·예방적재시작 안전망은 유지돼야 한다."""
        cfg = Config()
        self.assertEqual(cfg.ws_trade_shard_count, 2)
        self.assertEqual(cfg.ws_trade_preemptive_restart_sec, 300.0)


class SymbolTruncationTests(unittest.TestCase):
    """유동성 상위 N개만 남기는 로직 — get_active_usdt_perpetual_symbols()가 거래대금
    내림차순으로 주므로 앞에서 자르는 것이 곧 '상위 유동성'이다."""

    def test_worker_truncates_to_limit(self):
        import inspect
        from bot import ws_trade_worker
        src = inspect.getsource(ws_trade_worker.run)
        self.assertIn("if cfg.spike_entry_max_symbols > 0 and len(all_symbols) > cfg.spike_entry_max_symbols:", src)
        self.assertIn("all_symbols = all_symbols[:cfg.spike_entry_max_symbols]", src)

    def test_truncation_happens_before_sharding(self):
        """자르기가 샤딩보다 먼저 일어나야 두 샤드 합이 정확히 N개가 된다."""
        import inspect
        from bot import ws_trade_worker
        src = inspect.getsource(ws_trade_worker.run)
        trunc_at = src.index("all_symbols[:cfg.spike_entry_max_symbols]")
        shard_at = src.index("all_symbols[SHARD_INDEX::SHARD_COUNT]")
        self.assertLess(trunc_at, shard_at)

    def test_exchange_returns_volume_sorted(self):
        """앞에서 자르는 것이 '상위 유동성'이 되려면 거래대금 내림차순이어야 한다."""
        import inspect
        from bot.exchange import Exchange
        src = inspect.getsource(Exchange.get_active_usdt_perpetual_symbols)
        self.assertIn("volume_by_symbol.get(s, 0), reverse=True", src)


class ReadLoopLoggerSilencedTests(unittest.TestCase):
    def test_noisy_loggers_stop_propagating_to_root(self):
        """로그 폭주의 실제 비용은 root의 파일/콘솔 핸들러 I/O다. propagate를 끊어
        그 비용만 제거한다(워커 프로세스 전용 — 라이브 매매 프로세스엔 영향 없음)."""
        import bot.ws_trade_worker  # noqa: F401  (import 부수효과 확인이 목적)
        self.assertFalse(logging.getLogger("binance.ws.threaded_stream").propagate)
        self.assertFalse(logging.getLogger("binance.ws.reconnecting_websocket").propagate)

    def test_error_counting_still_works(self):
        """[중요] setLevel(CRITICAL)로 막으면 WsHealthMonitor의 _ReadLoopErrorWatcher가
        레코드를 못 받아 error_count 집계가 죽는다 — 부하는 줄지만 관측성을 잃는다.
        propagate만 끊는 방식은 카운팅이 그대로 살아있어야 한다."""
        import bot.ws_trade_worker  # noqa: F401
        from bot.ws_client import WsHealthMonitor
        health = WsHealthMonitor()
        logging.getLogger("binance.ws.threaded_stream").error(
            "Error receiving message: Read loop has been closed, please reset the websocket connection."
        )
        snap = health.snapshot()
        self.assertGreater(snap["error_count_60s"], 0,
                            "propagate를 끊어도 에러 카운팅은 살아있어야 한다")


class WatchdogHealthCriterionTests(unittest.TestCase):
    def test_data_flow_branch_wired(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn("if cfg.ws_trade_health_by_data_flow:", src)
        self.assertIn('f"no_data:message_count_60s={message_count_60s}"', src)

    def test_error_count_branch_kept_as_fallback(self):
        """플래그를 끄면 기존 error_count 기준으로 되돌아갈 수 있어야 한다(롤백 경로 보존)."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.main)
        self.assertIn("elif error_count_60s >= cfg.ws_max_error_count_60s:", src)


if __name__ == "__main__":
    unittest.main()


class AggressiveFillSeparationTests(unittest.TestCase):
    """[2026-08-16 사용자제안] "V2에서 스파이크 조기진입 기능만 빼는 건 어때" —
    태깅(관측)과 공격적 체결(행동)을 분리해, 나쁜 쪽(스프레드를 넘겨 체결)만 끌 수 있게 한다.

    근거: spike 태그 거래 5건 승률 40.0%/-0.18U vs 비spike 19건 78.9%/+1.08U.
    슬리피지가 기록된 2건 중 BMTUSDT SHORT는 +0.558%(불리)로, 레버리지 5배 기준 약
    -2.8%p ROE 핸디캡을 안고 시작했다 — 유동성 얇은 알트에서 스프레드 비용이 그대로
    손실로 잡힌 것으로 보인다."""

    def test_aggressive_fill_enabled_by_decision(self):
        """[2026-08-25] 코드 기본값은 여전히 false지만(test_code_default_is_off), 운영 .env는
        의도적으로 true다. 근거: 원장 7일 실측에서 early_entry_spike lane이 유일한 흑자
        구간(36건 승률 69.4% 건당 +0.0073 vs 비스파이크 -0.0066).
        [원복 조건] 스파이크 lane 건당 순손익이 마이너스로 돌아서면 false로 되돌리고
        이 테스트도 assertFalse로 되돌린다."""
        self.assertTrue(Config().spike_entry_aggressive_fill)

    def test_code_default_is_off(self):
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('spike_entry_aggressive_fill: bool = _bool("SPIKE_ENTRY_AGGRESSIVE_FILL", "false")', src)

    def test_entry_gated_by_flag(self):
        """태그가 있어도 플래그가 꺼져 있으면 aggressive로 넘기지 않아야 한다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        # [2026-08-25] micro_scalp lane이 추가되면서 호출부가 fast_entry_lane 변수를 거치도록
        # 바뀌었다. 옛 리터럴 문자열 대신 "스파이크 태그는 반드시 플래그와 AND된다"는 의도를
        # 검사한다 — 이 AND가 빠졌던 게 실제 라이브 버그였다.
        self.assertIn(
            'spike_aggressive = bool(candidate.get("early_entry_spike")) and cfg.spike_entry_aggressive_fill',
            src,
        )
        self.assertIn("fast_entry_lane = candidate.get(\"entry_lane\") == \"micro_scalp\" or spike_aggressive", src)
        self.assertIn("aggressive=fast_entry_lane", src)

    def test_tagging_still_recorded(self):
        """관측용 태깅 자체는 유지돼야 한다 — 표본을 쌓아 나중에 효과를 판정하기 위함."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn('early_entry_spike=bool(candidate.get("early_entry_spike"))', src)
