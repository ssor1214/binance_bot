"""[2026-08-15 사용자요청] "총 자산이 5usdt 미만일때 최소 진입 1.9usdt, 총 자산이 8usdt가
넘으면 최소 진입 4usdt, 총 자산이 10usdt가 넘으면 그때부터 최소 진입 7usdt로 세팅. 이후
자산이 50usdt 이상일땐 이전처럼 15%비율로 진입" — compute_min_margin()의 극소잔고 계단식
+ 50달러 졸업(graduation) 검증."""
import unittest

from bot.config import Config
from bot.main import compute_min_margin


class TinyBalanceMinMarginTierTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_below_5_uses_tier1(self):
        self.assertEqual(compute_min_margin(3.0, self.cfg), 2.5)
        self.assertEqual(compute_min_margin(4.99, self.cfg), 2.5)

    def test_5_to_8_still_tier1_no_threshold_defined_between(self):
        """사용자가 5~8 사이 문턱을 따로 안 줬으므로 계단식 그대로 tier1이 유지돼야 한다."""
        self.assertEqual(compute_min_margin(5.0, self.cfg), 2.5)
        self.assertEqual(compute_min_margin(7.99, self.cfg), 2.5)

    def test_over_8_uses_tier2(self):
        self.assertEqual(compute_min_margin(8.0, self.cfg), 4.0)
        self.assertEqual(compute_min_margin(9.99, self.cfg), 4.0)

    def test_10_to_below_graduation_uses_tier3(self):
        self.assertEqual(compute_min_margin(10.0, self.cfg), 7.0)
        self.assertEqual(compute_min_margin(49.99, self.cfg), 7.0)

    def test_50_and_above_graduates_off_tiny_balance_tiers(self):
        """[2026-08-15 사용자요청] 50달러부터는 극소잔고 계단식을 완전히 벗어나 옛 로직
        (aggressive_min_margin_usdt, aggressive_balance_threshold=100 미만이라 여전히
        그쪽)으로 돌아가야 한다 — tier3(7)보다 항상 작으므로 compute_position_size의
        max(비중기반, 최소증거금기반)에서 15% 비율 사이징이 자연스럽게 지배하게 된다."""
        self.assertEqual(compute_min_margin(50.0, self.cfg), self.cfg.aggressive_min_margin_usdt)
        self.assertEqual(compute_min_margin(80.0, self.cfg), self.cfg.aggressive_min_margin_usdt)

    def test_above_aggressive_threshold_falls_back_to_old_logic(self):
        self.assertEqual(compute_min_margin(150.0, self.cfg), self.cfg.min_margin_usdt)

    def test_large_balance_ladder_matches_user_request(self):
        self.assertEqual(compute_min_margin(100.0, self.cfg), 25.0)
        self.assertEqual(compute_min_margin(199.99, self.cfg), 25.0)
        self.assertEqual(compute_min_margin(200.0, self.cfg), 35.0)
        self.assertEqual(compute_min_margin(300.0, self.cfg), 40.0)
        self.assertEqual(compute_min_margin(500.0, self.cfg), 50.0)

    def test_graduation_floor_is_smaller_than_15pct_ratio_notional_at_50(self):
        """50달러 시점에 min_margin(졸업 후 값) * 레버리지가 balance*0.15*레버리지보다
        작아야(=비율 사이징이 이긴다) "이전처럼 15% 비율로 진입"이 실제로 보장된다."""
        floor = compute_min_margin(50.0, self.cfg)
        ratio_notional_factor = 50.0 * self.cfg.position_size_max  # 레버리지는 양쪽에 곱해지므로 상쇄
        # [2026-08-25 사용자요청으로 무효화] "1슬롯 최소 10 USDT" 요구에 따라 졸업구간 하한을
        # 10.0으로 올렸다. 그 결과 잔고 50에서는 하한(10) > 15% 비율 증거금(7.5)이 되어
        # 비율 사이징이 아니라 하한이 지배한다. 교차점은 10/0.15 ≈ 66.7 USDT — 그 위로는
        # 다시 비율 사이징이 이긴다. 이 테스트는 그 교차점을 명시적으로 고정한다.
        self.assertGreaterEqual(floor, ratio_notional_factor)
        self.assertAlmostEqual(floor / 0.15, 66.67, places=1)

    def test_thresholds_match_user_request(self):
        self.assertEqual(self.cfg.tiny_balance_tier1_threshold, 5.0)
        self.assertEqual(self.cfg.tiny_balance_tier1_min_margin_usdt, 2.5)
        self.assertEqual(self.cfg.tiny_balance_tier2_threshold, 8.0)
        self.assertEqual(self.cfg.tiny_balance_tier2_min_margin_usdt, 4.0)
        self.assertEqual(self.cfg.tiny_balance_tier3_threshold, 10.0)
        self.assertEqual(self.cfg.tiny_balance_tier3_min_margin_usdt, 7.0)
        self.assertEqual(self.cfg.tiny_balance_graduation_threshold, 50.0)


if __name__ == "__main__":
    unittest.main()
