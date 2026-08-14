"""[2026-08-11 사용자요청] 진입 직후 유예기간 동안 손절폭을 넓혀서 순간적 되돌림(휩쏘)에
스탑이 스치는 걸 줄이는 compute_stop_loss_pct()를 검증한다. 실 API 호출 없음."""
import time
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.main import compute_stop_loss_pct
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.stop_loss_pct = 6.0
    c.short_stop_loss_pct = 3.0
    c.stop_loss_grace_sec = 45.0
    c.stop_loss_grace_widen_mult = 1.5
    return c


class StopLossGracePeriodTests(unittest.TestCase):
    def test_disabled_when_grace_sec_zero(self):
        c = cfg()
        c.stop_loss_grace_sec = 0.0
        pct, widened = compute_stop_loss_pct(c, "SHORT", entered_at=time.time())
        self.assertEqual(pct, 3.0)  # SHORT전용값 그대로, 유예 없음
        self.assertFalse(widened)

    def test_entered_at_none_means_just_now_always_widened(self):
        """entered_at=None(진입 시점)이면 유예기간이 항상 적용된 것으로 본다."""
        c = cfg()
        pct, widened = compute_stop_loss_pct(c, "SHORT", entered_at=None)
        self.assertEqual(pct, 3.0 * 1.5)
        self.assertTrue(widened)

    def test_within_grace_window_widened(self):
        c = cfg()
        with patch("bot.main.time.time", return_value=1000.0 + 10):  # 10초 경과(45초 미만)
            pct, widened = compute_stop_loss_pct(c, "LONG", entered_at=1000.0)
        self.assertEqual(pct, 6.0 * 1.5)
        self.assertTrue(widened)

    def test_after_grace_window_not_widened(self):
        c = cfg()
        with patch("bot.main.time.time", return_value=1000.0 + 50):  # 50초 경과(45초 초과)
            pct, widened = compute_stop_loss_pct(c, "LONG", entered_at=1000.0)
        self.assertEqual(pct, 6.0)
        self.assertFalse(widened)

    def test_long_ignores_short_stop_loss_pct(self):
        c = cfg()
        c.stop_loss_grace_sec = 0.0
        pct, widened = compute_stop_loss_pct(c, "LONG", entered_at=time.time())
        self.assertEqual(pct, 6.0)  # LONG은 항상 공용 stop_loss_pct


class PositionManagerGraceAwareEvaluateTests(unittest.TestCase):
    """[2026-08-12 실거래 사고 재발방지] PositionManager.evaluate()가 내부적으로 쓰는
    _stop_loss_pct_for()가 compute_stop_loss_pct()와 동일하게 유예기간을 반영하는지
    검증한다. ONEUSDT(SHORT, 106초만에 -3.04%로 손절됨 — 원래 180초 유예중이면 6%까지
    버텨야 했음)/BEATUSDT(LONG, 44초만에 -6.39%로 손절됨 — 유예중이면 12%까지 버텨야 했음)
    사고를 재현/재발방지하는 테스트."""

    def make_manager(self):
        c = cfg()
        c.take_profit_min = 50.0
        c.take_profit_hard_cap = 50.0
        c.small_profit_lock_balance_threshold = 0
        c.small_profit_balance_threshold = 0
        c.force_profit_exit_max_hold_min = 0.0
        return PositionManager(c)

    def test_short_within_grace_does_not_stop_at_base_threshold(self):
        """SHORT, 유예기간(45초) 이내, 기본 3% 손실이어도 유예중이면 손절 아님(4.5%까지 버텨야 함)."""
        pm = self.make_manager()
        pm.track("ONEUSDT", "SHORT", entry_price=100.0, quantity=1.0, leverage=1.0)
        pm.positions["ONEUSDT"].entered_at = 1000.0
        with patch("bot.position_manager.time.time", return_value=1000.0 + 10):  # 10초 경과
            decision = pm.evaluate("ONEUSDT", mark_price=103.0)  # ROE -3% (기본 SHORT 임계값)
        self.assertIsNone(decision, "유예기간 중에는 기본폭(3%)에서 손절되면 안 된다")

    def test_short_within_grace_stops_at_widened_threshold(self):
        """SHORT, 유예기간 이내, 넓혀진 4.5%(3%*1.5)를 넘으면 그때는 손절돼야 한다."""
        pm = self.make_manager()
        pm.track("ONEUSDT", "SHORT", entry_price=100.0, quantity=1.0, leverage=1.0)
        pm.positions["ONEUSDT"].entered_at = 1000.0
        with patch("bot.position_manager.time.time", return_value=1000.0 + 10):
            decision = pm.evaluate("ONEUSDT", mark_price=104.6)  # ROE -4.6% > 4.5% 넓혀진 임계값
        self.assertEqual(decision, "STOP_LOSS")

    def test_short_after_grace_stops_at_base_threshold(self):
        """유예기간(45초) 경과 후에는 다시 기본 3%에서 손절돼야 한다."""
        pm = self.make_manager()
        pm.track("ONEUSDT", "SHORT", entry_price=100.0, quantity=1.0, leverage=1.0)
        pm.positions["ONEUSDT"].entered_at = 1000.0
        with patch("bot.position_manager.time.time", return_value=1000.0 + 50):  # 50초 경과
            decision = pm.evaluate("ONEUSDT", mark_price=103.5)  # ROE -3.5% > 기본 3%
        self.assertEqual(decision, "STOP_LOSS")

    def test_long_within_grace_does_not_stop_at_base_threshold(self):
        """LONG, 유예기간 이내, 기본 6% 손실이어도 유예중이면 손절 아님(9%까지 버텨야 함)."""
        pm = self.make_manager()
        pm.track("BEATUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)
        pm.positions["BEATUSDT"].entered_at = 2000.0
        with patch("bot.position_manager.time.time", return_value=2000.0 + 5):  # 5초 경과
            decision = pm.evaluate("BEATUSDT", mark_price=93.7)  # ROE -6.3% (기본 임계값 근처)
        self.assertIsNone(decision, "유예기간 중에는 기본폭(6%)에서 손절되면 안 된다")


class SyncExistingPositionsPreservesGraceStateTests(unittest.TestCase):
    """[2026-08-14 실측 사고] 재시작 시 sync_existing_positions()가 entered_at을 '지금'으로
    새로 채우고 stop_loss_widened를 항상 False로 초기화해서, 진입 유예기간(180초) 동안
    넓혀둔 손절폭(~20% ROE)이 재시작 이후 영원히 원래 폭(8%)으로 안 좁혀지던 버그
    (APRUSDT 실사고, -20.1% ROE 손절) 재발방지 검증."""

    def test_track_with_explicit_entered_at_overrides_default(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("ABCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0, entered_at=500.0)
        self.assertEqual(pm.positions["ABCUSDT"].entered_at, 500.0)

    def test_track_without_entered_at_defaults_to_now(self):
        """dataclass의 default_factory=time.time은 정의 시점에 함수를 바인딩하므로
        bot.position_manager.time.time 패치로는 가로챌 수 없다 — 대신 실제 현재시각과
        거의 같은지(수 초 오차 이내)로 검증한다."""
        c = cfg()
        pm = PositionManager(c)
        before = time.time()
        pm.track("ABCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        after = time.time()
        self.assertTrue(before <= pm.positions["ABCUSDT"].entered_at <= after)

    def test_track_stop_loss_widened_flag_passthrough(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("ABCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0,
                  entered_at=500.0, stop_loss_widened=True)
        self.assertTrue(pm.positions["ABCUSDT"].stop_loss_widened)

    def test_track_defaults_stop_loss_widened_to_false(self):
        """execute_entry 등 신규 진입 경로는 이 인자를 안 넘기므로 기존과 동일하게 False."""
        c = cfg()
        pm = PositionManager(c)
        pm.track("ABCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        self.assertFalse(pm.positions["ABCUSDT"].stop_loss_widened)

    def test_restored_position_past_grace_narrows_immediately_on_next_check(self):
        """재시작으로 복원된 포지션(entered_at=원래 진입시각, widened=True)이 이미 유예기간을
        지난 상태라면, compute_stop_loss_pct가 즉시 '더 이상 넓지 않음'을 반환해야 한다
        (main.py의 reconcile 루프가 이걸 보고 바로 원래 폭으로 좁힌다)."""
        c = cfg()  # stop_loss_grace_sec = 45.0
        pm = PositionManager(c)
        # 진입한 지 100초 됐다고 가정(유예 45초를 훨씬 지남) — 재시작 시점이 여기 해당
        original_entered_at = 1000.0
        pm.track("ABCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0,
                  entered_at=original_entered_at, stop_loss_widened=True)
        with patch("bot.main.time.time", return_value=original_entered_at + 100):
            from bot.main import compute_stop_loss_pct
            pct, still_widened = compute_stop_loss_pct(c, "LONG", pm.positions["ABCUSDT"].entered_at)
        self.assertFalse(still_widened, "재시작 전 유예기간이 이미 끝났다면 복원 직후에도 넓은 상태가 아니어야 한다")
        self.assertEqual(pct, 6.0)  # 원래 폭(넓히지 않은 값)


if __name__ == "__main__":
    unittest.main()
