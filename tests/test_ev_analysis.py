"""[2026-08-09] ev_analysis 단위테스트. 전부 합성(가짜) 거래 기록만 사용하고 실제 원장
파일이나 API는 건드리지 않는다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ev_analysis import analyze_by_side, analyze_by_symbol, negative_ev_segments


def rec(symbol="AUSDT", side="LONG", pnl_pct=1.0, pnl_usdt=0.5, entered_at=0.0):
    return {
        "symbol": symbol, "side": side, "estimated_pnl_pct": pnl_pct,
        "estimated_pnl_usdt": pnl_usdt, "entered_at": entered_at,
    }


class AnalyzeBySymbolTests(unittest.TestCase):
    def test_positive_expectancy_symbol(self):
        records = [rec(symbol="AUSDT", pnl_pct=5.0, pnl_usdt=2.0) for _ in range(10)]
        stats = analyze_by_symbol(records, min_sample=5)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0].key, "AUSDT")
        self.assertEqual(stats[0].win_rate, 1.0)
        self.assertGreater(stats[0].expectancy_pct, 0)
        self.assertTrue(stats[0].sufficient_sample)

    def test_negative_expectancy_symbol_all_losses(self):
        records = [rec(symbol="BUSDT", pnl_pct=-3.0, pnl_usdt=-1.0) for _ in range(10)]
        stats = analyze_by_symbol(records, min_sample=5)
        self.assertLess(stats[0].expectancy_pct, 0)

    def test_insufficient_sample_flagged(self):
        records = [rec(symbol="CUSDT", pnl_pct=5.0, pnl_usdt=2.0) for _ in range(3)]
        stats = analyze_by_symbol(records, min_sample=10)
        self.assertFalse(stats[0].sufficient_sample)

    def test_sorted_ascending_by_expectancy(self):
        records = (
            [rec(symbol="GOOD", pnl_pct=5.0, pnl_usdt=2.0) for _ in range(10)]
            + [rec(symbol="BAD", pnl_pct=-5.0, pnl_usdt=-2.0) for _ in range(10)]
        )
        stats = analyze_by_symbol(records, min_sample=5)
        self.assertEqual([s.key for s in stats], ["BAD", "GOOD"])

    def test_profit_factor_none_when_no_losses(self):
        records = [rec(symbol="AUSDT", pnl_pct=5.0, pnl_usdt=2.0) for _ in range(5)]
        stats = analyze_by_symbol(records, min_sample=5)
        self.assertIsNone(stats[0].profit_factor)

    def test_max_consecutive_losses_counted_correctly(self):
        records = [
            rec(pnl_usdt=1.0), rec(pnl_usdt=-1.0), rec(pnl_usdt=-1.0), rec(pnl_usdt=-1.0), rec(pnl_usdt=1.0),
        ]
        stats = analyze_by_symbol(records, min_sample=1)
        self.assertEqual(stats[0].max_consecutive_losses, 3)


class AnalyzeBySideTests(unittest.TestCase):
    def test_separates_long_and_short(self):
        records = (
            [rec(side="LONG", pnl_pct=5.0, pnl_usdt=2.0) for _ in range(10)]
            + [rec(side="SHORT", pnl_pct=-5.0, pnl_usdt=-2.0) for _ in range(10)]
        )
        stats = {s.key: s for s in analyze_by_side(records, min_sample=5)}
        self.assertGreater(stats["LONG"].expectancy_pct, 0)
        self.assertLess(stats["SHORT"].expectancy_pct, 0)


class NegativeEvSegmentsTests(unittest.TestCase):
    def test_excludes_insufficient_sample_even_if_negative(self):
        records = [rec(symbol="THIN", pnl_pct=-10.0, pnl_usdt=-5.0) for _ in range(2)]
        stats = analyze_by_symbol(records, min_sample=10)
        self.assertEqual(negative_ev_segments(stats), [])  # 표본 부족이라 제외돼야 함

    def test_includes_sufficient_negative_sample(self):
        records = [rec(symbol="BAD", pnl_pct=-10.0, pnl_usdt=-5.0) for _ in range(10)]
        stats = analyze_by_symbol(records, min_sample=5)
        flagged = negative_ev_segments(stats)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].key, "BAD")


if __name__ == "__main__":
    unittest.main()
