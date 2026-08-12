"""[2026-08-11 사용자요청] 진입 직후 유예기간 동안 손절폭을 넓혀서 순간적 되돌림(휩쏘)에
스탑이 스치는 걸 줄이는 compute_stop_loss_pct()를 검증한다. 실 API 호출 없음."""
import time
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.main import compute_stop_loss_pct


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


if __name__ == "__main__":
    unittest.main()
