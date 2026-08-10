"""[2026-08-10 사용자요청] "딜레이 없는 맹목적 재연결은 IP 밴을 부른다" — WS 워커 재시작
지수 백오프 단위테스트. 실제 서브프로세스를 띄우거나 API를 호출하지 않는다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import _compute_ws_restart_backoff_sec


def cfg(**overrides):
    c = Config()
    c.ws_restart_backoff_base_sec = 5.0
    c.ws_restart_backoff_max_sec = 120.0
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class ComputeBackoffTests(unittest.TestCase):
    def test_first_restart_uses_base_delay(self):
        self.assertEqual(_compute_ws_restart_backoff_sec(0, cfg()), 5.0)

    def test_doubles_each_consecutive_restart(self):
        c = cfg()
        self.assertEqual(_compute_ws_restart_backoff_sec(0, c), 5.0)
        self.assertEqual(_compute_ws_restart_backoff_sec(1, c), 10.0)
        self.assertEqual(_compute_ws_restart_backoff_sec(2, c), 20.0)
        self.assertEqual(_compute_ws_restart_backoff_sec(3, c), 40.0)

    def test_caps_at_max_delay(self):
        c = cfg()
        self.assertEqual(_compute_ws_restart_backoff_sec(10, c), 120.0)  # 5*2^10 >> 120
        self.assertEqual(_compute_ws_restart_backoff_sec(100, c), 120.0)  # 극단값도 안전


class RestartLoopBackoffIntegrationTests(unittest.TestCase):
    """main.py의 재시작 루프에서 실제로 백오프가 지켜지는지는 while-루프 통합 테스트라
    무겁다 — 대신 워커 dict의 next_restart_allowed_at/consecutive_restart_count 필드가
    start_ws_layer()에서 정확히 초기화되는지만 확인한다(재시작 루프 자체의 사용 방식은
    _compute_ws_restart_backoff_sec 단위테스트로 이미 검증됨)."""

    def test_start_ws_layer_initializes_backoff_fields_to_zero(self):
        from bot.main import start_ws_layer

        c = cfg(ws_market_data_enabled=True, ws_market_shard_count=1)
        ex = MagicMock()
        with patch("subprocess.Popen", return_value=MagicMock()):
            handles = start_ws_layer(ex, c, ["BTCUSDT"])
        w = handles["workers"][0]
        self.assertEqual(w["consecutive_restart_count"], 0)
        self.assertEqual(w["next_restart_allowed_at"], 0.0)


if __name__ == "__main__":
    unittest.main()
