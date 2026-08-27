"""[2026-08-15 사용자요청] 손절 유예 게이트 — 손절선에 닿았더라도 회복 조짐(반대방향
detect_reversal)이 뚜렷하면 아주 짧게(기본 30초)만 유예했다가 확정한다.

백테스트 근거(SL 461건 실측 재현): 회복신호 없이 무조건 기다리는 건 여전히 나쁘고
(대조군 145건 60초 대기 평균 -1.03%p ROE), 회복신호 3/3표가 있는 241건만 30초 대기에서
개선 54.8%/악화 45.2%, 평균 +0.29%p. 안전캡 1.0%p는 최악값을 -26.4%p에서 -5.8%p로 줄이면서
업사이드는 거의 유지했다.

[중요] 봇에서만 유예하면 거래소 STOP_MARKET(트리거가가 evaluate의 손절선과 같은 레벨)이
먼저 체결돼 유예가 무의미해진다 — 그래서 유예 시작 시 거래소 주문도 안전캡만큼 넓힌다.
이 테스트는 그 배선까지 확인한다.
"""
import time
import unittest
from unittest.mock import MagicMock

from bot.config import Config, TUNABLE_PARAMS, TUNABLE_PARAMS_KO
from bot.position_manager import PositionManager


class SlDeferConfigTests(unittest.TestCase):
    def test_code_default_is_off_when_env_unset(self):
        """.env는 사용자 승인으로 켜져 있지만(라이브 현재 상태), 환경변수가 아예 없는 신규
        배포 환경에서의 dataclass 기본값은 꺼짐이어야 한다 — Config()로 .env를 그대로 읽는
        대신 소스의 리터럴 기본값을 확인한다(이 저장소 기존 관례)."""
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('sl_defer_enabled: bool = _bool("SL_DEFER_ENABLED", "false")', src)

    def test_env_enabled_after_backtest_validation(self):
        """[2026-08-15 23:0x] 백테스트(SL 461건) 검증 후 사용자 승인으로 라이브 활성화.
        끄면 이 단언을 반대로 갱신하고 그 이유를 여기 남길 것."""
        cfg = Config()
        self.assertTrue(cfg.sl_defer_enabled)
        self.assertEqual(cfg.sl_defer_sec, 30.0)
        self.assertEqual(cfg.sl_defer_min_votes, 3)
        self.assertEqual(cfg.sl_defer_extra_loss_cap_pct, 1.0)

    def test_exposed_to_telegram_whitelist(self):
        """사용자가 텔레그램에서 직접 켜고 조절할 수 있어야 한다."""
        for key in ("SL_DEFER_ENABLED", "SL_DEFER_SEC", "SL_DEFER_MIN_VOTES",
                    "SL_DEFER_EXTRA_LOSS_CAP_PCT"):
            self.assertIn(key, TUNABLE_PARAMS)
            self.assertIn(key, TUNABLE_PARAMS_KO)


class SlDeferPositionStateTests(unittest.TestCase):
    def test_tracked_position_defaults(self):
        pm = PositionManager(Config())
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)
        pos = pm.positions["BTCUSDT"]
        self.assertEqual(pos.sl_defer_until, 0.0)
        self.assertEqual(pos.sl_defer_start_roe, 0.0)
        self.assertFalse(pos.sl_defer_used)
        self.assertIsNone(pos.sl_defer_prev_stop_order_id)


def _pm_with_losing_long(cfg):
    """손절선에 막 닿은 LONG 포지션 하나를 가진 PositionManager를 만든다."""
    pm = PositionManager(cfg)
    pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)
    pm.positions["BTCUSDT"].stop_order_id = "old-stop"
    return pm


class SlDeferGateTests(unittest.TestCase):
    """maybe_defer_stop_loss()의 분기를 실제 함수로 검증한다.
    캔들 조회/지표/거래소는 이 저장소 기존 관례대로 목으로 대체한다."""

    def setUp(self):
        from bot import main as bot_main
        self.bot_main = bot_main
        self.cfg = Config()
        self.cfg.sl_defer_enabled = True
        self.cfg.stop_loss_pct = 6.0
        self.cfg.stop_loss_grace_sec = 0.0  # 진입직후 유예 확장은 이 테스트 범위 밖
        self._orig_detect = bot_main.detect_reversal
        self._orig_add = bot_main.add_indicators
        bot_main.add_indicators = lambda df, cfg: df

    def tearDown(self):
        self.bot_main.detect_reversal = self._orig_detect
        self.bot_main.add_indicators = self._orig_add

    def _ex(self):
        ex = MagicMock()
        ex.get_klines.return_value = object()
        ex.place_stop_market.return_value = {"algoId": "new-stop"}
        return ex

    def test_no_recovery_signal_means_no_defer(self):
        self.bot_main.detect_reversal = lambda *a, **k: False
        pm = _pm_with_losing_long(self.cfg)
        ex = self._ex()
        deferred = self.bot_main.maybe_defer_stop_loss(ex, pm, self.cfg, "BTCUSDT", 94.0)
        self.assertFalse(deferred)
        ex.place_stop_market.assert_not_called()

    def test_recovery_signal_defers_and_widens_exchange_stop(self):
        self.bot_main.detect_reversal = lambda *a, **k: True
        pm = _pm_with_losing_long(self.cfg)
        ex = self._ex()
        deferred = self.bot_main.maybe_defer_stop_loss(ex, pm, self.cfg, "BTCUSDT", 94.0)
        self.assertTrue(deferred)
        pos = pm.positions["BTCUSDT"]
        self.assertGreater(pos.sl_defer_until, time.time())
        self.assertTrue(pos.sl_defer_used)
        # [2026-08-16 수정] 안전캡은 "진입가 기준 base+cap"이 아니라 "지금 ROE - cap" 기준이어야
        # 한다. evaluate가 STOP_LOSS를 주는 시점은 정의상 이미 base보다 깊으므로, 진입가 기준으로
        # 계산하면 새 주문이 항상 현재가를 지나쳐 -2021로 거부된다(실거래 3전 3패로 확인).
        # mark 94.0, entry 100.0, lev 1.0 -> 현재 ROE -6.0%. target = -6.0 - 1.0 = -7.0%
        ex.place_stop_market.assert_called_once()
        widened_price = ex.place_stop_market.call_args[0][3]
        self.assertAlmostEqual(widened_price, 100.0 * (1 - 7.0 / 100), places=6)
        # 새 손절가는 반드시 현재가보다 아래(LONG)여야 즉시 체결이 안 난다
        self.assertLess(widened_price, 94.0)
        self.assertEqual(pos.stop_order_id, "new-stop")
        ex.cancel_order.assert_called_once_with("BTCUSDT", "old-stop")

    def test_exchange_widen_failure_aborts_defer(self):
        """거래소 확장에 실패하면 유예를 포기해야 한다 — 봇만 기다리면
        거래소 주문이 같은 레벨에서 체결돼 EXTERNAL_CLOSE_LOSS로 밀릴 뿐이다."""
        self.bot_main.detect_reversal = lambda *a, **k: True
        pm = _pm_with_losing_long(self.cfg)
        ex = self._ex()
        ex.place_stop_market.side_effect = RuntimeError("api down")
        deferred = self.bot_main.maybe_defer_stop_loss(ex, pm, self.cfg, "BTCUSDT", 94.0)
        self.assertFalse(deferred)
        self.assertEqual(pm.positions["BTCUSDT"].sl_defer_until, 0.0)

    def test_safety_cap_aborts_ongoing_defer(self):
        self.bot_main.detect_reversal = lambda *a, **k: True
        pm = _pm_with_losing_long(self.cfg)
        pos = pm.positions["BTCUSDT"]
        pos.sl_defer_until = time.time() + 60
        pos.sl_defer_start_roe = -6.0
        pos.sl_defer_used = True
        # 추가로 1.5%p 더 하락 → 캡(1.0%p) 초과 → 즉시 확정
        deferred = self.bot_main.maybe_defer_stop_loss(self._ex(), pm, self.cfg, "BTCUSDT", 92.5)
        self.assertFalse(deferred)
        self.assertEqual(pos.sl_defer_until, 0.0)

    def test_within_cap_and_time_keeps_waiting(self):
        pm = _pm_with_losing_long(self.cfg)
        pos = pm.positions["BTCUSDT"]
        pos.sl_defer_until = time.time() + 60
        pos.sl_defer_start_roe = -6.0
        pos.sl_defer_used = True
        deferred = self.bot_main.maybe_defer_stop_loss(self._ex(), pm, self.cfg, "BTCUSDT", 93.8)
        self.assertTrue(deferred)

    def test_expired_defer_confirms(self):
        pm = _pm_with_losing_long(self.cfg)
        pos = pm.positions["BTCUSDT"]
        pos.sl_defer_until = time.time() - 1  # 이미 만료
        pos.sl_defer_start_roe = -6.0
        pos.sl_defer_used = True
        deferred = self.bot_main.maybe_defer_stop_loss(self._ex(), pm, self.cfg, "BTCUSDT", 94.0)
        self.assertFalse(deferred)

    def test_only_one_defer_per_position(self):
        """무한정 미루기 방지 — 백테스트도 거래당 1회 유예만 시뮬레이션했다."""
        self.bot_main.detect_reversal = lambda *a, **k: True
        pm = _pm_with_losing_long(self.cfg)
        pos = pm.positions["BTCUSDT"]
        pos.sl_defer_used = True  # 이미 한 번 썼음
        deferred = self.bot_main.maybe_defer_stop_loss(self._ex(), pm, self.cfg, "BTCUSDT", 94.0)
        self.assertFalse(deferred)

    def test_klines_failure_falls_back_to_normal_stop_loss(self):
        self.bot_main.detect_reversal = lambda *a, **k: True
        pm = _pm_with_losing_long(self.cfg)
        ex = self._ex()
        ex.get_klines.side_effect = RuntimeError("network")
        deferred = self.bot_main.maybe_defer_stop_loss(ex, pm, self.cfg, "BTCUSDT", 94.0)
        self.assertFalse(deferred)


class SlDeferWiringSourceTests(unittest.TestCase):
    """evaluate()의 STOP_LOSS 반환 지점에 실제로 배선됐는지, 그리고 기본 꺼짐 플래그로
    가드되는지 소스 레벨로 확인(전체 실거래 흐름 mock은 exchange 의존이 과함 — 기존 관례)."""

    def test_gate_is_wired_behind_enabled_flag(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main)
        self.assertIn('if action == "STOP_LOSS" and cfg.sl_defer_enabled:', src)
        self.assertIn("maybe_defer_stop_loss(ex, pm, cfg, symbol, mark_price)", src)

    def test_recovery_path_restores_tight_stop(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main)
        self.assertIn("손절 유예 후 회복", src)


if __name__ == "__main__":
    unittest.main()


class DeferStopPriceNeverImmediatelyTriggersTests(unittest.TestCase):
    """[2026-08-16 실거래로 발견한 구조적 결함의 회귀 방지]
    유예용 손절가는 '지금 가격보다 불리한 쪽'에 놓여야 -2021("Order would immediately
    trigger")이 나지 않는다. 최초 구현은 진입가 기준 base_pct+cap으로 계산해서, evaluate가
    STOP_LOSS를 주는 시점(정의상 이미 base보다 깊음)에는 항상 현재가를 지나쳐 3전 3패했다."""

    def setUp(self):
        from bot import main as bot_main
        self.bot_main = bot_main
        self.cfg = Config()
        self.cfg.sl_defer_enabled = True

    def _placed_price(self, side, entry, mark, leverage):
        ex = MagicMock()
        ex.place_stop_market.return_value = {"algoId": "x"}
        pm = PositionManager(self.cfg)
        pm.track("BTCUSDT", side, entry_price=entry, quantity=1.0, leverage=leverage)
        pos = pm.positions["BTCUSDT"]
        from bot.strategy import pnl_pct
        roe = pnl_pct(entry, mark, side) * leverage
        ok = self.bot_main._widen_exchange_stop_for_defer(ex, self.cfg, pos, "BTCUSDT", roe)
        self.assertTrue(ok)
        return ex.place_stop_market.call_args[0][3]

    def test_long_stop_below_current_price(self):
        for entry, mark, lev in ((100.0, 94.0, 1.0), (100.0, 98.4, 6.0), (0.2, 0.1968, 6.0)):
            price = self._placed_price("LONG", entry, mark, lev)
            self.assertLess(price, mark, f"LONG 유예 손절가가 현재가 이상이면 즉시 체결된다 (entry={entry})")

    def test_short_stop_above_current_price(self):
        for entry, mark, lev in ((100.0, 106.0, 1.0), (100.0, 101.6, 6.0), (0.2, 0.2032, 6.0)):
            price = self._placed_price("SHORT", entry, mark, lev)
            self.assertGreater(price, mark, f"SHORT 유예 손절가가 현재가 이하면 즉시 체결된다 (entry={entry})")

    def test_gap_equals_safety_cap_in_roe_terms(self):
        """현재 ROE와 새 손절선의 간격이 정확히 안전캡이어야 한다."""
        from bot.strategy import pnl_pct
        entry, mark, lev = 100.0, 98.4, 6.0
        price = self._placed_price("LONG", entry, mark, lev)
        current_roe = pnl_pct(entry, mark, "LONG") * lev
        stop_roe = pnl_pct(entry, price, "LONG") * lev
        self.assertAlmostEqual(current_roe - stop_roe, self.cfg.sl_defer_extra_loss_cap_pct, places=6)
