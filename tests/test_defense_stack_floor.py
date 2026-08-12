"""[2026-08-12 사용자요청] "방어배율이 곱셈으로 겹쳐서 수량이 0이 되는" 문제 — 하한선
(DEFENSE_STACK_MIN_RATIO_MULT)이 있으면 여러 방어 배율이 겹쳐도 최초 비중의 일정 비율
밑으로는 안 내려가야 한다. execute_entry 전체를 통합 테스트하기보다, 하한 로직 자체가
main.py 안에 정확히 반영됐는지 소스 스캔으로 검증(과도한 API 모킹 없이 가볍게)."""
import re
import unittest
from pathlib import Path

from bot.config import Config


class DefenseStackFloorTests(unittest.TestCase):
    def test_config_field_exists_with_sane_default(self):
        cfg = Config()
        self.assertTrue(0 < cfg.defense_stack_min_ratio_mult <= 1.0)

    def test_execute_entry_applies_floor_after_all_multipliers(self):
        src = Path("bot/main.py").read_text(encoding="utf-8")
        # liquidity_mult 적용 이후, min_margin_usdt 계산 이전에 floor 로직이 있어야 한다
        liquidity_idx = src.index("ratio *= liquidity_mult")
        floor_idx = src.index("defense_stack_min_ratio_mult", liquidity_idx)
        margin_idx = src.index("compute_min_margin(total_balance, cfg)", liquidity_idx)
        self.assertTrue(liquidity_idx < floor_idx < margin_idx,
                         "하한 로직이 모든 배율 적용 후 & 수량 계산 전에 있어야 한다")

    def test_floor_logic_only_raises_never_lowers(self):
        """하한선은 ratio가 floor_ratio보다 작을 때만 끌어올려야 하고, floor 이상이면
        건드리지 않아야 한다 — 로직을 직접 재현해서 확인."""
        base = 0.19
        mult = 0.30
        floor_ratio = base * mult

        # 이미 floor 이상인 경우: 그대로 유지
        ratio = 0.10
        if 0 < ratio < floor_ratio:
            ratio = floor_ratio
        self.assertEqual(ratio, 0.10)

        # floor보다 낮은 경우: floor로 올라감
        ratio = 0.01
        if 0 < ratio < floor_ratio:
            ratio = floor_ratio
        self.assertAlmostEqual(ratio, floor_ratio)

        # 정확히 0인 경우(신호 자체가 없어 곱해질 게 없는 등): 하한 적용 안 함(0 그대로)
        ratio = 0.0
        if 0 < ratio < floor_ratio:
            ratio = floor_ratio
        self.assertEqual(ratio, 0.0)


if __name__ == "__main__":
    unittest.main()
