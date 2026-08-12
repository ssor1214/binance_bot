"""[2026-08-11 사용자요청] SHORT만 손절폭을 따로 조일 수 있게 SHORT_STOP_LOSS_PCT를
추가했다 — LONG은 공용 stop_loss_pct 그대로 영향받지 않고, SHORT만 별도 값을 쓰는지,
0(기본값)이면 기존처럼 공용값으로 폴백하는지 검증한다. 실 API 호출 없음."""
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.short_stop_loss_pct = 3.0
    c.take_profit_min = 20.0  # 이 테스트에서 익절 분기 배제
    c.take_profit_hard_cap = 20.0
    c.small_profit_lock_balance_threshold = 0
    c.small_profit_balance_threshold = 0
    c.force_profit_exit_max_hold_min = 0.0
    c.stop_loss_grace_sec = 0.0  # 이 테스트는 유예기간 없이 기본폭 자체를 검증(유예는 별도 테스트파일)
    return c


class ShortStopLossTests(unittest.TestCase):
    def make_manager(self, config=None):
        return PositionManager(config or cfg())

    def test_short_uses_short_stop_loss_pct(self):
        """SHORT는 SHORT_STOP_LOSS_PCT(3%)에서 손절돼야 한다 — 공용 6%까지 안 기다림."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "SHORT", entry_price=100.0, quantity=1.0, leverage=1.0)
        # ROE -4% (가격 +4%, SHORT라 손실) — 공용 6%엔 안 닿지만 SHORT전용 3%는 넘음
        decision = pm.evaluate("BTCUSDT", mark_price=104.0)
        self.assertEqual(decision, "STOP_LOSS")

    def test_long_still_uses_shared_stop_loss_pct(self):
        """LONG은 SHORT_STOP_LOSS_PCT 설정과 무관하게 공용 stop_loss_pct(6%)를 그대로 써야 한다."""
        pm = self.make_manager()
        pm.track("ETHUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)
        # ROE -4% (가격 -4%) — LONG은 공용 6% 기준이라 아직 손절 아님
        decision = pm.evaluate("ETHUSDT", mark_price=96.0)
        self.assertIsNone(decision)
        # ROE -6.5% — 공용 6%를 넘어서야 손절
        decision = pm.evaluate("ETHUSDT", mark_price=93.5)
        self.assertEqual(decision, "STOP_LOSS")

    def test_zero_falls_back_to_shared_stop_loss_pct(self):
        """SHORT_STOP_LOSS_PCT=0(기본값)이면 SHORT도 기존처럼 공용 stop_loss_pct를 그대로 써야 한다."""
        c = cfg()
        c.short_stop_loss_pct = 0.0
        pm = self.make_manager(c)
        pm.track("SOLUSDT", "SHORT", entry_price=100.0, quantity=1.0, leverage=1.0)
        # ROE -4% — SHORT전용값 없으니 공용 6% 기준, 아직 손절 아님
        decision = pm.evaluate("SOLUSDT", mark_price=104.0)
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
