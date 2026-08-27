"""[2026-08-17 야간 복기로 발견] 단기 재진입 차단 로그가 사실과 달랐던 문제.

`SYMBOL_COOLDOWN_BLOCK_MIN=0`(8/14 사용자요청 원복값, 버그 아님 - .env 693행)일 때도
"0분 동안 이 심볼 재진입을 짧게 차단합니다"를 WARNING으로 남겼다. cooldown_until이 now와
같아 실제로는 아무것도 막지 않는데 로그만 보면 차단된 것처럼 읽힌다.

실측: 2026-08-17 야간에 PORTALUSDT 2회 / HUSDT 1회 이 로그가 떴고 전부 즉시 재진입이
허용됐다. 그중 PORTALUSDT는 6거래 5손실 -1.24USDT였다. 복기하는 쪽에서 "차단이 걸렸다는데
왜 재진입됐나"를 추적하느라 시간을 버렸다.

동작(차단 안 함)은 사용자가 요청한 그대로 유지하고, 로그만 사실과 맞춘다.
"""
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


class SymbolCooldownLogHonestyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = patch("bot.position_manager.STATS_FILE",
                              Path(self._tmp.name) / "stats.json")
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _cfg(self, block_min):
        c = Config()
        c.symbol_cooldown_loss_count = 2
        c.symbol_cooldown_window_min = 30.0
        c.symbol_cooldown_block_min = block_min
        # 스트릭 기반 격리와 슬리피지 격리가 끼어들면 무엇이 블락을 세웠는지 흐려진다.
        c.symbol_blacklist_loss_threshold = 99
        c.symbol_blacklist_min_loss_streak = 99
        c.slippage_quarantine_multiplier = 99.0
        c.global_loss_streak_threshold = 99
        return c

    def _two_losses(self, pm):
        for _ in range(2):
            pm.record_result("TESTUSDT", pnl_pct_value=-1.5, pnl_usdt=-0.2,
                             side="LONG", leverage=4.0, origin="bot")

    def test_disabled_gate_does_not_claim_to_block(self):
        pm = PositionManager(self._cfg(0.0))
        with self.assertLogs("bot.position", level="INFO") as cm:
            self._two_losses(pm)
        text = "\n".join(cm.output)
        self.assertNotIn("짧게 차단합니다", text,
                         "차단하지 않는데 차단한다고 로그를 남기면 안 된다")
        self.assertIn("비활성", text, "비활성 상태임을 로그로 알려야 한다")
        self.assertFalse([r for r in cm.records
                          if r.levelno >= logging.WARNING and "차단" in r.getMessage()],
                         "차단이 없으면 WARNING으로 올리지 않는다")

    def test_disabled_gate_really_allows_reentry(self):
        """로그만 바꾸고 동작은 그대로여야 한다 - 8/14 사용자요청 원복값을 지킨다."""
        pm = PositionManager(self._cfg(0.0))
        self._two_losses(pm)
        until = pm.symbol_blacklist_until.get("TESTUSDT", 0.0)
        self.assertLessEqual(until, time.time() + 1e-6,
                             "block_min=0이면 미래 시각으로 블락이 걸려선 안 된다")

    def test_enabled_gate_still_blocks_and_warns(self):
        pm = PositionManager(self._cfg(10.0))
        with self.assertLogs("bot.position", level="WARNING") as cm:
            self._two_losses(pm)
        self.assertIn("짧게 차단합니다", "\n".join(cm.output))
        self.assertGreater(pm.symbol_blacklist_until.get("TESTUSDT", 0.0), time.time() + 500,
                           "10분 차단이 실제로 걸려야 한다")

    def test_single_loss_does_not_trigger_gate(self):
        pm = PositionManager(self._cfg(10.0))
        pm.record_result("TESTUSDT", pnl_pct_value=-1.5, pnl_usdt=-0.2,
                         side="LONG", leverage=4.0, origin="bot")
        self.assertLessEqual(pm.symbol_blacklist_until.get("TESTUSDT", 0.0), time.time() + 1e-6)


if __name__ == "__main__":
    unittest.main()
