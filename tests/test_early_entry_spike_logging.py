"""[2026-08-15 사용자요청] "V2 이후 실제로 스파이크 조기체결이 적용된 거래가 어느 건지
알 수가 없다" — candidate["early_entry_spike"]가 execute_entry까지는 전달됐지만
TrackedPosition/TradeRecord에는 저장이 안 되던 관찰성 누락을 검증한다."""
import unittest

from bot.config import Config
from bot.position_manager import PositionManager


class EarlyEntrySpikeTrackedPositionTests(unittest.TestCase):
    def test_track_default_is_false(self):
        cfg = Config()
        pm = PositionManager(cfg)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)
        self.assertFalse(pm.positions["BTCUSDT"].early_entry_spike)

    def test_track_stores_true_when_passed(self):
        cfg = Config()
        pm = PositionManager(cfg)
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0, early_entry_spike=True)
        self.assertTrue(pm.positions["BTCUSDT"].early_entry_spike)


class EarlyEntrySpikeExecuteEntryWiringSourceTests(unittest.TestCase):
    """execute_entry()가 candidate의 early_entry_spike를 pm.track()에 실제로 넘기는지,
    청산 기록 생성부(TradeRecord)가 pos.early_entry_spike를 실어보내는지 소스 레벨로 확인
    (전체 실거래 흐름을 mock하기엔 exchange 의존이 너무 무거움 — 이 저장소 기존 관례)."""

    def test_execute_entry_passes_candidate_flag_to_track(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn('early_entry_spike=bool(candidate.get("early_entry_spike"))', src)

    def test_trade_record_carries_position_flag(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main)
        self.assertIn('early_entry_spike=getattr(pos, "early_entry_spike", False)', src)

    def test_scan_entry_candidate_logs_spike_tag_context(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.scan_entry_candidate)
        self.assertIn("early_entry_spike 태그 감지", src)
        self.assertIn("priority=%.2f", src)

    def test_execute_entry_logs_spike_attempt_context(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("early_entry_spike 후보 진입 시도", src)
        self.assertIn("aggressive_fill=%s", src)
        self.assertIn("fallback_allowed=%s", src)


class TradeRecordFieldTests(unittest.TestCase):
    def test_trade_record_has_early_entry_spike_field_default_false(self):
        from bot.trade_ledger import TradeRecord
        record = TradeRecord(
            symbol="BTCUSDT", side="LONG", origin="bot", entry_reason="PUMP_SIGNAL",
            exit_reason="TAKE_PROFIT", entry_price=100.0, exit_price=101.0, quantity=1.0,
            leverage=1.0, entered_at=0.0, exited_at=1.0, held_seconds=1.0,
            estimated_pnl_pct=1.0, estimated_pnl_usdt=0.1, bot_version="test", config_snapshot={},
        )
        self.assertFalse(record.early_entry_spike)


if __name__ == "__main__":
    unittest.main()
