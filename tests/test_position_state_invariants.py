"""[2026-08-11 취침중 검토] VirtualEntrySession 상태머신 리뷰에서 나온 "상태 전이는 허용된
경로로만" 원칙을 TrackedPosition의 armed/peak_pnl에 적용할 가치가 있는지 검토한 결과.

결론: 이미 각 변경 지점(evaluate()/apply_average_down())이 개별적으로 올바르게 가드하고
있고(예: `if roe > pos.peak_pnl:`로 단조증가 보장), 변경 지점 수 자체가 적어서(armed 6곳,
peak_pnl 3곳) 별도의 상태머신 레이어를 새로 만드는 건 이 시점엔 과할 수 있다고 판단했다.
대신 그 불변식(invariant)을 회귀테스트로 codify해서, 나중에 누군가 실수로 이 불변식을
깨는 코드를 넣으면(예: peak_pnl을 조건 없이 덮어쓰기) 테스트가 바로 잡아내도록 한다 —
런타임 코드 변경 없이 안전망만 추가(자본 리스크 없음)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg() -> Config:
    c = Config()
    c.take_profit_min = 3.0
    c.short_take_profit_min = 3.0
    c.trail_drawdown_pct = 1.0
    c.stop_loss_pct = 6.0
    c.take_profit_hard_cap = 20.0
    c.small_profit_lock_balance_threshold = 0  # 이 테스트에서 소액계좌 조기잠금 분기 배제
    c.small_profit_balance_threshold = 0
    c.average_down_enabled = True
    c.average_down_max_total_margin_ratio = 0.5
    return c


class PositionStateInvariantTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stats_path = Path(self.tmp.name) / ".bot_stats.json"
        patcher = patch("bot.position_manager.STATS_FILE", stats_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return PositionManager(cfg())

    def test_peak_pnl_never_decreases_while_armed(self):
        """가격이 오르내려도(익절선 위에서) peak_pnl은 한 번 올라간 뒤 절대 내려가면 안 된다
        — 트레일링 확정 판단(peak_pnl - roe)의 정확성이 이 불변식에 의존한다."""
        pm = self.make_manager()
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["BTCUSDT"]

        # ROE 4% 도달 -> armed, peak_pnl=4.0
        pm.evaluate("BTCUSDT", mark_price=101.0)  # (101-100)/100*4 = 4%
        self.assertTrue(pos.armed)
        self.assertAlmostEqual(pos.peak_pnl, 4.0)

        # 가격이 더 올라 ROE 6% -> peak_pnl 갱신
        pm.evaluate("BTCUSDT", mark_price=101.5)  # 1.5%*4=6%
        self.assertAlmostEqual(pos.peak_pnl, 6.0)

        # 가격이 살짝 밀려 ROE 5.5% (트레일링 폭 1.0% 미만 하락이라 아직 확정 안 됨)
        decision = pm.evaluate("BTCUSDT", mark_price=101.375)  # 1.375%*4=5.5%
        self.assertIsNone(decision)
        # 핵심 불변식: peak_pnl은 6.0에서 절대 내려가면 안 된다(현재 roe 5.5%로 되돌아가면 안 됨)
        self.assertAlmostEqual(pos.peak_pnl, 6.0)

    def test_average_down_resets_armed_and_peak_pnl(self):
        """물타기로 평단가가 바뀌면 이전 peak_pnl은 더 이상 유효하지 않다 — armed=False,
        peak_pnl=0.0으로 반드시 리셋돼야 한다(그렇지 않으면 새 평단가 기준 ROE인데
        옛 peak_pnl과 비교해서 엉뚱하게 즉시 트레일링 확정될 위험이 있다)."""
        pm = self.make_manager()
        pm.track("ETHUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["ETHUSDT"]
        pm.evaluate("ETHUSDT", mark_price=101.0)
        self.assertTrue(pos.armed)
        self.assertGreater(pos.peak_pnl, 0.0)

        pm.apply_average_down("ETHUSDT", new_entry_price=95.0, new_quantity=2.0, added_margin_usdt=10.0)

        self.assertFalse(pos.armed)
        self.assertEqual(pos.peak_pnl, 0.0)
        self.assertEqual(pos.average_down_count, 1)

    def test_armed_stays_true_across_cycles_without_average_down(self):
        """물타기가 없는 한(지금 라이브 기본값), 한 번 armed된 포지션은 다음 사이클에서도
        계속 armed 상태를 유지해야 한다 — 매 evaluate() 호출마다 실수로 초기화되면 안 됨."""
        pm = self.make_manager()
        pm.track("SOLUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["SOLUSDT"]
        pm.evaluate("SOLUSDT", mark_price=101.0)
        self.assertTrue(pos.armed)
        for _ in range(5):
            pm.evaluate("SOLUSDT", mark_price=101.0)
            self.assertTrue(pos.armed)  # 반복 호출해도 계속 armed 유지


if __name__ == "__main__":
    unittest.main()
