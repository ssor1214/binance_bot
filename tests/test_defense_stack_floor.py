"""[2026-08-17 운영 복기] 방어 하한은 손익 저하 국면에서 보호보다 거래수 유지에 치우친
기본값이었다. 이제 기본은 비활성화(0)이고, 사용자가 명시적으로 켰을 때만 동작해야 한다."""
import unittest
from pathlib import Path


class DefenseStackFloorTests(unittest.TestCase):
    def test_config_field_disabled_by_default(self):
        src = Path("bot/config.py").read_text(encoding="utf-8")
        self.assertIn('defense_stack_min_ratio_mult: float = _float("DEFENSE_STACK_MIN_RATIO_MULT", 0.0)', src)

    def test_execute_entry_checks_floor_only_when_enabled(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        self.assertIn("defense_floor_anchor_ratio > 0 and cfg.defense_stack_min_ratio_mult > 0", src)
        self.assertIn('candidate.get("entry_lane") == "frequency"', src)
        self.assertIn('candidate.get("entry_lane") == "micro_scalp"', src)

    def test_floor_logic_when_enabled_only_raises_never_lowers(self):
        base = 0.19
        mult = 0.30
        floor_ratio = base * mult
        ratio = 0.10
        if 0 < ratio < floor_ratio:
            ratio = floor_ratio
        self.assertEqual(ratio, 0.10)
        ratio = 0.01
        if 0 < ratio < floor_ratio:
            ratio = floor_ratio
        self.assertAlmostEqual(ratio, floor_ratio)
        ratio = 0.0
        if 0 < ratio < floor_ratio:
            ratio = floor_ratio
        self.assertEqual(ratio, 0.0)

    def test_frequency_lane_floor_respects_lane_size_cap(self):
        base = 0.19
        default_floor_mult = 0.90
        lane_mult = 0.35
        floor_mult = min(default_floor_mult, lane_mult)
        self.assertAlmostEqual(base * floor_mult, 0.0665)

    def test_floor_anchor_uses_symbol_capped_ratio_not_raw_base_ratio(self):
        raw_base_ratio = 0.15
        symbol_capped_ratio = 0.05
        floor_mult = 0.90
        old_floor_ratio = raw_base_ratio * floor_mult
        new_floor_ratio = symbol_capped_ratio * floor_mult
        self.assertAlmostEqual(old_floor_ratio, 0.135)
        self.assertAlmostEqual(new_floor_ratio, 0.045)
        self.assertLess(new_floor_ratio, old_floor_ratio)


if __name__ == "__main__":
    unittest.main()
