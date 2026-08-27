"""[2026-08-25 버그수정] 중첩 감시기 판정이 정상적인 수동 기동을 죽이던 문제.

원래는 부모 프로세스의 CommandLine에 "run_forever.py"와 "binance-futures-bot"이 들어 있으면
무조건 중첩으로 판정했다. 그런데 사람이 쓰는 런처(PowerShell Start-Process,
bash `cd <repo> && python run_forever.py`)의 명령줄에도 그 두 문자열이 그대로 들어간다.
그래서 정상 기동이 전부 "중첩 감시기"로 오판돼 즉시 종료됐다.
진짜 중첩(multiprocessing spawn 재실행)은 부모가 반드시 python 프로세스다.
"""
import unittest
from unittest.mock import patch

import run_forever


def _with_parent(name, cmdline):
    return patch.object(run_forever.subprocess, "check_output", return_value=f"{name}|{cmdline}\n")


REPO = r"C:\Users\lg\Desktop\binance-futures-bot"


class SupervisorParentGuardTests(unittest.TestCase):
    def test_powershell_launcher_is_not_nested(self):
        """Start-Process 명령줄에 두 문자열이 다 들어가도 중첩이 아니다 — 이게 실제 버그였다."""
        cmd = f'powershell -NoProfile -Command Start-Process python.exe -ArgumentList "run_forever.py" -WorkingDirectory "{REPO}"'
        with _with_parent("powershell.exe", cmd):
            self.assertFalse(run_forever.has_same_supervisor_parent())

    def test_bash_launcher_is_not_nested(self):
        cmd = f'bash -c cd "{REPO}" && python run_forever.py'
        with _with_parent("bash.exe", cmd):
            self.assertFalse(run_forever.has_same_supervisor_parent())

    def test_real_python_parent_is_nested(self):
        """진짜 중첩 — 부모가 python으로 run_forever.py를 돌리고 있는 경우는 계속 막아야 한다."""
        cmd = '"' + REPO + r'\.venv\Scripts\python.exe' + '" run_forever.py'
        with _with_parent("python.exe", cmd):
            self.assertTrue(run_forever.has_same_supervisor_parent())

    def test_python_parent_of_other_repo_is_not_nested(self):
        with _with_parent("python.exe", r'"C:\other\python.exe" run_forever.py'):
            self.assertFalse(run_forever.has_same_supervisor_parent())

    def test_lookup_failure_defaults_to_not_nested(self):
        """조회 실패 시 기동을 막으면 안 된다(봇이 안 뜨는 쪽이 더 위험)."""
        with patch.object(run_forever.subprocess, "check_output", side_effect=OSError):
            self.assertFalse(run_forever.has_same_supervisor_parent())


if __name__ == "__main__":
    unittest.main()
