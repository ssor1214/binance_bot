"""[2026-08-10] close_market_position()의 -2022(ReduceOnly Order is rejected) 재시도
로직 단위테스트. 실거래에서 손절 청산이 -2022로 두 번 연속 실패하고 27초 뒤에야 거래소
자체 스탑이 대신 처리된 사례(GRVTUSDT, -3.01USDT) 발견 후 추가됨 — 실패 시 실제 보유
수량을 즉시 재조회해 재시도해야 한다. 실 API를 절대 호출하지 않는다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from binance.exceptions import BinanceAPIException

from bot.exchange import Exchange


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def make_2022_error():
    resp = FakeResponse(400, '{"code": -2022, "msg": "ReduceOnly Order is rejected."}')
    return BinanceAPIException(resp, 400, resp.text)


def make_other_error(code=-1000):
    resp = FakeResponse(400, f'{{"code": {code}, "msg": "some other error"}}')
    return BinanceAPIException(resp, 400, resp.text)


class FakeClient:
    """futures_create_order 호출을 기록하고, 지정된 횟수만큼 -2022를 던지도록 설정
    가능한 가짜 클라이언트. futures_position_information은 재조회용 응답을 고정 반환."""

    def __init__(self, fail_times=0, error=None, position_info=None):
        self.order_calls = []
        self.fail_times = fail_times
        self._fail_count = 0
        self.error = error or make_2022_error()
        self.position_info = position_info if position_info is not None else []

    def futures_create_order(self, **kwargs):
        self.order_calls.append(kwargs)
        if self._fail_count < self.fail_times:
            self._fail_count += 1
            raise self.error
        return {"orderId": 999, "status": "FILLED"}

    def futures_position_information(self, symbol=None):
        return self.position_info


def make_exchange(client):
    ex = Exchange.__new__(Exchange)
    ex.client = client
    return ex


class CloseMarketPositionRetryTests(unittest.TestCase):
    def test_succeeds_immediately_when_no_error(self):
        client = FakeClient(fail_times=0)
        ex = make_exchange(client)
        result = ex.close_market_position("TESTUSDT", "LONG", 10.0)
        self.assertEqual(len(client.order_calls), 1)
        self.assertEqual(client.order_calls[0]["quantity"], 10.0)
        self.assertIsNotNone(result)

    def test_retries_with_live_quantity_on_2022(self):
        """[핵심] -2022로 첫 시도가 실패하면, 거래소 실제 보유수량을 재조회해서
        그 수량으로 즉시 재시도해야 한다(추적 수량과 실제 수량이 어긋난 경우 대응)."""
        position_info = [{"symbol": "TESTUSDT", "positionAmt": "7.5", "entryPrice": "100.0"}]
        client = FakeClient(fail_times=1, position_info=position_info)
        ex = make_exchange(client)
        result = ex.close_market_position("TESTUSDT", "LONG", 10.0)  # 봇 추적 수량은 10.0
        self.assertEqual(len(client.order_calls), 2)
        self.assertEqual(client.order_calls[0]["quantity"], 10.0)  # 1차 시도: 원래 추적 수량
        self.assertEqual(client.order_calls[1]["quantity"], 7.5)  # 2차 시도: 재조회한 실제 수량
        self.assertEqual(client.order_calls[1]["side"], "SELL")  # LONG 청산은 SELL
        self.assertIsNotNone(result)

    def test_returns_none_when_position_already_closed_on_retry(self):
        """재조회했더니 이미 포지션이 없으면(다른 경로로 이미 종료됨) 불필요한 재주문 없이
        None을 반환해야 한다."""
        client = FakeClient(fail_times=1, position_info=[])  # positionAmt 없음 = 청산됨
        ex = make_exchange(client)
        result = ex.close_market_position("TESTUSDT", "LONG", 10.0)
        self.assertEqual(len(client.order_calls), 1)  # 재시도 주문을 넣지 않음
        self.assertIsNone(result)

    def test_short_side_retry_uses_buy(self):
        position_info = [{"symbol": "TESTUSDT", "positionAmt": "-5.0", "entryPrice": "100.0"}]
        client = FakeClient(fail_times=1, position_info=position_info)
        ex = make_exchange(client)
        ex.close_market_position("TESTUSDT", "SHORT", 5.0)
        self.assertEqual(client.order_calls[1]["side"], "BUY")

    def test_does_not_retry_on_other_error_codes(self):
        """[회귀] -2022가 아닌 다른 에러는 그대로 위로 전파해야 한다(엉뚱하게 삼키면 안 됨)."""
        client = FakeClient(fail_times=1, error=make_other_error(-1000))
        ex = make_exchange(client)
        with self.assertRaises(BinanceAPIException):
            ex.close_market_position("TESTUSDT", "LONG", 10.0)
        self.assertEqual(len(client.order_calls), 1)  # 재시도 안 함

    def test_does_not_infinite_loop_if_retry_also_fails(self):
        """재시도까지 -2022로 실패하면 더 이상 재시도하지 않고(무한루프 방지) 예외를
        위로 전파해야 한다."""
        position_info = [{"symbol": "TESTUSDT", "positionAmt": "7.5", "entryPrice": "100.0"}]
        client = FakeClient(fail_times=2, position_info=position_info)  # 1차, 2차 모두 실패
        ex = make_exchange(client)
        with self.assertRaises(BinanceAPIException):
            ex.close_market_position("TESTUSDT", "LONG", 10.0)
        self.assertEqual(len(client.order_calls), 2)  # 1차 + 재시도 1회, 그 이상은 안 함


if __name__ == "__main__":
    unittest.main()
