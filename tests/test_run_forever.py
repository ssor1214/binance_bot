import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_forever


class SupervisorHeartbeatTests(unittest.TestCase):
    def test_stale_heartbeat_from_previous_process_does_not_kill_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "heartbeat.txt"
            heartbeat_path.write_text("1000", encoding="utf-8")
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=3500, now=3501)
        self.assertEqual(age, 1)

    def test_new_process_heartbeat_becomes_the_freshest_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "heartbeat.txt"
            heartbeat_path.write_text("3510", encoding="utf-8")
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=3500, now=3515)
        self.assertEqual(age, 5)

    def test_missing_heartbeat_uses_process_start_as_initial_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "missing.txt"
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=3500, now=3799)
        self.assertEqual(age, 299)


class SupervisorRamAutoRecoveryTests(unittest.TestCase):
    """[2026-08-12 사용자요청] "RAM 문제로 멈추면 새로시작 + RAM 수급까지 자동으로" —
    free_ram_if_low()이 임계값 밑에서만, 그리고 KNOWN_SAFE_TO_CLOSE 목록의 프로세스만
    종료 시도하는지 검증한다. 실제 taskkill은 subprocess.run을 모킹해 절대 실행하지 않음."""

    def test_does_nothing_when_ram_is_sufficient(self):
        with patch.object(run_forever, "get_available_ram_mb", return_value=2000.0), \
             patch("run_forever.subprocess.run") as mock_run:
            run_forever.free_ram_if_low()
        mock_run.assert_not_called()

    def test_does_nothing_when_ram_check_fails(self):
        """조회 실패(None)면 감시 자체를 막지 않기 위해 아무 것도 안 한다."""
        with patch.object(run_forever, "get_available_ram_mb", return_value=None), \
             patch("run_forever.subprocess.run") as mock_run:
            run_forever.free_ram_if_low()
        mock_run.assert_not_called()

    def test_attempts_to_close_only_known_safe_processes_when_ram_is_low(self):
        with patch.object(run_forever, "get_available_ram_mb", return_value=200.0), \
             patch("run_forever.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            run_forever.free_ram_if_low()
        killed_targets = [call.args[0][2] for call in mock_run.call_args_list]  # taskkill /IM <name> /F
        self.assertEqual(killed_targets, run_forever.KNOWN_SAFE_TO_CLOSE)

    def test_never_targets_live_trading_or_dashboard_processes(self):
        """매매/모니터링 관련 프로세스명이 실수로라도 목록에 섞이지 않았는지 고정 검증."""
        forbidden_substrings = ["bot.main", "ws_worker", "dashboard", "run_forever", "python"]
        for name in run_forever.KNOWN_SAFE_TO_CLOSE:
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden.lower(), name.lower())


class FastCrashBackoffTests(unittest.TestCase):
    """[2026-08-13] IP밴 중 급속 재시작이 밴을 계속 연장시키던 사고 재발방지 검증."""

    def test_backoff_follows_table_in_order(self):
        for streak, expected in enumerate(run_forever.FAST_CRASH_BACKOFF_SEC):
            self.assertEqual(run_forever.compute_fast_crash_backoff_sec(streak), expected)

    def test_backoff_clamps_to_last_value_beyond_table_length(self):
        far_beyond = len(run_forever.FAST_CRASH_BACKOFF_SEC) + 10
        self.assertEqual(
            run_forever.compute_fast_crash_backoff_sec(far_beyond),
            run_forever.FAST_CRASH_BACKOFF_SEC[-1],
        )

    def test_backoff_never_negative_for_negative_streak(self):
        # streak은 항상 0 이상으로만 호출되지만, 방어적으로 음수가 들어와도 첫 값으로 클램프.
        self.assertEqual(
            run_forever.compute_fast_crash_backoff_sec(-5),
            run_forever.FAST_CRASH_BACKOFF_SEC[0],
        )

    def test_backoff_table_strictly_increasing(self):
        table = run_forever.FAST_CRASH_BACKOFF_SEC
        self.assertTrue(all(table[i] < table[i + 1] for i in range(len(table) - 1)))

    def test_first_backoff_matches_normal_restart_delay(self):
        """첫 크래시는 지금까지와 동일하게 즉각 재시도(RESTART_DELAY_SEC)해야 정상 일시적
        오류(네트워크 순간 끊김 등)에서 불필요하게 느려지지 않는다."""
        self.assertEqual(run_forever.FAST_CRASH_BACKOFF_SEC[0], run_forever.RESTART_DELAY_SEC)


if __name__ == "__main__":
    unittest.main()
