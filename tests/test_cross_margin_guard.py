"""[2026-08-15 사용자신고/요청] "AIOUSDT가 크로스 20배로 잡혔어(증거금 2usdt 정도)" →
"300usdt 미만은 절대 크로스 진행하지 않게 해줘".

조사 결과 set_margin_type/set_leverage가 실패해도 경고 로그만 남기고 진입이 그대로
진행되는 구조였다(실측: 마진타입 실패 64건 -4067 "open orders exist", 레버리지 실패 7건
-4028 "Leverage 5 is not valid"). 봇은 보유 심볼에 STOP_MARKET/트레일링 주문을 항상
걸어두므로 -4067은 상시 발생 조건이고, 그 결과 심볼이 CROSSED로 남은 채 주문이 나갈 수
있었다 — 2026-08-09에 격리로 전환한 이유(한 포지션 손실이 계좌 전체를 갉아먹는 것 방지)가
그대로 뚫리는 경로.
"""
import unittest
from unittest.mock import MagicMock

from bot.config import Config


class CrossMarginConfigTests(unittest.TestCase):
    def test_threshold_matches_user_request(self):
        self.assertEqual(Config().cross_margin_min_balance_usdt, 300.0)

    def test_code_default_is_300(self):
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('cross_margin_min_balance_usdt: float = _float("CROSS_MARGIN_MIN_BALANCE_USDT", 300.0)', src)


class SetterReturnValueTests(unittest.TestCase):
    """실패를 삼키지 않고 성공 여부를 반환하는지 — 이 신고의 근본 원인."""

    def _ex(self):
        from bot.exchange import Exchange
        ex = Exchange.__new__(Exchange)  # __init__(API 연결) 우회
        ex.client = MagicMock()
        ex.cfg = Config()
        return ex

    def _api_error(self, code):
        from binance.exceptions import BinanceAPIException
        resp = MagicMock()
        resp.text = "{}"
        e = BinanceAPIException(resp, 400, '{"code": %d, "msg": "x"}' % code)
        e.code = code
        return e

    def test_set_margin_type_returns_true_on_success(self):
        ex = self._ex()
        self.assertTrue(ex.set_margin_type("BTCUSDT", "ISOLATED"))

    def test_set_margin_type_treats_already_set_as_success(self):
        """-4046("no need to change margin type")은 정상 상황이므로 성공으로 취급."""
        ex = self._ex()
        ex.client.futures_change_margin_type.side_effect = self._api_error(-4046)
        self.assertTrue(ex.set_margin_type("BTCUSDT", "ISOLATED"))

    def test_set_margin_type_returns_false_on_open_orders_error(self):
        """-4067: 실제로 64건 발생했던 그 실패 — 반드시 False여야 한다."""
        ex = self._ex()
        ex.client.futures_change_margin_type.side_effect = self._api_error(-4067)
        self.assertFalse(ex.set_margin_type("BTCUSDT", "ISOLATED"))

    def test_set_leverage_returns_true_on_success(self):
        ex = self._ex()
        self.assertTrue(ex.set_leverage("BTCUSDT", 6))

    def test_set_leverage_returns_false_on_invalid_leverage(self):
        """-4028: HUSDT에서 실제로 7건 발생했던 그 실패."""
        ex = self._ex()
        ex.client.futures_change_leverage.side_effect = self._api_error(-4028)
        self.assertFalse(ex.set_leverage("BTCUSDT", 5))


class RiskSettingsLookupTests(unittest.TestCase):
    def _ex(self, positions):
        from bot.exchange import Exchange
        ex = Exchange.__new__(Exchange)
        ex.client = MagicMock()
        ex.cfg = Config()
        ex.client.futures_account.return_value = {"positions": positions}
        return ex

    def test_reads_leverage_and_isolated(self):
        ex = self._ex([{"symbol": "AIOUSDT", "leverage": "20", "isolated": False}])
        self.assertEqual(ex.get_symbol_risk_settings("AIOUSDT"), {"leverage": 20.0, "isolated": False})

    def test_returns_none_when_symbol_absent(self):
        ex = self._ex([{"symbol": "BTCUSDT", "leverage": "6", "isolated": True}])
        self.assertIsNone(ex.get_symbol_risk_settings("AIOUSDT"))

    def test_returns_none_on_api_failure(self):
        ex = self._ex([])
        ex.client.futures_account.side_effect = RuntimeError("down")
        self.assertIsNone(ex.get_symbol_risk_settings("AIOUSDT"))


class EntryGuardSourceTests(unittest.TestCase):
    """execute_entry에 실제로 가드가 배선됐는지 소스 레벨 확인
    (전체 실거래 흐름 mock은 exchange 의존이 과함 — 이 저장소 기존 관례)."""

    def test_cross_margin_guard_wired(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn('if not ex.set_margin_type(symbol, "ISOLATED") and total_balance < cfg.cross_margin_min_balance_usdt:', src)
        self.assertIn("교차 마진으로 잡힐 위험이 있어 진입을 취소합니다", src)

    def test_leverage_guard_wired(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("if not ex.set_leverage(symbol, leverage):", src)
        self.assertIn("진입을 취소합니다(손절선이 의도보다 느슨해질 위험)", src)

    def test_lower_actual_leverage_does_not_block_entry(self):
        """실제 배수가 의도보다 낮으면 위험하지 않으므로 진입은 계속하되 추적값을 실제값으로 맞춘다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("실제 배수 %sx로 더 낮아 진입 계속", src)
        self.assertIn("leverage = int(actual_lev)", src)


if __name__ == "__main__":
    unittest.main()
