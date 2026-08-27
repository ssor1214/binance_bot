import os
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

    def test_transient_read_failure_after_long_healthy_run_does_not_look_stale(self):
        """[2026-08-15 실측 오탐] 325분(19514초) 정상 운행 중이던 프로세스가, 하트비트
        파일을 하필 write_text() 도중(비원자적 쓰기) 읽어서 파싱 실패(heartbeat_timestamp()
        가 None)를 만난 순간 process_started_at까지 되돌아가버려 "19514초 동안 정지"로
        오판, 실제로는 몇십초 전까지 정상 갱신 중이던 프로세스를 강제종료한 사고가 있었다.
        last_known_at(직전에 성공적으로 읽은 값)을 process_started_at보다 우선 사용해야
        이런 찰나의 읽기 실패 한 번으로 나이가 프로세스 가동시간까지 튀지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "heartbeat.txt"
            heartbeat_path.write_text("", encoding="utf-8")  # write_text() 도중을 재현(파싱 실패)
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                # 프로세스는 5시간 넘게 떠 있었지만, 마지막으로 성공했던 하트비트는 3초 전.
                age = run_forever.heartbeat_age_sec(
                    process_started_at=1000, now=19514 + 1003, last_known_at=19514 + 1000
                )
        self.assertEqual(age, 3)  # 19514초(=process_started_at 기준 오탐값)가 아니라 3초여야 함

    def test_genuinely_frozen_heartbeat_is_still_detected_even_with_last_known_at(self):
        """last_known_at을 들고 있어도, 진짜로 하트비트가 멈추면(last_known_at 자체가 계속
        오래돼감) 정상적으로 오래된 것으로 잡혀야 한다 — 오탐 방지가 실제 정지 감지를
        무력화하면 안 된다."""
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat_path = Path(tmp) / "heartbeat.txt"
            heartbeat_path.write_text("1000", encoding="utf-8")
            with patch.object(run_forever, "HEARTBEAT_PATH", heartbeat_path):
                age = run_forever.heartbeat_age_sec(process_started_at=900, now=1700, last_known_at=1000)
        self.assertEqual(age, 700)


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
        # [2026-08-14 점검] subprocess.run은 이미 모킹되어 실제 taskkill/실프로세스 종료는
        # 절대 일어나지 않는다(핵심 안전조건, 이 테스트가 검증하는 바로 그것) — 다만 log()가
        # 실제 supervisor.log 파일에 "ChatGPT.exe 종료 완료" 같은 오해 소지 있는 줄을 남기고
        # 콘솔에도 출력해 마치 실제로 죽인 것처럼 보였다(실측: 라이브 감시와 무관한 순수
        # 착시, 기능 버그는 아님). log()도 함께 모킹해 테스트가 실제 로그 파일을 더럽히지
        # 않도록 정리한다.
        with patch.object(run_forever, "get_available_ram_mb", return_value=200.0), \
             patch("run_forever.subprocess.run") as mock_run, \
             patch.object(run_forever, "log"):
            mock_run.return_value.returncode = 0
            run_forever.free_ram_if_low()
        killed_targets = [call.args[0][2] for call in mock_run.call_args_list]  # taskkill /IM <name> /F
        self.assertEqual(killed_targets, run_forever.KNOWN_SAFE_TO_CLOSE)

    def test_does_not_kill_anything_when_safe_target_list_is_empty(self):
        with patch.object(run_forever, "get_available_ram_mb", return_value=200.0), \
             patch.object(run_forever, "KNOWN_SAFE_TO_CLOSE", []), \
             patch("run_forever.subprocess.run") as mock_run, \
             patch.object(run_forever, "log"):
            run_forever.free_ram_if_low()
        mock_run.assert_not_called()

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


class SupervisorSingleInstanceLockTests(unittest.TestCase):
    def test_second_supervisor_instance_exits_with_duplicate_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".run_forever.lock"
            # [2026-08-25] 전역 커널 뮤텍스를 그대로 쓰면 라이브 봇이 돌고 있을 때
            # 첫 acquire부터 실패해서 "봇이 켜져 있으면 깨지는 테스트"가 된다.
            # 테스트 전용 이름으로 격리한다.
            unique_mutex = r"Local\BinanceFuturesBotSupervisorTest%d" % os.getpid()
            with patch.object(run_forever, "SUPERVISOR_LOCK_PATH", lock_path),                  patch.object(run_forever, "SUPERVISOR_MUTEX_NAME", unique_mutex),                  patch.object(run_forever, "_SUPERVISOR_MUTEX", None):
                first = run_forever.acquire_supervisor_lock()
                with self.assertRaises(SystemExit) as ctx:
                    run_forever.acquire_supervisor_lock()
                first.close()
        self.assertEqual(ctx.exception.code, run_forever.DUPLICATE_INSTANCE_EXIT_CODE)


if __name__ == "__main__":
    unittest.main()
