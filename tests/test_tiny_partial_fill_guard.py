"""극소 부분체결 가드(helper + 배선) 검증."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import _is_effectively_dust_entry, _minimum_entry_margin_usdt


class MinimumEntryMarginTests(unittest.TestCase):
    def test_uses_existing_position_floor_when_open_positions_exist(self):
        cfg = Config()
        cfg.small_balance_min_margin_usdt = 4.0
        cfg.small_balance_existing_positions_min_margin_usdt = 3.0
        cfg.low_balance_recovery_min_margin_usdt = 3.0
        cfg.tiny_balance_tier2_threshold = 8.0
        cfg.tiny_balance_tier2_min_margin_usdt = 4.0
        margin = _minimum_entry_margin_usdt(
            9.34, cfg,
            has_open_positions=True,
            low_balance_recovery_floor_enabled=True,
        )
        self.assertEqual(margin, 3.0)

    def test_uses_recent_defense_floor_when_recent_defense_is_active(self):
        cfg = Config()
        cfg.small_balance_min_margin_usdt = 4.0
        cfg.recent_defense_min_margin_usdt = 1.4
        cfg.tiny_balance_tier2_threshold = 8.0
        cfg.tiny_balance_tier2_min_margin_usdt = 4.0
        margin = _minimum_entry_margin_usdt(
            9.34, cfg,
            has_open_positions=False,
            low_balance_recovery_floor_enabled=False,
            recent_defense_active=True,
        )
        self.assertEqual(margin, 1.4)

    def test_dust_guard_rejects_tiny_partial_fill(self):
        self.assertTrue(_is_effectively_dust_entry(0.043, 3.0))

    def test_dust_guard_keeps_meaningful_partial_fill(self):
        self.assertFalse(_is_effectively_dust_entry(1.8, 3.0))


class ExecuteEntryDustGuardSourceTests(unittest.TestCase):
    def test_execute_entry_has_dust_cleanup_branch(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn("극소 부분체결 감지", src)
        # [2026-08-25] 먼지 임계를 진입 하한과 분리하면서 cfg를 넘기도록 시그니처가 바뀌었다.
        # (하한 10 USDT 환경에서 임계가 5.0이 되어 4 USDT 부분체결이 되팔리던 버그 수정)
        self.assertIn("_is_effectively_dust_entry(actual_margin_usdt, target_min_margin_usdt, cfg)", src)
        self.assertIn("ex.close_market_position(symbol, pos[\"side\"], abs(pos[\"amount\"]))", src)


if __name__ == "__main__":
    unittest.main()
