"""reconcile_positions()의 손절 재타이트 가드가 심볼별 mark_price를 올바르게 쓰는지 검증.

[2026-08-17 실거래에서 발견] 이 파일의 기존 두 테스트는 **포지션을 항상 1개만** 두고 돌렸다.
그래서 "위 루프에서 새어 나온 마지막 심볼의 live가 쓰인다"는 실제 결함이 드러날 수 없었다
(포지션이 하나면 새어 나온 값이 우연히 정답과 같다). 실측 AIOUSDT 17:46~17:49:
  triggerPrice=0.07126475, markPrice=0.01941100
0.019411은 같은 시각 보유 중이던 USUSDT의 가격이었다. 그 결과 유예용으로 넓혀둔 손절폭이
유예 만료 후에도 계속 재타이트를 건너뛰어 포지션이 과소보호 상태로 남았다.
아래 MultiPositionTests가 그 조건(포지션 2개 이상)을 재현한다.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot.main import reconcile_positions
from bot.position_manager import PositionManager


def make_cfg() -> Config:
    c = Config()
    c.stop_loss_grace_sec = 45.0
    c.stop_loss_grace_widen_mult = 1.5
    c.stop_loss_grace_stage2_sec = 0.0
    c.stop_loss_pct = 6.0
    c.short_stop_loss_pct = 3.0
    return c


class ReconcileStopRetightenMarkPriceTests(unittest.TestCase):
    def test_retighten_uses_live_symbol_mark_price_not_stale_other_symbol(self):
        cfg = make_cfg()
        pm = PositionManager(cfg)
        pm.track("BICOUSDT", "SHORT", entry_price=0.0230, quantity=100.0, leverage=4.0, entered_at=1000.0)
        pos = pm.positions["BICOUSDT"]
        pos.stop_loss_widened = True
        pos.applied_stop_loss_pct = 4.5
        pos.stop_order_id = 111

        ex = MagicMock()
        ex.get_open_positions.return_value = [{
            "symbol": "BICOUSDT",
            "amount": -100.0,
            "entry_price": 0.0230,
            "mark_price": 0.0231,
            "side": "SHORT",
            "leverage": 4.0,
        }]
        ex.get_open_algo_orders.return_value = [{"algoId": 111}]
        ex.place_stop_market.return_value = {"algoId": 222}

        tg = SimpleNamespace(send=MagicMock(), notify_error=MagicMock())

        with patch("bot.main.time.time", return_value=1000.0 + 50):
            reconcile_positions(ex, pm, cfg, tg)

        ex.place_stop_market.assert_called_once()
        self.assertEqual(pos.stop_order_id, 222)
        self.assertEqual(pos.applied_stop_loss_pct, 3.0)
        ex.cancel_order.assert_called_once_with("BICOUSDT", 111)
        ex.get_mark_price.assert_not_called()

    def test_retighten_falls_back_to_mark_price_lookup_when_live_mark_missing(self):
        cfg = make_cfg()
        pm = PositionManager(cfg)
        pm.track("ABCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=4.0, entered_at=1000.0)
        pos = pm.positions["ABCUSDT"]
        pos.stop_loss_widened = True
        pos.applied_stop_loss_pct = 9.0
        pos.stop_order_id = 333

        ex = MagicMock()
        ex.get_open_positions.return_value = [{
            "symbol": "ABCUSDT",
            "amount": 1.0,
            "entry_price": 100.0,
            "side": "LONG",
            "leverage": 4.0,
        }]
        ex.get_open_algo_orders.return_value = [{"algoId": 333}]
        ex.get_mark_price.return_value = 101.0
        ex.place_stop_market.return_value = {"algoId": 444}

        tg = SimpleNamespace(send=MagicMock(), notify_error=MagicMock())

        with patch("bot.main.time.time", return_value=1000.0 + 50):
            reconcile_positions(ex, pm, cfg, tg)

        ex.get_mark_price.assert_called_once_with("ABCUSDT")
        ex.place_stop_market.assert_called_once()
        self.assertEqual(pos.stop_order_id, 444)


class MultiPositionMarkPriceLeakTests(unittest.TestCase):
    """포지션이 2개 이상일 때 각 심볼이 자기 mark_price로 판정되는지."""

    def _run(self, ex, pm, cfg, tg, now):
        with patch("bot.main.time.time", return_value=now):
            reconcile_positions(ex, pm, cfg, tg)

    def _incident_setup(self, cfg, aio_live_extra: dict):
        """2026-08-17 17:43~17:50 실사고 재현 배치.

        AIOUSDT LONG entry=0.07235 (재타이트 목표가 0.07235*(1-6/100/4)=0.07126),
        USUSDT는 가격대가 3.7배 낮고(0.019305) live_positions의 **마지막** 항목이다.
        새어 나온 live를 쓰면 LONG 판정에 mark=0.019305가 들어가 mark<=stop이 되어
        "즉시발동 위험"으로 오판하고 재타이트를 건너뛴다.
        """
        pm = PositionManager(cfg)
        pm.track("AIOUSDT", "LONG", entry_price=0.07235, quantity=165.0, leverage=4.0, entered_at=1000.0)
        pm.track("USUSDT", "SHORT", entry_price=0.019305, quantity=622.0, leverage=4.0, entered_at=1000.0)
        aio = pm.positions["AIOUSDT"]
        aio.stop_loss_widened = True
        aio.applied_stop_loss_pct = 9.0  # 유예 중 넓혀둔 폭 -> 만료 후 6.0으로 좁혀야 한다
        aio.stop_order_id = 111
        pm.positions["USUSDT"].stop_order_id = 222

        ex = MagicMock()
        aio_live = {"symbol": "AIOUSDT", "amount": 165.0, "entry_price": 0.07235,
                    "side": "LONG", "leverage": 4.0}
        aio_live.update(aio_live_extra)
        ex.get_open_positions.return_value = [
            aio_live,
            # 마지막 항목 — 예전 코드에서 새어 나오던 값이 바로 이것이다.
            {"symbol": "USUSDT", "amount": -622.0, "entry_price": 0.019305,
             "mark_price": 0.019305, "side": "SHORT", "leverage": 4.0},
        ]
        ex.get_open_algo_orders.return_value = [{"algoId": 111}, {"algoId": 222}]
        ex.place_stop_market.return_value = {"algoId": 999}
        return pm, aio, ex

    def test_retighten_not_skipped_by_other_symbols_mark_price(self):
        cfg = make_cfg()
        pm, aio, ex = self._incident_setup(cfg, {"mark_price": 0.07240})
        tg = SimpleNamespace(send=MagicMock(), notify_error=MagicMock())

        self._run(ex, pm, cfg, tg, 1000.0 + 50)

        ex.place_stop_market.assert_called_once()
        self.assertEqual(ex.place_stop_market.call_args[0][0], "AIOUSDT")
        self.assertEqual(aio.stop_order_id, 999)
        self.assertEqual(aio.applied_stop_loss_pct, 6.0,
                         "유예 만료 후 손절폭이 원래 값으로 좁혀져야 한다")
        ex.cancel_order.assert_any_call("AIOUSDT", 111)

    def test_falls_back_to_own_symbol_lookup_not_other_symbols_live(self):
        """자기 live에 mark_price가 없으면 자기 심볼로 조회해야 한다 —
        다른 심볼의 live 값을 빌려 쓰면 안 된다."""
        cfg = make_cfg()
        pm, aio, ex = self._incident_setup(cfg, {})  # mark_price 없음
        ex.get_mark_price.return_value = 0.07240
        tg = SimpleNamespace(send=MagicMock(), notify_error=MagicMock())

        self._run(ex, pm, cfg, tg, 1000.0 + 50)

        ex.get_mark_price.assert_any_call("AIOUSDT")
        ex.place_stop_market.assert_called_once()
        self.assertEqual(aio.stop_order_id, 999)


if __name__ == "__main__":
    unittest.main()
