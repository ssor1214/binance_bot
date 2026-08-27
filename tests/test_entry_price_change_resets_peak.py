"""[2026-08-16 실거래 사고로 발견] 평단가가 바뀌면 peak_pnl 기준점도 초기화해야 한다.

실측 사고(CYSUSDT 2026-08-16 07:18, origin=manual):
  07:18:11  진입 0.8395 (5x)
  07:18:18  최소 익절선 도달, 트레일링 시작 (ROE 3.57%)
  07:18:47  사용자가 수동으로 포지션 조정 → 평단 0.8395 → 0.8730
  07:18:47  "트레일링 확정: 고점ROE=17.92% 현재ROE=-0.74% (18.66%p 하락)" → 즉시 청산

peak_pnl(17.92%)은 옛 평단 기준이고 현재 ROE(-0.74%)는 새 평단 기준이라, 둘을 비교하면
실제로는 없었던 "18.66%p 급락"이 만들어진다. 실제 손익은 -0.15%였을 뿐이고, 1~2분 뒤
가격이 0.8890(새 평단 기준 ROE +9.16%)까지 올라 홀딩이 이익이었다.

물타기 경로(PositionManager.apply_average_down)는 이미 peak_pnl/armed를 리셋하고 있었는데,
reconcile_positions의 "수량/평단가 변경 감지" 경로에만 그 처리가 빠져 있었다.
"""
import unittest

from bot.config import Config
from bot.position_manager import PositionManager


class ApplyAverageDownResetsPeakTests(unittest.TestCase):
    """비교 기준이 되는 기존 동작 — 물타기 경로는 원래 리셋하고 있었다."""

    def test_average_down_resets_peak_and_armed(self):
        pm = PositionManager(Config())
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=5.0)
        pos = pm.positions["BTCUSDT"]
        pos.peak_pnl = 17.92
        pos.armed = True
        pm.apply_average_down("BTCUSDT", new_entry_price=104.0, new_quantity=2.0)
        self.assertEqual(pos.peak_pnl, 0.0)
        self.assertFalse(pos.armed)
        self.assertEqual(pos.entry_price, 104.0)


class ReconcileEntryChangeSourceTests(unittest.TestCase):
    """reconcile_positions의 수동 변경 감지 경로에도 같은 처리가 배선됐는지 소스로 확인
    (전체 실거래 흐름 mock은 exchange 의존이 과함 — 이 저장소 기존 관례)."""

    def _src(self):
        import inspect
        from bot import main as bot_main
        return inspect.getsource(bot_main.reconcile_positions)

    def test_detects_entry_price_change_separately_from_quantity(self):
        """수량만 바뀐 경우(부분청산 등 평단 유지)는 기준점이 그대로이므로 리셋하면 안 된다 —
        괜히 리셋하면 익절 트레일링이 늦어진다."""
        src = self._src()
        self.assertIn('entry_changed = abs(pos.entry_price - live["entry_price"]) > 1e-12', src)
        self.assertIn("if entry_changed:", src)

    def test_resets_peak_and_armed_on_entry_change(self):
        src = self._src()
        self.assertIn("pos.peak_pnl = 0.0", src)
        self.assertIn("pos.armed = False", src)

    def test_logs_the_reset(self):
        """자는 동안 발생해도 사후에 추적할 수 있어야 한다."""
        src = self._src()
        self.assertIn("고점ROE/트레일링 기준점 초기화", src)


class StalePeakWouldMisfireTests(unittest.TestCase):
    """리셋이 없으면 실제로 오탐이 나는지 — 사고 수치를 그대로 재현해 확인한다."""

    def test_stale_peak_produces_phantom_drawdown(self):
        from bot.strategy import pnl_pct
        old_entry, new_entry, price, lev = 0.8394851063829787, 0.873, 0.8717, 5.0
        stale_peak_roe = pnl_pct(old_entry, 0.8774, "LONG") * lev  # 옛 평단 기준 고점
        current_roe = pnl_pct(new_entry, price, "LONG") * lev      # 새 평단 기준 현재
        phantom_drawdown = stale_peak_roe - current_roe
        # 실제 손익은 -1% 미만인데 기준점이 어긋나면 18%p대 급락으로 보인다
        self.assertGreater(phantom_drawdown, 15.0)
        self.assertGreater(current_roe, -1.0)

    def test_rebased_peak_removes_phantom_drawdown(self):
        """peak을 0으로 리셋하면 다음 평가부터 새 평단 기준으로 다시 쌓인다."""
        pm = PositionManager(Config())
        pm.track("CYSUSDT", "LONG", entry_price=0.8394851063829787, quantity=94.0, leverage=5.0)
        pos = pm.positions["CYSUSDT"]
        pos.peak_pnl = 17.92
        pos.armed = True
        # 사고 경로와 동일하게 평단만 갱신 + 리셋
        pos.entry_price = 0.873
        pos.peak_pnl = 0.0
        pos.armed = False
        self.assertEqual(pos.peak_pnl, 0.0)


if __name__ == "__main__":
    unittest.main()
