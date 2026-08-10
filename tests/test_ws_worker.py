"""[2026-08-10] bot/ws_worker.py의 _atomic_write_json() 재시도 로직 단위테스트.
실거래 검증 중 Windows에서 os.replace()가 간헐적으로 PermissionError(백신 실시간 검사 등이
원인으로 추정)를 내는 게 실측돼 추가한 재시도 로직 — 이게 없으면 매 덤프 주기(2초)마다
15% 확률로 캐시 파일이 갱신 안 되는 문제가 있었다. 실제 파일시스템/네트워크는 건드리지 않고
Path.replace만 가짜로 실패시켜서 검증한다."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ws_worker import _atomic_write_json


class AtomicWriteJsonTests(unittest.TestCase):
    def test_writes_correctly_on_first_try(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            _atomic_write_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})

    def test_retries_on_transient_permission_error_and_eventually_succeeds(self):
        """[2026-08-10 실거래 검증 회귀테스트] 처음 몇 번은 PermissionError가 나도(백신 등의
        일시적 잠금 재현), 결국 성공해야 한다 — 첫 시도 실패로 바로 포기하면 안 됨."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            call_count = {"n": 0}
            real_replace = Path.replace

            def flaky_replace(self, target):
                call_count["n"] += 1
                if call_count["n"] < 3:
                    raise PermissionError("일시적 잠금(테스트 모의)")
                return real_replace(self, target)

            with patch.object(Path, "replace", flaky_replace), \
                 patch("bot.ws_worker.time.sleep"):  # 테스트가 실제로 대기하지 않게
                _atomic_write_json(path, {"b": 2})

            self.assertEqual(call_count["n"], 3)
            self.assertEqual(json.loads(path.read_text()), {"b": 2})

    def test_raises_after_exhausting_all_retries(self):
        """계속 실패하면(진짜로 뭔가 심각하게 잘못된 경우) 무한정 숨기지 않고 결국 예외를
        던져야 한다 — 호출부(dump_cache)가 이걸 잡아 로그로 남기고 넘어간다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            with patch.object(Path, "replace", side_effect=PermissionError("영구 실패(테스트 모의)")), \
                 patch("bot.ws_worker.time.sleep"):
                with self.assertRaises(PermissionError):
                    _atomic_write_json(path, {"c": 3})


if __name__ == "__main__":
    unittest.main()
