import socket
import urllib.request
import sys
import types
import unittest

import offline_backtest as ob


def candle(ts, o, h, l, c, v=100, taker=60):
    return ob.Candle(ts, o, h, l, c, v, v * c, taker)


class OfflineBacktestTests(unittest.TestCase):
    def test_network_is_blocked(self):
        ob.disable_network()
        with self.assertRaisesRegex(RuntimeError, ob.OFFLINE_ERROR):
            socket.create_connection(("example.com", 80))
        with self.assertRaisesRegex(RuntimeError, ob.OFFLINE_ERROR):
            urllib.request.urlopen("https://example.com")

    def test_trailing_uses_prior_candle_peak(self):
        settings = ob.Settings(leverage=4, stop_roe_pct=5, take_profit_roe_pct=3,
                               hard_take_profit_roe_pct=20, trailing_drawdown_roe_pct=1)
        pos = ob.Position("X", "LONG", 0, 100, 1, 25, 0, 101, trailing_armed=True)
        self.assertEqual(ob.exit_decision(pos, candle(60000, 101, 101.1, 100.6, 100.8), settings),
                         (100.7475, "trailing_stop"))

    def test_average_down_is_opt_in_and_once_only(self):
        c = candle(60000, 100, 100, 99, 99.5)
        disabled = ob.Position("X", "LONG", 0, 100, 1, 25, 0, 100)
        self.assertEqual(ob._average_down(disabled, c, ob.Settings(average_down=False), 20), 20)
        enabled = ob.Position("X", "LONG", 0, 100, 1, 25, 0, 100)
        balance = ob._average_down(enabled, c, ob.Settings(average_down=True, fee_rate=0, slippage_bps=0), 20)
        self.assertLess(balance, 20)
        self.assertTrue(enabled.average_down_done)
        self.assertEqual(ob._average_down(enabled, c, ob.Settings(average_down=True), balance), balance)

    def test_loaded_requests_entry_points_are_blocked_without_importing_requests(self):
        fake = types.SimpleNamespace(request=lambda: None, Session=type("Session", (), {"request": lambda self: None}))
        old = sys.modules.get("requests")
        sys.modules["requests"] = fake
        try:
            ob.disable_network()
            with self.assertRaisesRegex(RuntimeError, ob.OFFLINE_ERROR):
                fake.request()
            with self.assertRaisesRegex(RuntimeError, ob.OFFLINE_ERROR):
                fake.Session().request()
        finally:
            if old is None:
                del sys.modules["requests"]
            else:
                sys.modules["requests"] = old

    def test_signal_needs_warmup_and_uses_only_history(self):
        settings = ob.Settings(warmup=60)
        history = [candle(i * 60000, 1, 1, 1, 1) for i in range(59)]
        snapshot = list(history)
        self.assertIsNone(ob.signal(history, settings))
        self.assertEqual(history, snapshot)  # signal cannot consume/append future candles

    def test_trade_sides_can_disable_short_entries(self):
        settings = ob.Settings(warmup=60, trade_sides="long-only")
        history = [candle(i * 60000, 100, 101, 99, 100, v=100, taker=50) for i in range(59)]
        history.append(candle(59 * 60000, 100, 100, 99, 99, v=1000, taker=20))
        self.assertIsNone(ob.signal(history, settings))

    def test_trade_sides_can_disable_long_entries(self):
        settings = ob.Settings(warmup=60, trade_sides="short-only")
        history = [candle(i * 60000, 100, 101, 99, 100, v=100, taker=50) for i in range(59)]
        history.append(candle(59 * 60000, 100, 101, 100, 101, v=1000, taker=80))
        self.assertIsNone(ob.signal(history, settings))

    def test_adverse_slippage(self):
        self.assertGreater(ob._fill(100, "LONG", True, 10), 100)
        self.assertLess(ob._fill(100, "LONG", False, 10), 100)

    def test_limit_entry_pullback_must_touch_price(self):
        settings = ob.Settings(limit_entry_pullback_bps=10)
        self.assertAlmostEqual(ob._entry_limit_price(100, "LONG", settings), 99.9)
        self.assertAlmostEqual(ob._entry_limit_price(100, "SHORT", settings), 100.1)
        self.assertTrue(ob._entry_limit_filled(candle(0, 100, 101, 99.8, 100), "LONG", 99.9))
        self.assertFalse(ob._entry_limit_filled(candle(0, 100, 101, 99.95, 100), "LONG", 99.9))
        self.assertTrue(ob._entry_limit_filled(candle(0, 100, 100.2, 99, 100), "SHORT", 100.1))
        self.assertFalse(ob._entry_limit_filled(candle(0, 100, 100.05, 99, 100), "SHORT", 100.1))

    def test_short_scalp_reversal_filter_rejects_long_lower_wick(self):
        settings = ob.Settings(short_max_lower_wick_body_ratio=1.0)
        self.assertFalse(ob._passes_short_scalp_reversal_filter(candle(0, 100, 101, 95, 99), settings))
        self.assertTrue(ob._passes_short_scalp_reversal_filter(candle(0, 100, 101, 98.8, 99), settings))

    def test_short_scalp_reversal_filter_rejects_close_far_from_low(self):
        settings = ob.Settings(short_max_close_from_low_pct=2.0)
        self.assertFalse(ob._passes_short_scalp_reversal_filter(candle(0, 100, 101, 95, 98), settings))
        self.assertTrue(ob._passes_short_scalp_reversal_filter(candle(0, 100, 101, 98, 99), settings))

    def test_close_records_mae_mfe(self):
        settings = ob.Settings(leverage=4)
        pos = ob.Position("X", "LONG", 0, 100, 1, 10, 0, 100)
        c = candle(60000, 100, 101, 99, 100)
        pos.max_adverse_roe = min(pos.max_adverse_roe, ob._adverse_roe(pos, c, settings))
        pos.max_favorable_roe = max(pos.max_favorable_roe, ob._favorable_roe(pos, c, settings))
        row, _ = ob._close(pos, 100, 60000, "test", settings, 0)
        self.assertAlmostEqual(row["max_adverse_roe"], -4.0)
        self.assertAlmostEqual(row["max_favorable_roe"], 4.0)

    def test_cost_accounting_identity(self):
        settings = ob.Settings(fee_rate=0.001, slippage_bps=10)
        pos = ob.Position("X", "LONG", 0, 100.1, 1, 25, 0.1001, 100.1)
        row, _ = ob._close(pos, 101, 60000, "test", settings, 0)
        self.assertAlmostEqual(row["net_pnl"], row["gross_pnl"] - row["fee"] - row["slippage"] - row["funding"])

    def test_same_candle_stop_wins_over_target(self):
        settings = ob.Settings(warmup=999, max_positions=1, slippage_bps=0, fee_rate=0, stop_roe_pct=4, take_profit_roe_pct=4)
        data = {"X": [candle(0, 100, 100, 100, 100), candle(60000, 100, 102, 98, 100)]}
        # Both 1% levels at 4x are touched, so the conservative stop must win.
        pos = ob.Position("X", "LONG", 0, 100, 1, 10, 0, 100)
        self.assertEqual(ob.exit_decision(pos, data["X"][1], settings), (99.0, "stop_loss"))

    def test_fee_is_deducted(self):
        settings = ob.Settings(fee_rate=0.001, slippage_bps=0)
        pos = ob.Position("X", "LONG", 0, 100, 1, 10, 0.1, 100)
        row, balance = ob._close(pos, 100, 60000, "end", settings, 0)
        self.assertAlmostEqual(row["fee"], 0.2)
        self.assertAlmostEqual(balance, 9.9)  # entry fee was already paid at entry


if __name__ == "__main__":
    unittest.main()
