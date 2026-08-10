"""[2026-08-11 사용자요청] IP밴(-1003/418/429) 메시지에서 정확한 해제 대기시간을 뽑아내는
parse_ban_wait_seconds()를 검증한다. 실 API 호출 없음."""
import unittest
from unittest.mock import patch

from bot.exchange import parse_ban_wait_seconds


class FakeBinanceAPIException(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self._message = message

    def __str__(self):
        return self._message


class ParseBanWaitSecondsTests(unittest.TestCase):
    def test_extracts_wait_seconds_from_1003_message(self):
        with patch("bot.exchange.time.time", return_value=1000.0):
            exc = FakeBinanceAPIException(-1003, "APIError(code=-1003): Way too many requests; IP banned until 1010000. Please use WebSocket.")
            wait = parse_ban_wait_seconds(exc)
        self.assertAlmostEqual(wait, 1010.0 - 1000.0, places=3)

    def test_returns_none_for_unrelated_error(self):
        exc = FakeBinanceAPIException(-2019, "Margin is insufficient")
        self.assertIsNone(parse_ban_wait_seconds(exc))

    def test_returns_none_when_no_ban_until_in_message(self):
        exc = FakeBinanceAPIException(-1003, "APIError(code=-1003): Way too many requests")
        self.assertIsNone(parse_ban_wait_seconds(exc))

    def test_never_returns_negative_wait(self):
        with patch("bot.exchange.time.time", return_value=999999999.0):
            exc = FakeBinanceAPIException(-1003, "IP banned until 1000")
            wait = parse_ban_wait_seconds(exc)
        self.assertEqual(wait, 0.0)

    def test_detects_418_by_message_even_without_dash1003_code(self):
        exc = FakeBinanceAPIException(None, "418 I'm a teapot; IP banned until 5000000000000")
        with patch("bot.exchange.time.time", return_value=1000.0):
            wait = parse_ban_wait_seconds(exc)
        self.assertIsNotNone(wait)


if __name__ == "__main__":
    unittest.main()
