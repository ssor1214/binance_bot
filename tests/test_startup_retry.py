import unittest
from unittest.mock import patch

from bot.main import _run_startup_step_with_retries


class StartupRetryTests(unittest.TestCase):
    def test_returns_immediately_on_first_success(self):
        self.assertEqual(_run_startup_step_with_retries("ok", lambda: 123, attempts=3, delay_sec=0), 123)

    def test_retries_until_success(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("temporary")
            return "done"

        with patch("bot.main.time.sleep") as sleep_mock, \
             patch("bot.main.write_heartbeat") as heartbeat_mock:
            result = _run_startup_step_with_retries("flaky", flaky, attempts=5, delay_sec=1.5)

        self.assertEqual(result, "done")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(heartbeat_mock.call_count, 2)

    def test_raises_last_exception_after_exhausting_attempts(self):
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            raise TimeoutError("still failing")

        with patch("bot.main.time.sleep") as sleep_mock, \
             patch("bot.main.write_heartbeat") as heartbeat_mock:
            with self.assertRaises(TimeoutError):
                _run_startup_step_with_retries("fail", always_fail, attempts=3, delay_sec=2.0)

        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(heartbeat_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
