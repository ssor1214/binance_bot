"""[2026-08-18 실거래로 발견] 거래소가 먼저 닫았는데 봇 판단으로 기록되던 청산사유 오분류.

`Exchange.close_market_position()`은 -2022(ReduceOnly 거부) 후 재조회에서 이미 포지션이
없으면 **None을 반환**한다(2026-08-10 도입). 그런데 main.py의 청산 호출부 6곳이 반환값을
무시하고 자기가 의도했던 사유를 그대로 원장에 기록했다.

실측(LABUSDT 2026-08-18 11:57:34):
  "순환매매 강제익절: 보유11.3분 ROE=3.41%" -> 시장가 청산 -> -2022
  -> "재조회 결과 이미 포지션 없음" -> 원장에 `TAKE_PROFIT`으로 기록
실제로는 거래소 트레일링이 먼저 체결시킨 건이었다. 이번엔 부호가 같아(둘 다 이익) 손익은
문제없었지만, 거래소가 손실 쪽으로 닫았는데 TAKE_PROFIT으로 남는 경우도 구조적으로 가능하다.
그러면 매시 복기에서 쓰는 "봇 자체판단(STOP_LOSS/EARLY_EXIT/SOFT_STOP) vs 거래소측
(EXTERNAL_*)" 구분이 왜곡된다 — 손실 원인을 진입 문제로 볼지 청산 문제로 볼지가 갈리는 축이다.

분류 기준은 reconcile_positions의 기존 규칙과 동일하게 실현손익 부호를 쓴다(손절 주문은 항상
손실로, 트레일링/익절 주문은 항상 이익 또는 최소 본절로 체결되므로 부호로 구분 가능).
"""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.main import classify_external_close_context, resolve_exit_reason


class ResolveExitReasonTests(unittest.TestCase):
    def test_normal_close_keeps_bot_reason(self):
        """봇이 실제로 청산에 성공했으면(주문 응답 있음) 의도한 사유를 그대로 쓴다."""
        order = {"orderId": 123, "status": "FILLED"}
        for reason in ("STOP_LOSS", "TAKE_PROFIT", "EARLY_EXIT", "SOFT_STOP",
                       "TIME_STOP", "FUNDING_FORCE_CLOSE", "TAKE_PROFIT_MOMENTUM_LOCK",
                       "UNARMED_MID_HOLD_CUT"):
            with self.subTest(reason=reason):
                self.assertEqual(resolve_exit_reason(order, reason, -1.0), reason)
                self.assertEqual(resolve_exit_reason(order, reason, +1.0), reason)

    def test_exchange_closed_first_becomes_external(self):
        """None = 거래소가 먼저 닫음. 봇 사유가 아니라 EXTERNAL_CLOSE_*로 기록해야 한다."""
        self.assertEqual(resolve_exit_reason(None, "TAKE_PROFIT", +0.05), "EXTERNAL_CLOSE_PROFIT")
        self.assertEqual(resolve_exit_reason(None, "TAKE_PROFIT", -0.05), "EXTERNAL_CLOSE_LOSS")
        self.assertEqual(resolve_exit_reason(None, "STOP_LOSS", -0.05), "EXTERNAL_CLOSE_LOSS")

    def test_labusdt_incident_reproduction(self):
        """실사고 재현: 강제익절 판단(ROE 3.41%) 직후 거래소가 먼저 닫은 건."""
        self.assertEqual(
            resolve_exit_reason(None, "TAKE_PROFIT", +0.0648),
            "EXTERNAL_CLOSE_PROFIT",
            "봇이 익절하려던 찰나 거래소가 닫았으면 거래소측 청산으로 남아야 한다",
        )

    def test_zero_pnl_counts_as_loss_side(self):
        """본절(0)은 손실 쪽으로 분류 — reconcile_positions의 기존 규칙(>0만 PROFIT)과 일치."""
        self.assertEqual(resolve_exit_reason(None, "TAKE_PROFIT", 0.0), "EXTERNAL_CLOSE_LOSS")


class AllCallSitesPatchedTests(unittest.TestCase):
    """청산 호출부가 하나라도 빠지면 그 경로만 조용히 오분류가 남는다 — 소스로 확인한다."""

    def setUp(self):
        src = Path("bot/main.py").read_text(encoding="utf-8-sig")
        self.tree = ast.parse(src)
        self.src = src

    def test_every_close_call_capturing_result(self):
        """원장에 기록하는 경로의 close_market_position 호출은 반환값을 받아야 한다."""
        bare = self.src.count("            ex.close_market_position(symbol, pos.side")
        captured = self.src.count("close_result = ex.close_market_position")
        self.assertEqual(bare, 0, "반환값을 버리는 청산 호출이 남아 있다")
        self.assertGreaterEqual(captured, 3, "직접 시장가 청산 경로는 모두 반환값을 받아야 한다")

    def test_every_ledger_record_uses_resolver(self):
        """record_trade_ledger의 사유 인자가 전부 resolve_exit_reason을 거치는지."""
        raw_calls = []
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "record_trade_ledger"):
                continue
            reason_arg = node.args[3] if len(node.args) > 3 else None
            wrapped = (isinstance(reason_arg, ast.Call)
                       and getattr(reason_arg.func, "id", "") == "resolve_exit_reason")
            # reconcile_positions의 EXTERNAL_CLOSE 경로는 이미 거래소 청산이라 감쌀 필요가 없다.
            already_external = (isinstance(reason_arg, ast.Name)
                                and reason_arg.id == "external_close_reason")
            if not wrapped and not already_external:
                raw_calls.append(node.lineno)
        self.assertEqual(raw_calls, [],
                         "resolve_exit_reason을 거치지 않는 원장 기록이 있다(줄번호: %s)" % raw_calls)


class ExternalCloseContextTests(unittest.TestCase):
    def test_profit_side_contexts(self):
        self.assertEqual(classify_external_close_context(SimpleNamespace(armed_at=123.0), 0.1), "armed_trailing_profit")
        self.assertEqual(classify_external_close_context(SimpleNamespace(armed_at=0.0), 0.1), "unarmed_profit")

    def test_loss_side_context_priority(self):
        pos = SimpleNamespace(
            protection_state="TRAILING_ACTIVE",
            stop_loss_widened=False,
            sl_defer_until=0.0,
            sl_defer_used=False,
            tp_fallback_order_id=None,
        )
        self.assertEqual(classify_external_close_context(pos, -0.1), "trailing_active_loss")
        pos.tp_fallback_order_id = 123
        self.assertEqual(classify_external_close_context(pos, -0.1), "tp_fallback_active_loss")
        pos.tp_fallback_order_id = None
        pos.sl_defer_used = True
        self.assertEqual(classify_external_close_context(pos, -0.1), "sl_defer_reverted_loss")
        pos.sl_defer_used = False
        pos.stop_loss_widened = True
        self.assertEqual(classify_external_close_context(pos, -0.1), "widened_stop_loss")


if __name__ == "__main__":
    unittest.main()
