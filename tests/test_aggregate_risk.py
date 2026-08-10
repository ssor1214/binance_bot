"""[2026-08-09] 합산 증거금 기준 상관리스크 필터 단위테스트. 실제 PositionManager는
.bot_stats.json을 읽고/쓰므로, _save_stats/_load_stats를 무력화한 가짜 인스턴스를 쓴다
(오늘 밤 정한 원칙: 테스트에서 절대 실제 .bot_stats.json을 건드리지 않음)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.main import (
    aggregate_risk_size_multiplier,
    compute_aggregate_worst_case_loss_pct,
    passes_aggregate_risk_filter,
)
from bot.position_manager import PositionManager, TrackedPosition


class FakeCfg:
    max_aggregate_margin_pct = 60.0
    aggregate_risk_hard_pause = False
    aggregate_risk_size_mult = 0.65


def make_pm():
    with patch.object(PositionManager, "_load_stats", lambda self: None), \
         patch.object(PositionManager, "_save_stats", lambda self: None):
        pm = PositionManager(FakeCfg())
    return pm


class AggregateRiskTests(unittest.TestCase):
    def test_no_positions_means_zero_risk(self):
        pm = make_pm()
        self.assertEqual(compute_aggregate_worst_case_loss_pct(pm, 100.0), 0.0)

    def test_single_position_margin_percentage(self):
        pm = make_pm()
        # 진입가 10, 수량 40, 레버리지 4배 -> notional=400, margin=100
        pm.positions["AUSDT"] = TrackedPosition(symbol="AUSDT", side="LONG", entry_price=10.0, quantity=40.0, leverage=4.0)
        pct = compute_aggregate_worst_case_loss_pct(pm, total_balance=100.0)
        self.assertAlmostEqual(pct, 100.0)  # margin(100)/balance(100)*100 = 100%

    def test_multiple_positions_sum_correctly(self):
        pm = make_pm()
        pm.positions["AUSDT"] = TrackedPosition(symbol="AUSDT", side="LONG", entry_price=10.0, quantity=40.0, leverage=4.0)  # margin=100
        pm.positions["BUSDT"] = TrackedPosition(symbol="BUSDT", side="SHORT", entry_price=5.0, quantity=40.0, leverage=4.0)  # margin=50
        pct = compute_aggregate_worst_case_loss_pct(pm, total_balance=300.0)
        self.assertAlmostEqual(pct, (100.0 + 50.0) / 300.0 * 100)

    def test_filter_passes_when_under_threshold(self):
        pm = make_pm()
        pm.positions["AUSDT"] = TrackedPosition(symbol="AUSDT", side="LONG", entry_price=10.0, quantity=10.0, leverage=4.0)  # margin=25
        self.assertTrue(passes_aggregate_risk_filter(pm, FakeCfg(), total_balance=100.0))  # 25% < 60%

    def test_filter_blocks_when_over_threshold(self):
        pm = make_pm()
        pm.positions["AUSDT"] = TrackedPosition(symbol="AUSDT", side="LONG", entry_price=10.0, quantity=280.0, leverage=4.0)  # margin=700
        self.assertFalse(passes_aggregate_risk_filter(pm, FakeCfg(), total_balance=100.0))  # 700% > 60%

    def test_size_multiplier_reduces_instead_of_requiring_frequency_pause(self):
        pm = make_pm()
        pm.positions["AUSDT"] = TrackedPosition(symbol="AUSDT", side="LONG", entry_price=10.0, quantity=28.0, leverage=4.0)  # margin=70

        self.assertFalse(FakeCfg.aggregate_risk_hard_pause)
        self.assertAlmostEqual(aggregate_risk_size_multiplier(pm, FakeCfg(), total_balance=100.0), 0.65)

    def test_size_multiplier_is_neutral_under_threshold(self):
        pm = make_pm()
        pm.positions["AUSDT"] = TrackedPosition(symbol="AUSDT", side="LONG", entry_price=10.0, quantity=10.0, leverage=4.0)  # margin=25

        self.assertAlmostEqual(aggregate_risk_size_multiplier(pm, FakeCfg(), total_balance=100.0), 1.0)


if __name__ == "__main__":
    unittest.main()
