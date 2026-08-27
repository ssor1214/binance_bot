"""[2026-08-17] 트레일링 무장(armed) 계측 필드.

배경: 실현손익 보정 후 436건을 재집계했더니 순익 전부가 "봇 트레일링이 무장한 69건"
(+8.99USDT, 승률 98.6%)에서 나왔고 무장 못 한 367건은 -16.76USDT였다. 그런데 무장 여부를
bot.log의 "트레일링 시작" 문자열로 역산할 수밖에 없어(원장에 필드가 없었음) 무장률 16%의
신뢰도가 낮았다. peak_pnl은 armed 이후에만 갱신되므로 "무장 전에 얼마나 올랐다가 닫혔는지"도
알 수 없었다.

이 테스트가 지키는 것:
1. 계측이 청산 판단을 바꾸지 않는다 (가장 중요 — 실거래 로직 무변경이 이 작업의 전제)
2. 무장 시각/ROE가 조기 return 경로에서도 빠짐없이 기록된다
3. 진입 시점부터의 최고/최저 ROE가 armed 여부와 무관하게 누적된다
4. 평단가가 바뀌면 관측 기준점도 초기화된다
"""
import time
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.position_manager import PositionManager, TrackedPosition


def _pm_with_position(side="LONG", entry=100.0, qty=1.0, leverage=4.0):
    pm = PositionManager(Config())
    pm.positions["TESTUSDT"] = TrackedPosition(
        symbol="TESTUSDT", side=side, entry_price=entry, quantity=qty, leverage=leverage,
    )
    return pm


class NoBehaviourChangeTests(unittest.TestCase):
    """계측 래퍼가 판단을 통과시키기만 하는지 — 결과가 _evaluate_inner와 항상 같아야 한다."""

    def test_wrapper_returns_inner_verdict_unchanged(self):
        for price in (80.0, 95.0, 99.0, 100.0, 101.0, 103.0, 120.0, 200.0):
            with self.subTest(price=price):
                pm = _pm_with_position()
                expected = pm._evaluate_inner("TESTUSDT", price)
                pm2 = _pm_with_position()
                self.assertEqual(pm2.evaluate("TESTUSDT", price), expected)

    def test_unknown_symbol_still_returns_none(self):
        pm = _pm_with_position()
        self.assertIsNone(pm.evaluate("NOPEUSDT", 100.0))

    def test_instrumentation_failure_does_not_block_exit(self):
        """계측 코드가 터져도 청산 판단은 그대로 나와야 한다(실거래 안전성)."""
        import bot.position_manager as pmod
        pm = _pm_with_position()
        expected = _pm_with_position()._evaluate_inner("TESTUSDT", 80.0)
        # pnl_pct를 계속 터뜨리면 내부 판단도 못 하므로 첫 호출(계측용)만 실패시킨다.
        real = pmod.pnl_pct
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("계측 실패")
            return real(*a, **kw)

        with patch("bot.position_manager.pnl_pct", side_effect=flaky):
            self.assertEqual(pm.evaluate("TESTUSDT", 80.0), expected)


class ArmingRecordTests(unittest.TestCase):
    def test_armed_at_and_roe_recorded_on_transition(self):
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        self.assertEqual(pos.armed_at, 0.0, "진입 직후엔 무장 전이어야 한다")

        before = time.time()
        pm.evaluate("TESTUSDT", 100.0 * (1 + 0.05 / 4))  # ROE +5% -> take_profit_min(3%) 초과
        if not pos.armed:
            self.skipTest("이 설정에서는 해당 가격에 무장하지 않는다(소액계좌 분기 등)")
        self.assertGreaterEqual(pos.armed_at, before)
        self.assertGreater(pos.armed_roe, 0.0)

    def test_armed_at_not_overwritten_by_later_cycles(self):
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        pm.evaluate("TESTUSDT", 100.0 * (1 + 0.05 / 4))
        if not pos.armed:
            self.skipTest("무장 미발생 설정")
        first = pos.armed_at
        pm.evaluate("TESTUSDT", 100.0 * (1 + 0.06 / 4))
        self.assertEqual(pos.armed_at, first, "무장 시각은 최초 1회만 기록돼야 한다")

    def test_never_armed_leaves_zero(self):
        """끝까지 무장 못 한 거래는 armed_at=0으로 남아야 구분이 된다."""
        pm = _pm_with_position()
        pm.evaluate("TESTUSDT", 100.2)  # ROE +0.8%, 무장선 미달
        self.assertEqual(pm.positions["TESTUSDT"].armed_at, 0.0)


class FavorableExcursionTests(unittest.TestCase):
    def test_tracks_peak_before_arming(self):
        """peak_pnl이 못 잡는 구간 — 무장 전 고점이 이 작업의 핵심 관측 대상이다."""
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        pm.evaluate("TESTUSDT", 100.0 * (1 + 0.02 / 4))  # ROE +2% (무장선 미달)
        self.assertFalse(pos.armed)
        self.assertAlmostEqual(pos.max_favorable_roe, 2.0, places=3)
        self.assertEqual(pos.peak_pnl, 0.0, "peak_pnl은 무장 전엔 안 움직인다(기존 동작)")

    def test_tracks_adverse_and_keeps_extremes(self):
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        pm.evaluate("TESTUSDT", 100.0 * (1 + 0.02 / 4))
        pm.evaluate("TESTUSDT", 100.0 * (1 - 0.015 / 4))
        pm.evaluate("TESTUSDT", 100.0)
        self.assertAlmostEqual(pos.max_favorable_roe, 2.0, places=3)
        self.assertAlmostEqual(pos.max_adverse_roe, -1.5, places=3)

    def test_short_side_direction(self):
        pm = _pm_with_position(side="SHORT")
        pos = pm.positions["TESTUSDT"]
        pm.evaluate("TESTUSDT", 100.0 * (1 - 0.02 / 4))  # 숏은 하락이 유리
        self.assertAlmostEqual(pos.max_favorable_roe, 2.0, places=3)

    def test_evaluate_calls_counts_polls(self):
        pm = _pm_with_position()
        for _ in range(3):
            pm.evaluate("TESTUSDT", 100.0)
        self.assertEqual(pm.positions["TESTUSDT"].evaluate_calls, 3)


class ResetOnEntryPriceChangeTests(unittest.TestCase):
    def test_average_down_resets_observation_basis(self):
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        pm.evaluate("TESTUSDT", 100.0 * (1 + 0.05 / 4))
        pm.apply_average_down("TESTUSDT", new_entry_price=90.0, new_quantity=2.0, added_margin_usdt=1.0)
        self.assertEqual(pos.max_favorable_roe, 0.0)
        self.assertEqual(pos.max_adverse_roe, 0.0)
        self.assertEqual(pos.armed_at, 0.0)
        self.assertEqual(pos.armed_roe, 0.0)


class LedgerFieldTests(unittest.TestCase):
    def test_trade_record_accepts_and_defaults_fields(self):
        from bot.trade_ledger import TradeRecord
        rec = TradeRecord(
            symbol="TESTUSDT", side="LONG", origin="bot", entry_reason="PUMP_SIGNAL",
            exit_reason="TAKE_PROFIT", entry_price=1.0, exit_price=1.1, quantity=1.0,
            leverage=4.0, entered_at=0.0, exited_at=1.0, held_seconds=1.0,
            estimated_pnl_pct=1.0, estimated_pnl_usdt=0.1, bot_version="t", config_snapshot={},
        )
        # 기본값은 None("모른다")이어야 한다 — 0("무장 안 함")과 구분돼야 복기가 가능하다.
        self.assertIsNone(rec.armed_at)
        self.assertIsNone(rec.max_favorable_roe)
        self.assertIsNone(rec.evaluate_calls)

    def test_record_trade_ledger_carries_observation_fields(self):
        import bot.main as main
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        pos.max_favorable_roe = 4.2
        pos.max_adverse_roe = -1.1
        pos.armed_at = 1234.0
        pos.armed_roe = 3.4
        pos.evaluate_calls = 7
        captured = {}
        with patch.object(main, "append_trade_record", lambda r: captured.setdefault("rec", r)), \
             patch.object(main, "mark_position_closed", lambda s: None):
            main.record_trade_ledger(Config(), pos, "TESTUSDT", "TAKE_PROFIT", 101.0, 1.0, 0.1)
        rec = captured["rec"]
        self.assertEqual(rec.armed_at, 1234.0)
        self.assertEqual(rec.armed_roe, 3.4)
        self.assertEqual(rec.max_favorable_roe, 4.2)
        self.assertEqual(rec.max_adverse_roe, -1.1)
        self.assertEqual(rec.evaluate_calls, 7)

    def test_never_armed_position_records_none_not_zero(self):
        """armed_at=0.0(무장 안 함)은 원장에서 None으로 남긴다 — 시각 0은 1970년이라 오해를 부른다."""
        import bot.main as main
        pm = _pm_with_position()
        pos = pm.positions["TESTUSDT"]
        captured = {}
        with patch.object(main, "append_trade_record", lambda r: captured.setdefault("rec", r)), \
             patch.object(main, "mark_position_closed", lambda s: None):
            main.record_trade_ledger(Config(), pos, "TESTUSDT", "STOP_LOSS", 99.0, -1.0, -0.1)
        self.assertIsNone(captured["rec"].armed_at)


if __name__ == "__main__":
    unittest.main()
