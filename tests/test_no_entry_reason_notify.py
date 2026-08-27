"""[2026-08-17 사용자요청] 진입 후보가 0개로 이어질 때 탈락 사유를 텔레그램으로 알린다.

계기: 사용자가 "거래 체결 안 되면 사유를 전달하기로 했는데 조용하다"고 지적.
확인해보니 tg.notify_scan_candidates()가 `if not candidates: return` 구조라 후보가 1개
이상일 때만 발송했다 — 정작 "왜 거래가 없는지" 가장 알고 싶은 침묵 구간에는 아무 알림도
가지 않는 구조였다(실측: 12:52 후보 1개 발송 이후 계속 0개라 침묵).

이 테스트는 그 공백을 메우는 maybe_notify_no_entry_reason()을 검증한다.
"""
import unittest
from unittest.mock import MagicMock

from bot.config import Config


class NoEntryReasonConfigTests(unittest.TestCase):
    def test_env_enabled_with_10min_throttle(self):
        self.assertEqual(Config().no_entry_reason_notify_sec, 600.0)

    def test_code_default_is_disabled(self):
        """환경변수가 없는 신규 배포 환경에서는 꺼져 있어야 한다(알림 스팸 방지)."""
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('no_entry_reason_notify_sec: float = _float("NO_ENTRY_REASON_NOTIFY_SEC", 0.0)', src)


class NoEntryReasonNotifyTests(unittest.TestCase):
    def setUp(self):
        from bot import main as bot_main
        self.m = bot_main
        self.cfg = Config()
        self.cfg.no_entry_reason_notify_sec = 600.0
        self.m._ENTRY_FUNNEL_COUNTS.clear()
        self.m._NO_ENTRY_NOTIFY_STATE.update({"last_sent_at": 0.0, "since": 0.0})

    def tearDown(self):
        self.m._ENTRY_FUNNEL_COUNTS.clear()
        self.m._NO_ENTRY_NOTIFY_STATE.update({"last_sent_at": 0.0, "since": 0.0})

    def _tg(self):
        return MagicMock()

    def test_disabled_when_interval_zero(self):
        self.cfg.no_entry_reason_notify_sec = 0.0
        tg = self._tg()
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=False)
        tg.send.assert_not_called()

    def test_does_not_send_before_interval(self):
        """매 스캔(약 30초)마다 보내면 스팸이 된다 — 스로틀이 핵심."""
        tg = self._tg()
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=False)  # 기준시각만 설정
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=False)
        tg.send.assert_not_called()

    def test_sends_after_interval_with_reason_breakdown(self):
        import time
        self.m._ENTRY_FUNNEL_COUNTS.update({"signal_missing": 900, "whipsaw": 60, "range_position": 40})
        self.m._NO_ENTRY_NOTIFY_STATE["since"] = time.time() - 1800
        self.m._NO_ENTRY_NOTIFY_STATE["last_sent_at"] = time.time() - 700
        tg = self._tg()
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=False)
        tg.send.assert_called_once()
        msg = tg.send.call_args[0][0]
        self.assertIn("진입 없음", msg)
        self.assertIn("진입신호 자체가 없음", msg, "stage 코드명 대신 한글 설명이 보여야 한다")
        self.assertIn("900건", msg)
        self.assertIn("90.0%", msg)

    def test_counter_cleared_after_send(self):
        """다음 구간을 새로 세야 한다 — 안 비우면 누적치가 계속 커져 비중이 왜곡된다."""
        import time
        self.m._ENTRY_FUNNEL_COUNTS.update({"signal_missing": 10})
        self.m._NO_ENTRY_NOTIFY_STATE["last_sent_at"] = time.time() - 700
        self.m.maybe_notify_no_entry_reason(self._tg(), self.cfg, had_candidates=False)
        self.assertEqual(sum(self.m._ENTRY_FUNNEL_COUNTS.values()), 0)

    def test_candidates_present_resets_and_stays_silent(self):
        """후보가 나온 주기는 침묵 구간이 아니다 — 알림 없이 타이머만 리셋."""
        import time
        self.m._ENTRY_FUNNEL_COUNTS.update({"signal_missing": 10})
        self.m._NO_ENTRY_NOTIFY_STATE["last_sent_at"] = time.time() - 700
        tg = self._tg()
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=True)
        tg.send.assert_not_called()
        self.assertEqual(sum(self.m._ENTRY_FUNNEL_COUNTS.values()), 0)

    def test_handles_empty_counter(self):
        """스캔 기록이 없으면 그 사실 자체를 알려야 한다(심볼목록/데이터 이상 신호)."""
        import time
        self.m._NO_ENTRY_NOTIFY_STATE["last_sent_at"] = time.time() - 700
        tg = self._tg()
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=False)
        tg.send.assert_called_once()
        self.assertIn("스캔 기록 없음", tg.send.call_args[0][0])

    def test_send_failure_does_not_raise(self):
        """알림 실패가 실매매를 막으면 안 된다 — 이 저장소 관측성 경로 원칙."""
        import time
        self.m._NO_ENTRY_NOTIFY_STATE["last_sent_at"] = time.time() - 700
        tg = self._tg()
        tg.send.side_effect = RuntimeError("telegram down")
        self.m.maybe_notify_no_entry_reason(tg, self.cfg, had_candidates=False)  # 예외 전파 없어야 함


class FunnelCounterWiringTests(unittest.TestCase):
    def test_record_event_increments_counter(self):
        """집계는 파일 재읽기 없이 인메모리 카운터로 — 관측 경로가 실매매를 느리게 하면 안 된다."""
        from bot import main as bot_main
        bot_main._ENTRY_FUNNEL_COUNTS.clear()
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            bot_main.record_entry_funnel_event(
                "BTCUSDT", "signal_missing", path=pathlib.Path(d) / "f.jsonl"
            )
        self.assertEqual(bot_main._ENTRY_FUNNEL_COUNTS["signal_missing"], 1)
        bot_main._ENTRY_FUNNEL_COUNTS.clear()

    def test_wired_into_scan_loop(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main)
        self.assertIn("maybe_notify_no_entry_reason(tg, cfg, bool(candidates))", src)


if __name__ == "__main__":
    unittest.main()
