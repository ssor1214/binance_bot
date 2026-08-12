"""[2026-08-11 사용자요청] "순환매매 맛보기" — 보유시간 초과 + 이미 익절 상태일 때만 강제
확정하는 로직을 검증한다. 손실 중이거나 최소 ROE 미만이면 절대 개입하면 안 된다.
실 API 호출 없음."""
import time
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.force_profit_exit_max_hold_min = 5.0
    c.force_profit_exit_min_roe = 1.5
    c.take_profit_min = 3.0  # 정상 트레일링 시작선(비교용으로 더 높게 둠)
    c.stop_loss_pct = 6.0
    c.take_profit_hard_cap = 20.0
    c.small_profit_lock_balance_threshold = 0  # 이 테스트에서 다른 분기 배제
    c.small_profit_balance_threshold = 0
    return c


class ForceProfitExitTests(unittest.TestCase):
    def make_manager(self, config=None):
        return PositionManager(config or cfg())

    def test_forces_exit_when_time_and_profit_both_met(self):
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["BTCUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 6 * 60):  # 6분 경과
            # ROE 2% = 가격 0.5% 이동(레버리지4x)
            decision = pm.evaluate("BTCUSDT", mark_price=100.5)
        self.assertEqual(decision, "TAKE_PROFIT")

    def test_does_not_force_when_still_losing(self):
        """핵심: 손실 중이면 시간이 지나도 절대 강제청산하면 안 된다(정상 로직에 맡김)."""
        pm = self.make_manager()
        pm.track("ETHUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["ETHUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 10 * 60):  # 10분 경과
            decision = pm.evaluate("ETHUSDT", mark_price=99.5)  # ROE = -2%
        self.assertIsNone(decision)  # 손절선(-6%)에도 안 닿았으니 유지돼야 함

    def test_does_not_force_when_time_not_yet_elapsed(self):
        pm = self.make_manager()
        pm.track("SOLUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["SOLUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 2 * 60):  # 2분 경과(기준 5분 미달)
            decision = pm.evaluate("SOLUSDT", mark_price=100.5)  # ROE 2%(익절 상태긴 함)
        self.assertIsNone(decision)

    def test_does_not_force_when_profit_below_min_roe(self):
        pm = self.make_manager()
        pm.track("XRPUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["XRPUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 10 * 60):
            # ROE 1% (기준 1.5% 미달)
            decision = pm.evaluate("XRPUSDT", mark_price=100.25)
        self.assertIsNone(decision)

    def test_holds_when_momentum_still_continuing(self):
        """[2026-08-11 실거래 발견/회귀] 龙虾USDT 실측 — 시간+ROE 조건은 맞아도 모멘텀이 계속
        강하면 이번 주기엔 확정하지 말고 보류해야 한다(다음 상승분을 놓치지 않기 위함)."""
        pm = self.make_manager()
        pm.track("BICOUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["BICOUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 9 * 60):
            decision = pm.evaluate("BICOUSDT", mark_price=100.5, momentum_continuing=True)
        self.assertIsNone(decision)  # 조건 충족해도 모멘텀 지속 중이라 보류돼야 함

    def test_disabled_when_max_hold_zero(self):
        """[회귀] 기본값(0)이면 기존 동작 그대로 — 이 규칙이 전혀 개입하지 않아야 한다."""
        c = cfg()
        c.force_profit_exit_max_hold_min = 0.0
        pm = self.make_manager(c)
        pm.track("DOGEUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["DOGEUSDT"]
        with patch("bot.position_manager.time.time", return_value=pos.entered_at + 10 * 60):  # 10분 경과
            decision = pm.evaluate("DOGEUSDT", mark_price=102.0)  # ROE 8% (익절기준3% 넘음 -> 정상 트레일링만 armed)
        self.assertIsNone(decision)  # armed만 되고 아직 트레일링 확정은 아님(고점=현재라 하락폭0), 새 규칙은 꺼져있어 무관


if __name__ == "__main__":
    unittest.main()
