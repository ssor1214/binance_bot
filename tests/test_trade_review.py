"""[2026-08-11 사용자요청] "30분마다 수익/손실 복기하고 텔레그램으로 보고" —
analyze_recent_trade_review()를 검증한다. 개선 제안은 changes로 반환만 하고 여기서
직접 적용하지 않는다(호출부가 tg.propose_tuning()으로 승인 절차를 거쳐야 함). 실 API
호출은 손실거래 표본(최대 5건)의 klines 조회만 모킹으로 대체. 실제 라이브 원장 파일은
절대 건드리지 않고 임시 파일만 사용한다."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import Config
from bot.main import analyze_recent_trade_review


def write_ledger(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def make_trade(symbol, side, pnl_usdt, entered_at, exited_at, origin="bot"):
    return {
        "symbol": symbol, "side": side, "origin": origin,
        "entry_price": 100.0, "exit_price": 100.0 + (1 if pnl_usdt > 0 else -1),
        "estimated_pnl_usdt": pnl_usdt, "entered_at": entered_at, "exited_at": exited_at,
    }


class TradeReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.ledger_path = str(Path(self._tmpdir.name) / "trade_ledger.jsonl")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_trades_in_window(self):
        write_ledger(self.ledger_path, [])
        cfg = Config()
        ex = MagicMock()
        diagnosis, changes = analyze_recent_trade_review(ex, cfg, 1800, ledger_path=self.ledger_path)
        self.assertIn("거래 없음", diagnosis)
        self.assertEqual(changes, {})

    def test_manual_trades_excluded(self):
        now = time.time()
        write_ledger(self.ledger_path, [make_trade("BTCUSDT", "LONG", 1.0, now - 60, now - 30, origin="manual")])
        cfg = Config()
        ex = MagicMock()
        diagnosis, changes = analyze_recent_trade_review(ex, cfg, 1800, ledger_path=self.ledger_path)
        self.assertIn("거래 없음", diagnosis)

    def test_summarizes_win_rate_and_pnl(self):
        now = time.time()
        rows = [
            make_trade("BTCUSDT", "LONG", 1.0, now - 100, now - 90),
            make_trade("ETHUSDT", "SHORT", -0.5, now - 80, now - 70),
        ]
        write_ledger(self.ledger_path, rows)
        cfg = Config()
        ex = MagicMock()
        ex.client.futures_klines.return_value = []  # 복기 API 호출은 빈 결과로 처리
        diagnosis, changes = analyze_recent_trade_review(ex, cfg, 1800, ledger_path=self.ledger_path)
        self.assertIn("2건", diagnosis)
        self.assertIn("50", diagnosis)  # 승률 50%

    def test_proposes_grace_widen_when_whipsaw_pattern_detected(self):
        """손실 5건 중 4건 이상이 청산후 회복되면(휩쏘성) 구체적 조정안을 changes로 반환해야 한다."""
        now = time.time()
        rows = [make_trade(f"SYM{i}USDT", "LONG", -0.3, now - 100 + i, now - 90 + i) for i in range(5)]
        write_ledger(self.ledger_path, rows)
        cfg = Config()
        cfg.stop_loss_grace_widen_mult = 1.5
        ex = MagicMock()
        # LONG 손실 청산 후 15분내 고가가 entry_price(100)를 넘음 -> 회복으로 판정
        ex.client.futures_klines.return_value = [
            [0, "99", "101", "98", "99.5"] for _ in range(5)
        ]
        diagnosis, changes = analyze_recent_trade_review(ex, cfg, 1800, ledger_path=self.ledger_path)
        self.assertIn("STOP_LOSS_GRACE_WIDEN_MULT", changes)
        self.assertGreater(changes["STOP_LOSS_GRACE_WIDEN_MULT"], 1.5)

    def test_no_proposal_when_no_recovery_pattern(self):
        """청산 후 회복이 없으면(정상 손절) changes가 비어있어야 한다."""
        now = time.time()
        rows = [make_trade(f"SYM{i}USDT", "LONG", -0.3, now - 100 + i, now - 90 + i) for i in range(5)]
        write_ledger(self.ledger_path, rows)
        cfg = Config()
        ex = MagicMock()
        # 청산 후에도 고가가 entry_price(100)를 못 넘음 -> 회복 아님
        ex.client.futures_klines.return_value = [
            [0, "95", "97", "94", "96"] for _ in range(5)
        ]
        diagnosis, changes = analyze_recent_trade_review(ex, cfg, 1800, ledger_path=self.ledger_path)
        self.assertEqual(changes, {})


if __name__ == "__main__":
    unittest.main()
