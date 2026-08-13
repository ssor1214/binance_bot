"""[2026-08-13 사용자요청] "복리로 잔고가 커지면 비중(%)은 그대로라 포지션당 달러 리스크가
계속 커진다" — 잔고 구간이 문턱을 초과하면 비중 상한을 단계적으로 낮추는
compute_large_balance_ratio_cap()(bot/main.py)과 PositionManager._large_balance_ratio_cap()/
next_position_size_ratio()가 정확히 같은 결과를 내는지 검증한다. "초과"만 해당(같으면 미포함),
스티키 없이 매번 그 시점 잔고로 실시간 판단. 실 API 호출 없음."""
import unittest

from bot.config import Config
from bot.main import compute_large_balance_ratio_cap
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.position_size_min = 0.19
    c.position_size_max = 0.19
    c.position_size_step = 0.0  # 연속승리로 인한 증가분 배제, 캡만 순수하게 검증
    c.small_balance_threshold = 0.0  # 이 테스트에서 저잔고 부스트 경로 배제
    c.large_balance_tier1_threshold = 300.0
    c.large_balance_tier1_max_ratio = 0.15
    c.large_balance_tier2_threshold = 500.0
    c.large_balance_tier2_max_ratio = 0.12
    c.large_balance_tier3_threshold = 1000.0
    c.large_balance_tier3_max_ratio = 0.10
    return c


class ComputeLargeBalanceRatioCapTests(unittest.TestCase):
    def test_below_tier1_returns_none(self):
        c = cfg()
        self.assertIsNone(compute_large_balance_ratio_cap(300.0, c))  # 같으면 미포함
        self.assertIsNone(compute_large_balance_ratio_cap(299.99, c))

    def test_tier1_applies_just_above_threshold(self):
        c = cfg()
        self.assertEqual(compute_large_balance_ratio_cap(300.01, c), 0.15)
        self.assertEqual(compute_large_balance_ratio_cap(500.0, c), 0.15)  # 500은 tier2 미포함

    def test_tier2_applies_just_above_threshold(self):
        c = cfg()
        self.assertEqual(compute_large_balance_ratio_cap(500.01, c), 0.12)
        self.assertEqual(compute_large_balance_ratio_cap(1000.0, c), 0.12)

    def test_tier3_applies_just_above_threshold(self):
        c = cfg()
        self.assertEqual(compute_large_balance_ratio_cap(1000.01, c), 0.10)
        self.assertEqual(compute_large_balance_ratio_cap(50000.0, c), 0.10)

    def test_drops_back_to_prior_tier_on_loss_no_stickiness(self):
        """500을 넘었다가 손실로 499가 되면 즉시 15%(tier1) 기준으로 돌아가야 한다."""
        c = cfg()
        self.assertEqual(compute_large_balance_ratio_cap(600.0, c), 0.12)
        self.assertEqual(compute_large_balance_ratio_cap(499.0, c), 0.15)


class PositionManagerLargeBalanceRatioTests(unittest.TestCase):
    def test_next_position_size_ratio_matches_main_function(self):
        c = cfg()
        pm = PositionManager(c)
        for balance in (250.0, 300.0, 300.01, 450.0, 500.01, 1000.01, 5000.0, 499.0):
            expected_cap = compute_large_balance_ratio_cap(balance, c)
            expected = min(c.position_size_min, expected_cap if expected_cap is not None else c.position_size_max)
            self.assertAlmostEqual(pm.next_position_size_ratio(balance), expected, places=6)

    def test_ratio_capped_at_10pct_above_1000(self):
        c = cfg()
        c.position_size_min = 0.19  # 기본 비중(19%)이 10% 캡보다 크더라도 캡을 넘으면 안 됨
        pm = PositionManager(c)
        ratio = pm.next_position_size_ratio(1500.0)
        self.assertEqual(ratio, 0.10)

    def test_no_cap_below_300(self):
        c = cfg()
        pm = PositionManager(c)
        ratio = pm.next_position_size_ratio(250.0)
        self.assertEqual(ratio, c.position_size_max)


if __name__ == "__main__":
    unittest.main()
