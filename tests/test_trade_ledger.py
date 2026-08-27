"""[2026-08-09] 거래 원장(trade_ledger) 단위테스트. 실제 로그 경로(DEFAULT_LEDGER_PATH)는
절대 건드리지 않고, 매 테스트마다 임시 디렉터리의 파일 경로만 사용한다."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.trade_ledger import (
    DEFAULT_LEDGER_PATH,
    TradeRecord,
    append_trade_record,
    find_open_bot_position_opened_at,
    infer_open_position_origin,
    load_open_bot_positions,
    load_trade_records,
    mark_bot_position_open,
    mark_position_closed,
    strategy_config_snapshot,
)


def make_record(**overrides):
    base = dict(
        symbol="TESTUSDT", side="LONG", origin="bot", entry_reason="PUMP_SIGNAL", exit_reason="TAKE_PROFIT",
        entry_price=1.0, exit_price=1.05, quantity=10.0, leverage=4.0,
        entered_at=1000.0, exited_at=1060.0, held_seconds=60.0,
        estimated_pnl_pct=5.0, estimated_pnl_usdt=2.0,
        bot_version="test-1", config_snapshot={"stop_loss_pct": 3.5},
    )
    base.update(overrides)
    return TradeRecord(**base)


class TradeLedgerTests(unittest.TestCase):
    def test_test_process_refuses_default_live_ledger_path(self):
        before = DEFAULT_LEDGER_PATH.read_text(encoding="utf-8") if DEFAULT_LEDGER_PATH.exists() else None

        append_trade_record(make_record(symbol="AUSDT"))

        after = DEFAULT_LEDGER_PATH.read_text(encoding="utf-8") if DEFAULT_LEDGER_PATH.exists() else None
        self.assertEqual(after, before)

    def test_append_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            append_trade_record(
                make_record(protection_state="STOP_ONLY", applied_stop_loss_pct=6.0, sl_defer_active=True),
                path=path,
            )
            records = load_trade_records(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symbol"], "TESTUSDT")
            self.assertEqual(records[0]["exit_reason"], "TAKE_PROFIT")
            self.assertEqual(records[0]["protection_state"], "STOP_ONLY")
            self.assertEqual(records[0]["applied_stop_loss_pct"], 6.0)
            self.assertTrue(records[0]["sl_defer_active"])

    def test_multiple_records_append_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            append_trade_record(make_record(symbol="AUSDT"), path=path)
            append_trade_record(make_record(symbol="BUSDT"), path=path)
            records = load_trade_records(path)
            self.assertEqual([r["symbol"] for r in records], ["AUSDT", "BUSDT"])

    def test_optional_fill_fields_default_to_none(self):
        rec = make_record()
        self.assertIsNone(rec.actual_fill_entry_price)
        self.assertIsNone(rec.commission_usdt)
        self.assertIsNone(rec.slippage_pct)
        self.assertIsNone(rec.protection_state)
        self.assertEqual(rec.applied_stop_loss_pct, 0.0)
        self.assertFalse(rec.sl_defer_active)

    def test_load_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.jsonl"
            self.assertEqual(load_trade_records(path), [])

    def test_load_skips_corrupted_lines_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text('{"symbol": "OKUSDT"}\nnot valid json\n{"symbol": "ALSOOK"}\n', encoding="utf-8")
            records = load_trade_records(path)
            self.assertEqual(len(records), 2)

    def test_write_failure_does_not_raise(self):
        """디렉터리를 만들 수 없는 경로(파일을 디렉터리처럼 쓰려는 경우)에서도 예외가
        거래 로직까지 전파되면 안 된다 — 원장 기록은 부가기능이라 실패해도 조용히 넘어간다."""
        with tempfile.TemporaryDirectory() as tmp:
            blocking_file = Path(tmp) / "not_a_dir"
            blocking_file.write_text("x", encoding="utf-8")
            bad_path = blocking_file / "ledger.jsonl"  # not_a_dir는 파일이라 하위경로 생성 불가
            try:
                append_trade_record(make_record(), path=bad_path)
            except Exception as e:
                self.fail(f"append_trade_record이 예외를 던지면 안 됨: {e}")


class StrategyConfigSnapshotTests(unittest.TestCase):
    def test_snapshot_includes_expected_keys_and_excludes_secrets(self):
        class FakeCfg:
            stop_loss_pct = 3.5
            take_profit_min = 4.0
            short_take_profit_min = 4.0
            take_profit_hard_cap = 20.0
            trail_drawdown_pct = 1.5
            leverage_min = 4
            leverage_max = 4
            pump_min_candle_chg_pct = 0.8
            pump_min_volume_ratio = 2.3
            min_entry_probability = 0.63
            short_min_entry_probability = 0.68
            mtf_min_agree_ratio = 0.5
            average_down_enabled = False
            fee_rate_roundtrip = 0.001
            api_key = "SHOULD-NOT-APPEAR"
            api_secret = "SHOULD-NOT-APPEAR"

        snap = strategy_config_snapshot(FakeCfg())
        self.assertEqual(snap["stop_loss_pct"], 3.5)
        self.assertEqual(snap["average_down_enabled"], False)
        self.assertNotIn("api_key", snap)
        self.assertNotIn("api_secret", snap)
        self.assertNotIn("SHOULD-NOT-APPEAR", str(snap))


class OpenBotPositionStateTests(unittest.TestCase):
    def test_mark_open_and_infer_bot_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 10.0, 3.0, 4.0, path=path)

            self.assertEqual(load_open_bot_positions(path)["ABCUSDT"]["side"], "LONG")
            self.assertEqual(infer_open_position_origin("ABCUSDT", "LONG", 10.0, 3.0, path=path), "bot")

    def test_unmatched_position_is_manual_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 10.0, 3.0, 4.0, path=path)

            self.assertEqual(infer_open_position_origin("ABCUSDT", "SHORT", 10.0, 3.0, path=path), "manual")
            self.assertEqual(infer_open_position_origin("XYZUSDT", "LONG", 10.0, 3.0, path=path), "manual")
            self.assertEqual(infer_open_position_origin("ABCUSDT", "LONG", 10.5, 3.0, path=path), "manual")

    def test_mark_closed_removes_open_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 10.0, 3.0, 4.0, path=path)
            mark_position_closed("ABCUSDT", path=path)

            self.assertEqual(load_open_bot_positions(path), {})
            self.assertEqual(infer_open_position_origin("ABCUSDT", "LONG", 10.0, 3.0, path=path), "manual")


class FindOpenBotPositionOpenedAtTests(unittest.TestCase):
    """[2026-08-14 실측 사고] 재시작 시 entered_at이 '지금'으로 리셋되어 진입 유예기간
    (180초) 동안 넓혀둔 손절폭이 영원히 안 좁혀지던 버그(APRUSDT -20.1% ROE 손절 실사고)
    재발방지 — 재시작 후에도 실제 최초 진입시각을 복원할 수 있는지 검증한다."""

    def test_matched_position_returns_original_opened_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 10.0, 3.0, 4.0, path=path)
            saved_opened_at = load_open_bot_positions(path)["ABCUSDT"]["opened_at"]

            result = find_open_bot_position_opened_at("ABCUSDT", "LONG", 10.0, 3.0, path=path)
        self.assertEqual(result, saved_opened_at)

    def test_unmatched_symbol_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 10.0, 3.0, 4.0, path=path)

            self.assertIsNone(find_open_bot_position_opened_at("XYZUSDT", "LONG", 10.0, 3.0, path=path))

    def test_mismatched_side_or_price_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open.json"
            mark_bot_position_open("ABCUSDT", "LONG", 10.0, 3.0, 4.0, path=path)

            self.assertIsNone(find_open_bot_position_opened_at("ABCUSDT", "SHORT", 10.0, 3.0, path=path))
            self.assertIsNone(find_open_bot_position_opened_at("ABCUSDT", "LONG", 10.5, 3.0, path=path))

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no_such_file.json"
            self.assertIsNone(find_open_bot_position_opened_at("ABCUSDT", "LONG", 10.0, 3.0, path=path))


if __name__ == "__main__":
    unittest.main()
