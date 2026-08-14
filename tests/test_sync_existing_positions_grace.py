"""[2026-08-14 실측 사고] sync_existing_positions()가 재시작 시 entered_at을 '지금'으로
새로 채우고 stop_loss_widened를 항상 False로 초기화해서, 진입 유예기간(180초) 동안
넓혀둔 손절폭(~20% ROE)이 재시작 이후 영원히 원래 폭(8%)으로 안 좁혀지던 버그(APRUSDT
실사고, -20.1% ROE 손절) 재발방지 검증. 실 API 호출 없음(FakeExchange만 사용)."""
import functools
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import sync_existing_positions
from bot.position_manager import PositionManager
from bot.trade_ledger import find_open_bot_position_opened_at, infer_open_position_origin, \
    load_open_bot_positions, mark_bot_position_open


class FakeExchange:
    def __init__(self, positions):
        self._positions = positions

    def get_open_positions(self):
        return self._positions


class SyncExistingPositionsGraceTests(unittest.TestCase):
    def test_bot_origin_position_restores_original_entered_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 100.0, 5.0, 4.0, path=path)
            original_opened_at = load_open_bot_positions(path)["ABCUSDT"]["opened_at"]

            cfg = Config()
            pm = PositionManager(cfg)
            ex = FakeExchange([{"symbol": "ABCUSDT", "amount": 5.0, "entry_price": 100.0,
                                 "side": "LONG", "leverage": 4.0}])

            with patch("bot.main.find_open_bot_position_opened_at",
                       new=functools.partial(find_open_bot_position_opened_at, path=path)), \
                 patch("bot.main.infer_open_position_origin",
                       new=functools.partial(infer_open_position_origin, path=path)):
                sync_existing_positions(ex, pm)

            pos = pm.positions["ABCUSDT"]
            self.assertEqual(pos.entered_at, original_opened_at)
            self.assertTrue(pos.stop_loss_widened,
                             "복원 직후엔 넓은 상태로 표시해 다음 폴링에서 유예기간 경과 여부를 재판정하게 해야 한다")

    def test_manual_position_gets_fresh_entered_at_and_not_widened(self):
        """origin=manual(로컬 기록 없음)이면 기존과 동일하게 안전한 기본값(지금/좁음) 유지."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"  # 존재하지 않는 빈 기록 파일

            cfg = Config()
            pm = PositionManager(cfg)
            ex = FakeExchange([{"symbol": "MANUALUSDT", "amount": 2.0, "entry_price": 50.0,
                                 "side": "LONG", "leverage": 3.0}])

            with patch("bot.main.find_open_bot_position_opened_at",
                       new=functools.partial(find_open_bot_position_opened_at, path=path)), \
                 patch("bot.main.infer_open_position_origin",
                       new=functools.partial(infer_open_position_origin, path=path)):
                sync_existing_positions(ex, pm)

            pos = pm.positions["MANUALUSDT"]
            self.assertEqual(pos.origin, "manual")
            self.assertFalse(pos.stop_loss_widened)


if __name__ == "__main__":
    unittest.main()
