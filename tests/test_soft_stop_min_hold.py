"""[2026-08-11 사용자요청] check_hourly_soft_stop이 진입 직후 순간 노이즈로 즉시 발동하던
문제(LONG 실측: 4.8초/9.6초만에 SOFT_STOP)를 막는 최소 보유시간 가드를 검증한다.
실 API 호출 없음(FakeExchange로 대체)."""
import time
import unittest
from unittest.mock import patch

import pandas as pd

from bot.config import Config
from bot.main import check_hourly_soft_stop
from bot.position_manager import PositionManager


class FakeExchange:
    def __init__(self, mark_price):
        self.mark_price = mark_price

    def get_klines(self, symbol, limit=3, interval="1h"):
        if interval == "1h" and limit == 3:
            # check_hourly_soft_stop 맨 앞의 "새 1시간봉인지" 판별용 호출(limit=3)
            return pd.DataFrame([{"open_time": pd.Timestamp("2026-08-11 12:00:00")}] * 3)
        # 상위시간대 정합 계산용(limit=ema_slow+10 이상) — 꾸준히 하락하는 종가로
        # ema_fast < ema_slow(LONG 추세 미지지) 상황을 만든다.
        n = max(limit, 80)
        closes = [100.0 - i * 0.1 for i in range(n)]
        return pd.DataFrame({"close": closes})

    def get_mark_price(self, symbol):
        return self.mark_price


def cfg() -> Config:
    c = Config()
    c.soft_stop_min_loss_roe = 1.5
    c.soft_stop_mtf_min_ratio = 0.5
    c.stop_loss_pct = 6.0
    c.soft_stop_min_hold_sec = 60.0
    return c


class SoftStopMinHoldTests(unittest.TestCase):
    def test_blocked_within_min_hold_window(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["BTCUSDT"]
        # ROE -2%(손실, soft_stop 발동 문턱 -1.5%보다 깊음) 지만 진입 5초밖에 안 지남
        ex = FakeExchange(mark_price=99.5)
        with patch("bot.main.time.time", return_value=pos.entered_at + 5):
            result = check_hourly_soft_stop(ex, c, pm, "BTCUSDT", {})
        self.assertFalse(result)

    def test_allowed_after_min_hold_window(self):
        c = cfg()
        pm = PositionManager(c)
        pm.track("ETHUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["ETHUSDT"]
        ex = FakeExchange(mark_price=99.5)  # ROE -2%
        with patch("bot.main.time.time", return_value=pos.entered_at + 61):
            result = check_hourly_soft_stop(ex, c, pm, "ETHUSDT", {})
        self.assertTrue(result)

    def test_disabled_when_zero(self):
        """SOFT_STOP_MIN_HOLD_SEC=0이면 기존처럼 즉시 발동 가능해야 한다(회귀 방지)."""
        c = cfg()
        c.soft_stop_min_hold_sec = 0.0
        pm = PositionManager(c)
        pm.track("SOLUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["SOLUSDT"]
        ex = FakeExchange(mark_price=99.5)
        with patch("bot.main.time.time", return_value=pos.entered_at + 1):
            result = check_hourly_soft_stop(ex, c, pm, "SOLUSDT", {})
        self.assertTrue(result)

    def test_min_hold_block_does_not_consume_hourly_check_slot(self):
        """가드에 걸려도 hourly_check_state가 갱신되면 안 된다 — 유예기간이 지난 뒤 같은
        1시간봉 안에서도 재평가 기회가 남아있어야 한다."""
        c = cfg()
        pm = PositionManager(c)
        pm.track("XRPUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["XRPUSDT"]
        ex = FakeExchange(mark_price=99.5)
        state = {}
        with patch("bot.main.time.time", return_value=pos.entered_at + 5):
            check_hourly_soft_stop(ex, c, pm, "XRPUSDT", state)
        self.assertNotIn("XRPUSDT", state)  # 가드에 막혔으니 아직 이번 시간대 체크 안 한 상태


if __name__ == "__main__":
    unittest.main()
