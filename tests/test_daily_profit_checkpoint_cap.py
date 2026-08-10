"""[2026-08-11 사용자요청] "50%, 100% 딱 2번만 물어보고 그 이후엔 계속 거래" 체크포인트
상한 로직을 검증한다. 실 API 호출 없음(TelegramNotifier는 토큰 미설정으로 send()가 no-op)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.config import Config
from bot.exchange import Exchange
from bot.main import update_daily_checkpoint
from bot.position_manager import PositionManager
from bot.telegram_notifier import TelegramNotifier


def cfg() -> Config:
    c = Config()
    c.daily_profit_target_pct = 50.0
    c.daily_profit_step_pct = 50.0
    c.daily_profit_max_checkpoints = 2
    c.daily_loss_limit_pct = 999.0  # 이 테스트에서 손실 서킷브레이커 부작용 방지
    c.telegram_bot_token = ""
    c.telegram_chat_id = ""
    return c


class DailyProfitCheckpointCapTests(unittest.TestCase):
    def make_env(self, config=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        c = config or cfg()
        pm = PositionManager(c)
        ex = Exchange.__new__(Exchange)
        tg = TelegramNotifier(c, ex, pm)
        daily_state = {"date": None, "start_balance": None, "next_threshold": None}
        return c, pm, tg, daily_state

    def _ask_count(self, tg):
        return tg.ask_daily_checkpoint.call_count

    def test_asks_at_50_and_100_but_not_150(self):
        c, pm, tg, daily_state = self.make_env()
        tg.ask_daily_checkpoint = Mock()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)

            pm.realized_pnl_usdt = 50.0  # +50%
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            self.assertEqual(self._ask_count(tg), 1)
            tg._awaiting_confirmation = False  # 실제 텔레그램 응답 처리 흉내

            pm.realized_pnl_usdt = 100.0  # +100%
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            self.assertEqual(self._ask_count(tg), 2)
            tg._awaiting_confirmation = False

            pm.realized_pnl_usdt = 150.0  # +150% — 상한(2회) 도달했으니 더 이상 물어보면 안 됨
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            self.assertEqual(self._ask_count(tg), 2)  # 여전히 2회

    def test_unlimited_when_max_checkpoints_zero(self):
        """[회귀] max_checkpoints=0(기본값)이면 기존처럼 무제한으로 계속 물어봐야 한다."""
        c = cfg()
        c.daily_profit_max_checkpoints = 0
        _, pm, tg, daily_state = self.make_env(c)
        tg.ask_daily_checkpoint = Mock()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            for pct in (50.0, 100.0, 150.0, 200.0):
                pm.realized_pnl_usdt = pct
                update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
                tg._awaiting_confirmation = False
        self.assertEqual(self._ask_count(tg), 4)

    def test_checkpoint_count_resets_on_new_day(self):
        c, pm, tg, daily_state = self.make_env()
        tg.ask_daily_checkpoint = Mock()
        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=1000.0):
            mock_date.today.return_value = "day1"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            pm.realized_pnl_usdt = 50.0
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
            tg._awaiting_confirmation = False
            pm.realized_pnl_usdt = 100.0
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=100.0)
        self.assertEqual(self._ask_count(tg), 2)

        with patch("bot.main.date") as mock_date, patch("bot.main.time.time", return_value=2000.0):
            mock_date.today.return_value = "day2"
            update_daily_checkpoint(None, pm, c, tg, daily_state, total_balance=150.0)
        self.assertEqual(daily_state["checkpoint_count"], 0)


if __name__ == "__main__":
    unittest.main()
