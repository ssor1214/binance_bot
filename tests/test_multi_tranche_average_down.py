"""[2026-08-10] 다회차 물타기 + 총증거금 하드캡 단위테스트. 사용자 요청: "물타기를 여러 번
할 수 있게 하되, 포지션 전체 마진은 항상 6% 내외로만" — 횟수 제한은 없애고 총액(초기+모든
추가분)에 하드캡을 건 새 설계를 검증한다. 실 API를 절대 호출하지 않는다."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager


def cfg(**overrides):
    c = Config()
    c.average_down_enabled = True
    c.stop_loss_pct = 6.0
    c.average_down_trigger_ratio = 0.5
    c.average_down_size_ratio = 0.045
    c.average_down_max_total_margin_ratio = 0.06
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def make_manager(config=None):
    tmp = tempfile.TemporaryDirectory()
    stats_path = Path(tmp.name) / ".bot_stats.json"
    patcher = patch("bot.position_manager.STATS_FILE", stats_path)
    patcher.start()
    pm = PositionManager(config or cfg())
    return pm, tmp, patcher


class TotalMarginCapTests(unittest.TestCase):
    def test_track_with_balance_at_entry_sets_initial_margin_and_cap(self):
        pm, tmp, patcher = make_manager()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        # 진입가 100, 수량 4, 레버리지 4 -> notional=400, margin=100
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=4.0, leverage=4.0, balance_at_entry=1000.0)
        pos = pm.positions["BTCUSDT"]
        self.assertAlmostEqual(pos.initial_margin_usdt, 100.0)
        self.assertAlmostEqual(pos.max_total_margin_usdt, 1000.0 * 0.06)  # 60.0

    def test_should_average_down_lazily_initializes_cap_when_missing(self):
        """[2026-08-10] reconcile로 뒤늦게 발견된 포지션처럼 balance_at_entry 없이 track된
        경우에도, should_average_down이 처음 불릴 때 반드시 상한을 채워넣어야 한다 —
        "정보가 없으면 무제한"이 되는 사각지대를 만들면 안 된다."""
        pm, tmp, patcher = make_manager()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=4.0, leverage=4.0)  # balance_at_entry 없음
        pos = pm.positions["BTCUSDT"]
        self.assertEqual(pos.max_total_margin_usdt, 0.0)  # 아직 안 채워짐

        pm.should_average_down("BTCUSDT", mark_price=100.0, balance=500.0)

        self.assertGreater(pos.max_total_margin_usdt, 0.0)
        self.assertAlmostEqual(pos.max_total_margin_usdt, 500.0 * 0.06)
        self.assertAlmostEqual(pos.initial_margin_usdt, 100.0)  # entry_price*qty/leverage = 100*4/4

    def test_blocks_when_total_margin_already_at_cap(self):
        pm, tmp, patcher = make_manager()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=4.0, leverage=4.0, balance_at_entry=1000.0)
        pos = pm.positions["BTCUSDT"]
        pos.total_margin_added_usdt = pos.max_total_margin_usdt - pos.initial_margin_usdt  # 정확히 상한 도달

        # 가격이 트리거 지점까지 밀려도(예: -3% ROE) 상한 도달했으므로 False여야 함
        result = pm.should_average_down("BTCUSDT", mark_price=99.25, balance=1000.0)  # LONG -0.75%*4=-3% ROE
        self.assertFalse(result)

    def test_allows_multiple_tranches_as_long_as_under_cap(self):
        """[2026-08-10 핵심 테스트] 여러 번 나눠서 물타기가 실제로 가능해야 한다(횟수 제한
        없음) — 단, 매번 총액이 상한 밑인지 확인한다."""
        pm, tmp, patcher = make_manager()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=4.0, leverage=4.0, balance_at_entry=1000.0)
        # 상한 60, 초기 100... 초기 마진이 이미 상한보다 큰 극단 케이스를 피하려 재설정
        pos = pm.positions["BTCUSDT"]
        pos.initial_margin_usdt = 10.0
        pos.max_total_margin_usdt = 60.0

        # 1차 트랜치: -3% ROE 지점(trigger_ratio*1=0.5 -> -3%)
        self.assertTrue(pm.should_average_down("BTCUSDT", mark_price=99.25, balance=1000.0))
        pm.apply_average_down("BTCUSDT", new_entry_price=99.5, new_quantity=8.0, added_margin_usdt=20.0)
        self.assertEqual(pos.average_down_count, 1)
        self.assertAlmostEqual(pos.total_margin_added_usdt, 20.0)

        # 2차 트랜치: 더 깊은 지점(trigger_ratio*2=1.0 -> min(0.95,1.0)*6%=-5.7%)에서 가능해야 함.
        # 부동소수점 경계(정확히 -5.7%)에 걸리지 않도록 여유를 두고 -5.8% ROE로 계산한다.
        deep_price = 99.5 * (1 - 0.058 / 4)
        self.assertTrue(pm.should_average_down("BTCUSDT", mark_price=deep_price, balance=1000.0))
        pm.apply_average_down("BTCUSDT", new_entry_price=deep_price, new_quantity=12.0, added_margin_usdt=25.0)
        self.assertEqual(pos.average_down_count, 2)
        self.assertAlmostEqual(pos.total_margin_added_usdt, 45.0)  # 20+25=45, 초기10 합쳐 55 < 상한60

        # 이제 남은 여유(60-10-45=5)가 거의 없으므로 다음 요청은 room이 작아 제한돼야 함
        remaining_room = pos.max_total_margin_usdt - (pos.initial_margin_usdt + pos.total_margin_added_usdt)
        self.assertAlmostEqual(remaining_room, 5.0)

    def test_trigger_depth_increases_with_tranche_count(self):
        """[2026-08-10] N번째 추가는 더 깊은 지점에서만 발동해야 한다(같은 지점에서 계속
        반복 발동하면 순식간에 상한을 다 써버림)."""
        pm, tmp, patcher = make_manager()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=4.0, leverage=4.0, balance_at_entry=100000.0)
        pos = pm.positions["BTCUSDT"]
        pos.average_down_count = 1  # 이미 1차 트랜치를 마쳤다고 가정

        # 1차 트리거 지점(-3% ROE)에서는 2차가 아직 발동하면 안 됨(2차는 -6%*min(0.95,1.0)=-5.7% 필요)
        result_at_first_trigger = pm.should_average_down("BTCUSDT", mark_price=99.25, balance=100000.0)
        self.assertFalse(result_at_first_trigger)


class BackwardCompatDisabledTests(unittest.TestCase):
    def test_disabled_by_default_returns_false_regardless_of_price(self):
        pm, tmp, patcher = make_manager(cfg(average_down_enabled=False))
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=4.0, leverage=4.0, balance_at_entry=1000.0)
        self.assertFalse(pm.should_average_down("BTCUSDT", mark_price=90.0, balance=1000.0))


if __name__ == "__main__":
    unittest.main()
