"""[2026-08-11 사용자요청] 일일 손실 서킷브레이커를 "날짜 바뀔 때까지"에서
"트리거 후 daily_loss_pause_hours(기본 4시간) 고정 정지"로 재설계한 부분을 검증한다.
실 API 호출 없음(TelegramNotifier는 토큰 미설정으로 send()가 no-op)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.exchange import Exchange
from bot.main import update_daily_checkpoint
from bot.position_manager import PositionManager
from bot.telegram_notifier import TelegramNotifier


def cfg() -> Config:
    c = Config()
    c.daily_loss_limit_pct = 15.0
    c.daily_loss_pause_hours = 4.0
    c.daily_profit_target_pct = 999.0  # 이 테스트에서 수익 체크포인트 알림 부작용 방지
    # [중요] Config()가 .env를 그대로 로드하면 라이브 텔레그램 토큰이 섞여 들어와
    # TelegramNotifier.send()가 실제로 텔레그램 API를 호출하려 든다(느려지고 실 네트워크
    # 사용) — 테스트에서는 반드시 비활성화한다.
    c.telegram_bot_token = ""
    c.telegram_chat_id = ""
    return c


class DailyLossPauseDurationTests(unittest.TestCase):
    def make_env(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        c = cfg()
        pm = PositionManager(c)
        ex = Exchange.__new__(Exchange)  # __init__ 건드리지 않음(REST 호출 없음)
        tg = TelegramNotifier(c, ex, pm)
        daily_state = {"date": None, "start_balance": None, "next_threshold": None}
        return c, pm, tg, daily_state

    def test_no_trigger_when_within_limit(self):
        c, pm, tg, daily_state = self.make_env()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)  # 날짜 초기화
            pm.realized_pnl_usdt = -5.0  # -5% 손실, 한도(15%) 미만
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        self.assertFalse(tg.trading_paused)
        self.assertFalse(daily_state.get("loss_limit_triggered"))

    def test_trigger_pauses_for_fixed_hours_not_until_midnight(self):
        c, pm, tg, daily_state = self.make_env()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            pm.realized_pnl_usdt = -16.0  # -16% 손실, 한도(15%) 초과
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        self.assertTrue(tg.trading_paused)
        self.assertTrue(daily_state["loss_limit_triggered"])
        self.assertAlmostEqual(daily_state["loss_limit_pause_until"], 1000.0 + 4 * 3600)

    def test_auto_resumes_after_pause_hours_elapsed_same_day(self):
        """[핵심] 날짜가 안 바뀌어도(자정 전이라도) 4시간 지나면 자동 재개돼야 한다 —
        기존 방식(날짜 바뀔 때까지)이었다면 이 테스트는 실패했을 것이다."""
        c, pm, tg, daily_state = self.make_env()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            pm.realized_pnl_usdt = -16.0
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        self.assertTrue(tg.trading_paused)

        # 3시간 59분 경과 — 아직 재개되면 안 됨
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0 + 4 * 3600 - 60):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        self.assertTrue(tg.trading_paused)

        # 4시간 1초 경과 — 날짜(day1)는 안 바뀌었지만 자동 재개돼야 함
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0 + 4 * 3600 + 1):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        self.assertFalse(tg.trading_paused)
        self.assertFalse(daily_state["loss_limit_triggered"])

    def test_does_not_retrigger_pause_reset_every_cycle(self):
        """한 번 트리거된 뒤 계속 손실 중이어도(-16%를 유지) pause_until을 계속 갱신해서
        무한 연장시키면 안 된다 — 최초 트리거 시각 기준 4시간이어야 한다."""
        c, pm, tg, daily_state = self.make_env()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            pm.realized_pnl_usdt = -16.0
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        first_until = daily_state["loss_limit_pause_until"]

        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0 + 3600):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)  # 여전히 -16%
        self.assertEqual(daily_state["loss_limit_pause_until"], first_until)


if __name__ == "__main__":
    unittest.main()
