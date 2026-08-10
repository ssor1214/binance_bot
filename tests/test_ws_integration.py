"""[2026-08-10] Exchange.get_klines()의 WS 캔들 캐시 오버레이 + record_trade_ledger()의
FillTracker 연동 배선 테스트. 실제 바이낸스 API를 절대 호출하지 않는다 — Exchange는
client를 가짜로 바꿔치기하고, RollingKlineCache/FillTracker는 순수 오프라인 객체다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import Config
from bot.exchange import Exchange
from bot.main import record_trade_ledger
from bot.position_manager import PositionManager, TrackedPosition
from bot.ws_client import FillTracker, OrderFill, RollingKlineCache


class FakeFuturesClient:
    """futures_klines 호출 여부/횟수만 기록하는 가짜 클라이언트."""

    def __init__(self):
        self.calls = []

    def futures_klines(self, symbol, interval, limit):
        self.calls.append((symbol, interval, limit))
        rows = []
        for i in range(limit + 1):  # get_klines는 마지막 1개(미완성)를 버리므로 +1개 반환
            rows.append([
                1700000000000 + i * 60000, "100.0", "101.0", "99.0", "100.5", "10.0",
                1700000059999 + i * 60000, "1000.0", 5, "3.0", "300.0", "0",
            ])
        return rows


def make_exchange(cfg=None):
    ex = Exchange.__new__(Exchange)
    ex.cfg = cfg or Config()
    ex.client = FakeFuturesClient()
    ex._symbol_info_cache = {}
    ex._ws_kline_cache = None
    ex._fill_tracker = None
    return ex


def rest_shaped_df(n, interval_symbol="AUSDT"):
    rows = []
    for i in range(n):
        rows.append({
            "open_time": pd.Timestamp(1700000000000 + i * 60000, unit="ms"), "open": 100.0 + i,
            "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i, "volume": 10.0,
            "close_time": 1700000059999 + i * 60000, "quote_asset_volume": 1000.0,
            "num_trades": 5, "taker_buy_base": 3.0, "taker_buy_quote": 300.0, "ignore": 0,
        })
    return pd.DataFrame(rows)


class ExchangeGetKlinesWsOverlayTests(unittest.TestCase):
    def test_uses_rest_when_no_ws_cache_attached(self):
        ex = make_exchange()
        df = ex.get_klines("AUSDT", limit=50)
        self.assertEqual(len(ex.client.calls), 1)
        self.assertEqual(len(df), 50)

    def test_uses_ws_cache_when_sufficient_history_available(self):
        ex = make_exchange()
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(60))
        ex.set_ws_kline_cache(cache)
        df = ex.get_klines("AUSDT", limit=50)
        self.assertEqual(len(ex.client.calls), 0)  # REST 호출 없이 캐시로 해결
        self.assertEqual(len(df), 50)

    def test_falls_back_to_rest_when_ws_cache_has_insufficient_history(self):
        ex = make_exchange()
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(10))  # limit(50)보다 적음
        ex.set_ws_kline_cache(cache)
        df = ex.get_klines("AUSDT", limit=50)
        self.assertEqual(len(ex.client.calls), 1)  # 부족하니 REST로 폴백
        self.assertEqual(len(df), 50)

    def test_falls_back_to_rest_when_ws_cache_is_stale(self):
        """[2026-08-10 실거래 사고 회귀테스트] WS 연결이 끊겨도 캐시엔 예전 캔들이 남아있어
        개수만으로는 충분해 보일 수 있다 — 최근 갱신 안 됐으면(신선하지 않으면) 반드시 REST로
        폴백해야 한다. 이걸 놓치면 멈춰버린 옛날 가격으로 계속 매매판단을 하게 된다."""
        ex = make_exchange()
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(60))  # 개수는 충분(limit=50보다 많음)
        with patch("bot.ws_client.time.time", return_value=cache._last_update_ts["AUSDT"] + 999):
            ex.set_ws_kline_cache(cache)
            df = ex.get_klines("AUSDT", limit=50)
        self.assertEqual(len(ex.client.calls), 1)  # 오래됐으니 REST로 폴백해야 함
        self.assertEqual(len(df), 50)

    def test_falls_back_to_rest_for_different_interval(self):
        """상위 타임프레임 조회(MTF) 등 cfg.interval과 다른 interval을 요청하면
        WS 캐시(항상 cfg.interval 기준)를 쓰면 안 되고 REST로 가야 한다."""
        cfg = Config()
        cfg.interval = "1m"
        ex = make_exchange(cfg)
        cache = RollingKlineCache()
        cache.seed("AUSDT", rest_shaped_df(60))
        ex.set_ws_kline_cache(cache)
        df = ex.get_klines("AUSDT", limit=50, interval="5m")
        self.assertEqual(len(ex.client.calls), 1)


class GetLastFillTests(unittest.TestCase):
    def test_returns_none_when_no_tracker_attached(self):
        ex = make_exchange()
        self.assertIsNone(ex.get_last_fill("AUSDT"))

    def test_returns_fill_when_tracker_has_one(self):
        ex = make_exchange()
        tracker = FillTracker()
        tracker.on_message({
            "e": "ORDER_TRADE_UPDATE", "E": 1,
            "o": {"s": "AUSDT", "S": "BUY", "x": "TRADE", "X": "FILLED", "ap": "10.5", "n": "0.01", "N": "USDT", "rp": "0.2"},
        })
        ex.set_fill_tracker(tracker)
        fill = ex.get_last_fill("AUSDT")
        self.assertEqual(fill.avg_price, 10.5)


def make_pm():
    with patch.object(PositionManager, "_load_stats", lambda self: None), \
         patch.object(PositionManager, "_save_stats", lambda self: None):
        return PositionManager(Config())


class RecordTradeLedgerFillEnrichmentTests(unittest.TestCase):
    """record_trade_ledger가 ex.get_last_fill()로 실제 체결가를 채우는지 검증한다.
    실 파일시스템 접근(append_trade_record)은 그대로 두되, 임시 디렉터리로 우회한다."""

    def _make_pos(self):
        return TrackedPosition(symbol="AUSDT", side="LONG", entry_price=100.0, quantity=10.0,
                                leverage=4.0, origin="bot")

    def test_fills_actual_exit_price_and_commission_when_fill_available(self):
        import bot.main as m

        ex = make_exchange()
        tracker = FillTracker()
        tracker.on_message({
            "e": "ORDER_TRADE_UPDATE", "E": 1,
            "o": {"s": "AUSDT", "S": "SELL", "x": "TRADE", "X": "FILLED", "ap": "105.2", "n": "0.05", "N": "USDT", "rp": "5.2"},
        })
        ex.set_fill_tracker(tracker)
        pos = self._make_pos()

        with patch.object(m, "append_trade_record") as fake_append, \
             patch.object(m, "mark_position_closed"):
            record_trade_ledger(Config(), pos, "AUSDT", "TAKE_PROFIT", 105.0, 5.0, 5.0, ex)

        fake_append.assert_called_once()
        record = fake_append.call_args[0][0]
        self.assertEqual(record.actual_fill_exit_price, 105.2)
        self.assertEqual(record.commission_usdt, 0.05)

    def test_leaves_fill_fields_none_when_no_tracker(self):
        import bot.main as m

        ex = make_exchange()  # fill_tracker 미연결
        pos = self._make_pos()

        with patch.object(m, "append_trade_record") as fake_append, \
             patch.object(m, "mark_position_closed"):
            record_trade_ledger(Config(), pos, "AUSDT", "TAKE_PROFIT", 105.0, 5.0, 5.0, ex)

        record = fake_append.call_args[0][0]
        self.assertIsNone(record.actual_fill_exit_price)

    def test_works_without_ex_argument_at_all(self):
        """ex를 아예 안 넘기는(기존 호출부와의 하위호환) 경우에도 예외 없이 동작해야 한다."""
        import bot.main as m

        pos = self._make_pos()
        with patch.object(m, "append_trade_record") as fake_append, \
             patch.object(m, "mark_position_closed"):
            record_trade_ledger(Config(), pos, "AUSDT", "TAKE_PROFIT", 105.0, 5.0, 5.0)

        fake_append.assert_called_once()


if __name__ == "__main__":
    unittest.main()
