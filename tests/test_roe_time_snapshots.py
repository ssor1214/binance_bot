"""[2026-08-18] 진입 후 ROE 시점 스냅샷 — 진입 품질 규명을 위한 관측 전용 필드.

배경: 관측 190건에서 "고점 ROE가 1.5%에 못 미친 거래" 65건(34.2%)이 승률 9.2%,
순익 -10.173으로 손실 전부를 만든다. 나머지 125건은 +7.291이라 이 34%만 없으면 흑자다
(완벽히 걸러낼 경우 -2.882 -> +7.291).

그런데 진입 시점 피처로는 구분이 안 된다. 확률/우선순위/강도/speed/total_score/mtf_agree/
btc_mult 등 9개를 대조한 결과 8개가 z<2로 무의미했다(확률은 불량 0.9420 vs 정상 0.9409로
오히려 불량이 미세하게 높다). 유일하게 유의한 명목크기(z=-2.13)는 잔고가 컸던 시기 효과다.

그래서 **진입 후 짧은 구간**에서 판별 가능한지를 측정한다. 기존 관측 필드는 전 구간
최고/최저(max_favorable_roe / max_adverse_roe)만 있어 시간 궤적이 없었다.

이 테스트가 지키는 것:
1. 스냅샷이 청산 판단을 바꾸지 않는다 (측정 먼저, 규칙은 그다음)
2. 30초/60초 경과 후 첫 폴링에서 한 번만 기록되고 이후 덮어쓰이지 않는다
3. 그 전에는 None으로 남아 "아직 안 지남"과 "ROE 0"이 구분된다
4. 평단가가 바뀌면 기준점과 함께 초기화된다

주의: 2026-08-17에 "무조건 120/180초 후 컷"을 검증했다가 승률 -11~12%p로 기각한 이력이 있다.
그때는 측정 없이 잘랐고, 이번엔 판별 가능성부터 잰다. 이 필드로 청산 규칙을 만들려면
탐지율/오탐률을 실측한 뒤 별도 검증을 거칠 것.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager, TrackedPosition


def _pm(entered_at, entry=100.0, side="LONG"):
    pm = PositionManager(Config())
    pm.positions["TESTUSDT"] = TrackedPosition(
        symbol="TESTUSDT", side=side, entry_price=entry, quantity=1.0,
        leverage=4.0, entered_at=entered_at,
    )
    return pm


class SnapshotTimingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p = patch("bot.position_manager.STATS_FILE", Path(self._tmp.name) / "s.json")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_none_before_threshold(self):
        """30초 전에는 None — 'ROE 0'과 구분돼야 한다."""
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 10):
            pm.evaluate("TESTUSDT", 100.0)
        pos = pm.positions["TESTUSDT"]
        self.assertIsNone(pos.roe_at_30s)
        self.assertIsNone(pos.roe_at_60s)

    def test_recorded_after_30s(self):
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 31):
            pm.evaluate("TESTUSDT", 100.0 * (1 + 0.02 / 4))  # ROE +2%
        pos = pm.positions["TESTUSDT"]
        self.assertAlmostEqual(pos.roe_at_30s, 2.0, places=3)
        self.assertIsNone(pos.roe_at_60s, "60초는 아직 안 지났다")

    def test_not_overwritten_later(self):
        """폴링이 계속 돌아도 첫 값이 유지돼야 한다(시점 스냅샷의 의미)."""
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 31):
            pm.evaluate("TESTUSDT", 100.0 * (1 + 0.02 / 4))
        first = pm.positions["TESTUSDT"].roe_at_30s
        with patch("bot.position_manager.time.time", return_value=now + 45):
            pm.evaluate("TESTUSDT", 100.0 * (1 + 0.09 / 4))
        self.assertEqual(pm.positions["TESTUSDT"].roe_at_30s, first)

    def test_both_recorded_after_60s(self):
        """첫 폴링이 60초 뒤라면 두 값이 같은 관측치로 채워진다(폴링 주기 약 5초라 정상)."""
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 61):
            pm.evaluate("TESTUSDT", 100.0 * (1 - 0.03 / 4))  # ROE -3%
        pos = pm.positions["TESTUSDT"]
        self.assertAlmostEqual(pos.roe_at_30s, -3.0, places=3)
        self.assertAlmostEqual(pos.roe_at_60s, -3.0, places=3)

    def test_short_side_sign(self):
        now = time.time()
        pm = _pm(now, side="SHORT")
        with patch("bot.position_manager.time.time", return_value=now + 31):
            pm.evaluate("TESTUSDT", 100.0 * (1 - 0.02 / 4))  # 숏은 하락이 유리
        self.assertAlmostEqual(pm.positions["TESTUSDT"].roe_at_30s, 2.0, places=3)


class NoBehaviourChangeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p = patch("bot.position_manager.STATS_FILE", Path(self._tmp.name) / "s.json")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_verdict_unchanged(self):
        """스냅샷 기록이 청산 판단을 바꾸지 않는다."""
        now = time.time()
        for price in (80.0, 99.0, 100.0, 103.0, 120.0):
            with self.subTest(price=price):
                expected = _pm(now)._evaluate_inner("TESTUSDT", price)
                pm = _pm(now)
                with patch("bot.position_manager.time.time", return_value=now + 61):
                    self.assertEqual(pm.evaluate("TESTUSDT", price), expected)

    def test_reset_on_average_down(self):
        now = time.time()
        pm = _pm(now)
        with patch("bot.position_manager.time.time", return_value=now + 61):
            pm.evaluate("TESTUSDT", 101.0)
        pm.apply_average_down("TESTUSDT", new_entry_price=90.0, new_quantity=2.0, added_margin_usdt=1.0)
        pos = pm.positions["TESTUSDT"]
        self.assertIsNone(pos.roe_at_30s)
        self.assertIsNone(pos.roe_at_60s)


class LedgerFieldTests(unittest.TestCase):
    def test_trade_record_defaults_none(self):
        from bot.trade_ledger import TradeRecord
        rec = TradeRecord(symbol="X", side="LONG", origin="bot", entry_reason="P",
                          exit_reason="TAKE_PROFIT", entry_price=1.0, exit_price=1.1,
                          quantity=1.0, leverage=4.0, entered_at=0.0, exited_at=1.0,
                          held_seconds=1.0, estimated_pnl_pct=1.0, estimated_pnl_usdt=0.1,
                          bot_version="t", config_snapshot={})
        self.assertIsNone(rec.roe_at_30s)
        self.assertIsNone(rec.roe_at_60s)

    def test_main_passes_snapshots_to_ledger(self):
        import bot.main as main
        from bot.position_manager import PositionManager
        pm = PositionManager(Config())
        pm.positions["TESTUSDT"] = TrackedPosition(
            symbol="TESTUSDT", side="LONG", entry_price=100.0, quantity=1.0, leverage=4.0)
        pos = pm.positions["TESTUSDT"]
        pos.roe_at_30s, pos.roe_at_60s = -1.25, -2.5
        captured = {}
        with patch.object(main, "append_trade_record", lambda r: captured.setdefault("r", r)), \
             patch.object(main, "mark_position_closed", lambda s: None):
            main.record_trade_ledger(Config(), pos, "TESTUSDT", "STOP_LOSS", 99.0, -1.0, -0.1)
        self.assertEqual(captured["r"].roe_at_30s, -1.25)
        self.assertEqual(captured["r"].roe_at_60s, -2.5)


if __name__ == "__main__":
    unittest.main()
