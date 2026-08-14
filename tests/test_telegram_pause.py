"""[2026-08-14 사용자요청] "텔레그램 자체에서도 봇 일시정지 기능(상시 버튼)이 필요해" —
하단 고정메뉴에 상시 노출되는 일시정지/재개 버튼과 텍스트 명령을 검증한다. 실제 텔레그램
API는 호출하지 않는다(enabled=False, send()가 즉시 no-op)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.telegram_notifier import TelegramNotifier


def make_notifier():
    cfg = Config()
    # telegram 토큰/채팅ID를 비워서 enabled=False로 만든다 — send()가 실제 API를 호출하지
    # 않고 즉시 리턴하므로 네트워크 없이 순수 상태(trading_paused)만 검증 가능하다.
    cfg.telegram_bot_token = ""
    cfg.telegram_chat_id = ""
    return TelegramNotifier(cfg, ex=None, pm=None)


class TelegramPauseButtonTests(unittest.TestCase):
    def test_starts_not_paused(self):
        tg = make_notifier()
        self.assertFalse(tg.trading_paused)

    def test_pause_button_text_sets_paused(self):
        tg = make_notifier()
        tg._handle_command("⏸ 일시정지")
        self.assertTrue(tg.trading_paused)

    def test_pause_plain_text_sets_paused(self):
        tg = make_notifier()
        tg._handle_command("일시정지")
        self.assertTrue(tg.trading_paused)

    def test_resume_button_text_clears_paused(self):
        tg = make_notifier()
        tg.trading_paused = True
        tg._handle_command("▶️ 재개")
        self.assertFalse(tg.trading_paused)

    def test_resume_also_clears_awaiting_confirmation(self):
        """일시정지 상태에서 재개하면, 별개로 걸려있던 일일체크포인트 확인대기 상태도
        같이 풀어줘야 다시 매매가 막히지 않는다."""
        tg = make_notifier()
        tg.trading_paused = True
        tg._awaiting_confirmation = True
        tg._handle_command("재개")
        self.assertFalse(tg.trading_paused)
        self.assertFalse(tg._awaiting_confirmation)

    def test_pause_when_already_paused_is_idempotent(self):
        tg = make_notifier()
        tg.trading_paused = True
        tg._handle_command("일시정지")
        self.assertTrue(tg.trading_paused)

    def test_resume_when_not_paused_is_idempotent(self):
        tg = make_notifier()
        tg._handle_command("재개")
        self.assertFalse(tg.trading_paused)

    def test_menu_keyboard_includes_pause_resume_row(self):
        """send_menu()가 만드는 고정 키보드에 일시정지/재개 버튼이 상시 포함되는지 확인.
        enabled=False라 실제 전송은 안 되지만, send()에 넘기는 reply_markup 구성 자체를
        검증하기 위해 send()를 가로챈다."""
        tg = make_notifier()
        captured = {}
        original_send = tg.send

        def fake_send(text, reply_markup=None):
            captured["reply_markup"] = reply_markup
            return original_send(text, reply_markup)

        tg.send = fake_send
        tg.send_menu()
        rows = captured["reply_markup"]["keyboard"]
        texts = {btn["text"] for row in rows for btn in row}
        self.assertIn("⏸ 일시정지", texts)
        self.assertIn("▶️ 재개", texts)


if __name__ == "__main__":
    unittest.main()
