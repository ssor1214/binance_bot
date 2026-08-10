"""[2026-08-09] TRAILING_STOP_MARKET 실패시 TAKE_PROFIT_MARKET 거래소측 폴백을 등록하는
로직에 대한 mock 기반 단위테스트. 실제 바이낸스 API를 절대 호출하지 않고, Exchange의
self.client를 가짜 객체로 바꿔치기해서 요청 페이로드만 검증한다.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.exchange import Exchange
from bot.position_manager import TrackedPosition


class FakeAlgoOrderClient:
    """futures_create_algo_order 호출을 기록만 하는 가짜 바이낸스 클라이언트.
    raise_types에 지정된 type의 주문은 예외를 던지도록 해서 실패 경로도 테스트한다."""

    def __init__(self, raise_types=None):
        self.calls = []
        self.raise_types = raise_types or set()
        self._next_id = 1000

    def futures_create_algo_order(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("type") in self.raise_types:
            raise RuntimeError(f"{kwargs.get('type')} 등록 실패(테스트 모의)")
        order_id = self._next_id
        self._next_id += 1
        return {"algoId": order_id}


def make_exchange(fake_client):
    """Exchange.__init__은 실제 API 키로 바이낸스 클라이언트를 생성하려 하므로 우회하고,
    self.client와 심볼 필터 캐시만 직접 채워서 완전히 오프라인으로 동작하게 만든다."""
    ex = Exchange.__new__(Exchange)
    ex.client = fake_client
    ex._symbol_info_cache = {
        "TESTUSDT": {
            "quantity_precision": 1,
            "price_precision": 4,
            "min_qty": 0.1,
            "step_size": 0.1,
            "min_notional": 5.0,
        }
    }
    return ex


class PlaceTakeProfitMarketTests(unittest.TestCase):
    def test_builds_correct_algo_order_payload_for_long(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        result = ex.place_take_profit_market("TESTUSDT", "LONG", 10.0, 1.2345)
        self.assertEqual(result["algoId"], 1000)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["algoType"], "CONDITIONAL")
        self.assertEqual(call["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(call["side"], "SELL")  # LONG 청산은 SELL
        self.assertEqual(call["quantity"], 10.0)
        self.assertEqual(call["triggerPrice"], 1.2345)  # price_precision=4 그대로
        self.assertEqual(call["reduceOnly"], "true")

    def test_short_side_closes_with_buy(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        ex.place_take_profit_market("TESTUSDT", "SHORT", 5.0, 0.9)
        self.assertEqual(client.calls[0]["side"], "BUY")  # SHORT 청산은 BUY

    def test_price_is_rounded_to_symbol_precision(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        ex.place_take_profit_market("TESTUSDT", "LONG", 1.0, 1.23456789)
        self.assertEqual(client.calls[0]["triggerPrice"], 1.2346)  # price_precision=4


class PlaceTrailingStopMarketTests(unittest.TestCase):
    def test_callback_rate_field_name_is_callbackRate(self):
        """[2026-08-09] -2007 Invalid callBack rate 원인이 파라미터명(priceRate->callbackRate)
        오류였음을 재발방지 차원에서 고정한다. 이 필드명이 다시 바뀌면 테스트가 즉시 실패한다."""
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        ex.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 0.38)
        call = client.calls[0]
        self.assertIn("callbackRate", call)
        self.assertNotIn("priceRate", call)
        self.assertEqual(call["callbackRate"], 0.38)
        self.assertEqual(call["type"], "TRAILING_STOP_MARKET")

    def test_callback_rate_is_clamped_to_binance_allowed_range(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        ex.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 0.01)  # 0.1 미만
        self.assertEqual(client.calls[0]["callbackRate"], 0.1)
        client2 = FakeAlgoOrderClient()
        ex2 = make_exchange(client2)
        ex2.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 9.0)  # 5.0 초과
        self.assertEqual(client2.calls[0]["callbackRate"], 5.0)


class FallbackBehaviorTests(unittest.TestCase):
    """트레일링 등록이 실패했을 때, execute_entry 내부와 동일한 순서(트레일링 시도 ->
    실패시 TAKE_PROFIT_MARKET 폴백 시도)로 호출했을 때의 결과만 검증한다. execute_entry
    자체는 klines/지표 계산 등 의존성이 많아 여기서는 그 부분만 별도로 떼어 확인한다."""

    def test_trailing_failure_falls_back_to_take_profit_market(self):
        client = FakeAlgoOrderClient(raise_types={"TRAILING_STOP_MARKET"})
        ex = make_exchange(client)

        trailing_order_id = None
        tp_fallback_order_id = None
        try:
            order = ex.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 0.38)
            trailing_order_id = order["algoId"]
        except Exception:
            order = ex.place_take_profit_market("TESTUSDT", "LONG", 10.0, 1.05)
            tp_fallback_order_id = order["algoId"]

        self.assertIsNone(trailing_order_id)
        self.assertIsNotNone(tp_fallback_order_id)
        types_called = [c["type"] for c in client.calls]
        self.assertEqual(types_called, ["TRAILING_STOP_MARKET", "TAKE_PROFIT_MARKET"])

    def test_both_trailing_and_fallback_fail_raises_for_critical_handling(self):
        client = FakeAlgoOrderClient(raise_types={"TRAILING_STOP_MARKET", "TAKE_PROFIT_MARKET"})
        ex = make_exchange(client)

        trailing_ok = False
        fallback_ok = False
        try:
            ex.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 0.38)
            trailing_ok = True
        except Exception:
            try:
                ex.place_take_profit_market("TESTUSDT", "LONG", 10.0, 1.05)
                fallback_ok = True
            except Exception:
                pass  # main.py에서는 이 지점에서 log.critical + tg.notify_error를 호출한다

        self.assertFalse(trailing_ok)
        self.assertFalse(fallback_ok)
        # 두 시도 모두 실제로 이루어졌는지(조용히 건너뛰지 않았는지) 확인
        self.assertEqual([c["type"] for c in client.calls], ["TRAILING_STOP_MARKET", "TAKE_PROFIT_MARKET"])


class VerifyRegisteredOrderTests(unittest.TestCase):
    """[2026-08-09] execute_entry가 실제로 하는 것과 동일하게: place_trailing_stop_market
    응답만 믿지 않고 get_open_algo_order_ids()로 실제 존재를 재확인하는 패턴을 검증한다."""

    def test_verification_passes_when_order_is_actually_open(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        order = ex.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 0.38)
        algo_id = int(order["algoId"])
        # get_open_algo_order_ids는 futures_get_open_algo_orders를 호출하므로 별도로 스텁한다
        client.futures_get_open_algo_orders = lambda: [{"algoId": algo_id}]
        self.assertIn(algo_id, ex.get_open_algo_order_ids())

    def test_verification_fails_when_order_missing_despite_success_response(self):
        """API가 성공을 알렸지만(algoId 반환) 실제 목록엔 없는 경우 — 이땐 폴백으로 넘어가야 한다."""
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        order = ex.place_trailing_stop_market("TESTUSDT", "LONG", 10.0, 1.05, 0.38)
        algo_id = int(order["algoId"])
        client.futures_get_open_algo_orders = lambda: []  # 실제로는 비어있음
        self.assertNotIn(algo_id, ex.get_open_algo_order_ids())


class AdoptExistingStopMarketTests(unittest.TestCase):
    """Restart recovery should adopt an existing exchange-side hard stop instead
    of placing a duplicate STOP_MARKET when the restored in-memory position has
    no stop_order_id."""

    def test_finds_existing_stop_market_by_symbol_side_qty_and_trigger(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        orders = [
            {
                "algoId": 11,
                "symbol": "TESTUSDT",
                "side": "SELL",
                "type": None,
                "quantity": "10.0",
                "triggerPrice": "0.985",
                "activatePrice": None,
                "callbackRate": None,
            }
        ]

        found = ex.find_matching_stop_market_order(
            orders, "TESTUSDT", "LONG", 10.0, 0.985,
        )

        self.assertIsNotNone(found)
        self.assertEqual(int(found["algoId"]), 11)

    def test_does_not_adopt_trailing_order_as_hard_stop(self):
        client = FakeAlgoOrderClient()
        ex = make_exchange(client)
        orders = [
            {
                "algoId": 22,
                "symbol": "TESTUSDT",
                "side": "SELL",
                "type": None,
                "quantity": "10.0",
                "triggerPrice": "0.985",
                "activatePrice": "1.010",
                "callbackRate": "0.5",
            }
        ]

        found = ex.find_matching_stop_market_order(
            orders, "TESTUSDT", "LONG", 10.0, 0.985,
        )

        self.assertIsNone(found)


class ProtectionStateTests(unittest.TestCase):
    """TrackedPosition.protection_state가 세 주문ID 필드로부터 올바르게 계산되는지 확인한다."""

    def _pos(self, stop_id=None, trailing_id=None, tp_fallback_id=None):
        return TrackedPosition(
            symbol="TESTUSDT", side="LONG", entry_price=1.0, quantity=10.0,
            stop_order_id=stop_id, trailing_order_id=trailing_id, tp_fallback_order_id=tp_fallback_id,
        )

    def test_no_stop_order_is_unprotected(self):
        self.assertEqual(self._pos().protection_state, "UNPROTECTED")

    def test_stop_only_when_only_stop_registered(self):
        self.assertEqual(self._pos(stop_id=1).protection_state, "STOP_ONLY")

    def test_trailing_active_when_trailing_registered(self):
        self.assertEqual(self._pos(stop_id=1, trailing_id=2).protection_state, "TRAILING_ACTIVE")

    def test_tp_fallback_active_when_only_fallback_registered(self):
        self.assertEqual(self._pos(stop_id=1, tp_fallback_id=3).protection_state, "TP_FALLBACK_ACTIVE")

    def test_trailing_takes_priority_over_fallback_if_both_somehow_set(self):
        self.assertEqual(self._pos(stop_id=1, trailing_id=2, tp_fallback_id=3).protection_state, "TRAILING_ACTIVE")


if __name__ == "__main__":
    unittest.main()
