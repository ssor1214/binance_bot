"""[2026-08-25] 순방향 분할 — 총 크기 고정, 초반에 유리할 때만 나머지를 채운다.

물타기(지는 포지션에 추가)와 정반대다. 핵심은 "이익 확대"가 아니라 "손실 축소"다.
원장 535건 시뮬레이션: 전량 1회 진입 건당 -0.5727%p -> 1차 40% 분할 건당 -0.2967%p.
총 명목이 같으므로 수수료가 안 늘고, 진입 횟수도 그대로라 거래수도 안 준다(원칙 1 무관).
"""
import time
import unittest

import pandas as pd
from unittest.mock import MagicMock, patch

from bot.config import Config
from bot.main import try_forward_scale_in
from bot.position_manager import PositionManager


def _cfg(**kw):
    cfg = Config()
    cfg.forward_scale_in_enabled = True
    cfg.fixed_entry_margin_usdt = 11.0
    cfg.forward_scale_in_first_ratio = 0.4
    cfg.forward_scale_in_check_sec = 60.0
    cfg.forward_scale_in_max_sec = 150.0
    cfg.forward_scale_in_min_roe = 0.0
    # 기본은 RSI 게이트 없이 분할 로직만 검증한다. 게이트는 ScaleInRsiGateTests가 따로 켠다.
    cfg.forward_scale_in_require_rsi = False
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _pm(cfg, held_sec=70.0, entry=100.0, side="LONG"):
    pm = PositionManager(cfg)
    pm.track("TESTUSDT", side, entry, 10.0, leverage=4.0, origin="bot", scale_in_done=False)
    pm.positions["TESTUSDT"].entered_at = time.time() - held_sec
    return pm


class ForwardScaleInTests(unittest.TestCase):
    def _run(self, cfg, pm, mark_price):
        ex = MagicMock()
        ex.get_mark_price.return_value = mark_price
        with patch("bot.main.try_average_down") as add:
            try_forward_scale_in(ex, pm, cfg, "TESTUSDT", MagicMock())
        return add

    def test_adds_remaining_when_favorable(self):
        """초반에 유리하면 나머지 60%(=6.6 USDT)를 채운다."""
        cfg = _cfg()
        add = self._run(cfg, _pm(cfg), 101.0)   # LONG +1% 가격 -> ROE +4%
        add.assert_called_once()
        self.assertAlmostEqual(add.call_args.kwargs["forced_margin_usdt"], 6.6, places=6)

    def test_does_not_add_when_unfavorable(self):
        """초반에 불리하면 추가하지 않는다 — 1차 40% 크기로만 물린다. 이게 손실 축소의 핵심."""
        cfg = _cfg()
        add = self._run(cfg, _pm(cfg), 99.0)
        add.assert_not_called()
        self.assertFalse(pmpos_done(_pm(cfg)))

    def test_too_early_does_nothing(self):
        cfg = _cfg()
        pm = _pm(cfg, held_sec=30.0)
        add = self._run(cfg, pm, 101.0)
        add.assert_not_called()
        self.assertFalse(pm.positions["TESTUSDT"].scale_in_done)

    def test_window_missed_marks_done_without_adding(self):
        """창을 놓치면 추가하지 않고 1차 크기로 끝까지 간다."""
        cfg = _cfg()
        pm = _pm(cfg, held_sec=200.0)
        add = self._run(cfg, pm, 101.0)
        add.assert_not_called()
        self.assertTrue(pm.positions["TESTUSDT"].scale_in_done)

    def test_only_once(self):
        """한 번 시도하면 다시 안 한다 — 반복 주문으로 수수료만 새는 걸 막는다."""
        cfg = _cfg()
        pm = _pm(cfg)
        self._run(cfg, pm, 101.0)
        self.assertTrue(pm.positions["TESTUSDT"].scale_in_done)
        add2 = self._run(cfg, pm, 101.0)
        add2.assert_not_called()

    def test_short_side_uses_correct_direction(self):
        cfg = _cfg()
        pm = _pm(cfg, side="SHORT")
        add = self._run(cfg, pm, 99.0)      # SHORT은 가격 하락이 유리
        add.assert_called_once()

    def test_disabled_is_noop(self):
        cfg = _cfg(forward_scale_in_enabled=False)
        add = self._run(cfg, _pm(cfg), 101.0)
        add.assert_not_called()

    def test_requires_fixed_margin(self):
        """총 크기가 고정돼 있지 않으면 '나머지'를 정의할 수 없으므로 아무 것도 안 한다."""
        cfg = _cfg(fixed_entry_margin_usdt=0.0)
        add = self._run(cfg, _pm(cfg), 101.0)
        add.assert_not_called()

    def test_manual_position_is_skipped(self):
        cfg = _cfg()
        pm = _pm(cfg)
        pm.positions["TESTUSDT"].origin = "manual"
        add = self._run(cfg, pm, 101.0)
        add.assert_not_called()

    def test_code_default_is_off(self):
        import inspect
        self.assertIn(
            'forward_scale_in_enabled: bool = _bool("FORWARD_SCALE_IN_ENABLED", "false")',
            inspect.getsource(Config),
        )


def pmpos_done(pm):
    return pm.positions["TESTUSDT"].scale_in_done


if __name__ == "__main__":
    unittest.main()


class NoPyramidingGuardTests(unittest.TestCase):
    """[2026-08-25] 불타기 방지 — 총 노출이 계획(FIXED_ENTRY_MARGIN_USDT)을 절대 못 넘는다.

    1차 분할이 실제로 적용되지 않은 포지션(전량 진입 폴백, 재시작 복원 등)에 2차가 붙으면
    총 노출이 11 -> 17.6으로 불어난다. 그건 순방향 분할이 아니라 불타기다.
    """

    def test_track_defaults_to_blocking_scale_in(self):
        """pm.track의 기본값은 '2차 없음'이다 — 명시적으로 분할을 적용한 진입만 2차를 받는다."""
        cfg = _cfg()
        pm = PositionManager(cfg)
        pm.track("TESTUSDT", "LONG", 100.0, 10.0, leverage=4.0, origin="bot")
        self.assertTrue(pm.positions["TESTUSDT"].scale_in_done)

    def test_split_applied_position_allows_scale_in(self):
        cfg = _cfg()
        pm = PositionManager(cfg)
        pm.track("TESTUSDT", "LONG", 100.0, 10.0, leverage=4.0, origin="bot", scale_in_done=False)
        self.assertFalse(pm.positions["TESTUSDT"].scale_in_done)

    def test_fallback_entry_gets_no_second_tranche(self):
        """전량 폴백 진입에는 2차가 붙지 않아야 한다(불타기 차단)."""
        cfg = _cfg()
        pm = PositionManager(cfg)
        pm.track("TESTUSDT", "LONG", 100.0, 10.0, leverage=4.0, origin="bot", scale_in_done=True)
        pm.positions["TESTUSDT"].entered_at = time.time() - 70.0
        ex = MagicMock()
        ex.get_mark_price.return_value = 101.0
        with patch("bot.main.try_average_down") as add:
            try_forward_scale_in(ex, pm, cfg, "TESTUSDT", MagicMock())
        add.assert_not_called()

    def test_total_exposure_never_exceeds_plan(self):
        """1차 + 2차 = 계획 총액. 2차가 나머지만 채우는지 산술로 고정한다."""
        cfg = _cfg()
        first = cfg.fixed_entry_margin_usdt * cfg.forward_scale_in_first_ratio
        pm = _pm(cfg)
        ex = MagicMock()
        ex.get_mark_price.return_value = 101.0
        with patch("bot.main.try_average_down") as add:
            try_forward_scale_in(ex, pm, cfg, "TESTUSDT", MagicMock())
        second = add.call_args.kwargs["forced_margin_usdt"]
        self.assertAlmostEqual(first + second, cfg.fixed_entry_margin_usdt, places=6)


class ScaleInStatePreservationTests(unittest.TestCase):
    """[2026-08-25] 2차 체결 시 관측값/무장 상태를 보존한다.

    물타기용 apply_average_down은 armed/peak_pnl/max_favorable_roe/roe_at_*를 전부
    리셋한다. 순방향 분할에 그대로 쓰면 UNARMED_MID_HOLD_CUT 발동 확률이 오르고,
    트레일링 익절이 늦어지고, 원장 판정용 관측값이 사라진다.
    """

    def _pos(self):
        cfg = _cfg()
        pm = PositionManager(cfg)
        pm.track("TESTUSDT", "LONG", 100.0, 10.0, leverage=4.0, origin="bot", scale_in_done=False)
        p = pm.positions["TESTUSDT"]
        p.armed = True
        p.peak_pnl = 5.0
        p.max_favorable_roe = 5.0
        p.roe_at_30s, p.roe_at_60s = 1.0, 2.0
        return pm, p

    def test_preserves_armed_and_observations(self):
        pm, p = self._pos()
        pm.apply_scale_in("TESTUSDT", 100.5, 25.0, added_margin_usdt=16.5, current_roe=1.8)
        self.assertTrue(p.armed)
        self.assertEqual(p.max_favorable_roe, 5.0)
        self.assertEqual(p.roe_at_30s, 1.0)
        self.assertEqual(p.roe_at_60s, 2.0)

    def test_resets_only_trailing_reference(self):
        """peak_pnl만 새 평단 기준으로 다시 잡는다 — 안 그러면 트레일링이 즉시 헛발동한다."""
        pm, p = self._pos()
        pm.apply_scale_in("TESTUSDT", 100.5, 25.0, added_margin_usdt=16.5, current_roe=1.8)
        self.assertAlmostEqual(p.peak_pnl, 1.8, places=6)

    def test_updates_price_and_quantity(self):
        pm, p = self._pos()
        pm.apply_scale_in("TESTUSDT", 100.5, 25.0, added_margin_usdt=16.5, current_roe=1.8)
        self.assertAlmostEqual(p.entry_price, 100.5, places=6)
        self.assertAlmostEqual(p.quantity, 25.0, places=6)

    def test_average_down_still_resets(self):
        """물타기 경로는 기존대로 리셋해야 한다(평단이 나빠지는 쪽이라 기준점이 무의미)."""
        pm, p = self._pos()
        pm.apply_average_down("TESTUSDT", 99.0, 25.0, added_margin_usdt=16.5)
        self.assertFalse(p.armed)
        self.assertEqual(p.peak_pnl, 0.0)
        self.assertEqual(p.max_favorable_roe, 0.0)


class ScaleInRsiGateTests(unittest.TestCase):
    """[2026-08-25] 2차 진입은 RSI 극단배제를 통과해야 한다.

    3분봉 캐시 2차 후보 1,120건 재측정(수수료 차감 후): 조건없음 -0.465% / EMA만 -0.479% /
    볼밴만 -0.745% / EMA+볼밴 -0.751% / 볼밴+RSI -0.072% / 셋전부 -0.090% /
    RSI 방향일치 +0.110% / **RSI 극단배제만 +0.113%**.
    수수료를 넘는 건 RSI 계열뿐이고, 볼밴은 어디에 붙여도 0.18~0.28%p씩 깎였다.
    """

    def _df(self, rsi):
        return pd.DataFrame([{"rsi": rsi}, {"rsi": rsi}])

    def test_long_in_overbought_is_rejected(self):
        from bot.main import rsi_extreme_ok
        self.assertFalse(rsi_extreme_ok(self._df(72.0), _cfg(), "LONG"))

    def test_long_below_overbought_is_allowed(self):
        from bot.main import rsi_extreme_ok
        self.assertTrue(rsi_extreme_ok(self._df(68.0), _cfg(), "LONG"))

    def test_short_in_oversold_is_rejected(self):
        from bot.main import rsi_extreme_ok
        self.assertFalse(rsi_extreme_ok(self._df(28.0), _cfg(), "SHORT"))

    def test_short_above_oversold_is_allowed(self):
        from bot.main import rsi_extreme_ok
        self.assertTrue(rsi_extreme_ok(self._df(32.0), _cfg(), "SHORT"))

    def test_slope_is_ignored(self):
        """기울기는 안 본다 — 실측상 기울기만 쓰면 98.4%가 통과해 효과가 없었다."""
        from bot.main import rsi_extreme_ok
        rising = pd.DataFrame([{"rsi": 50.0}, {"rsi": 55.0}])
        falling = pd.DataFrame([{"rsi": 55.0}, {"rsi": 50.0}])
        self.assertTrue(rsi_extreme_ok(rising, _cfg(), "LONG"))
        self.assertTrue(rsi_extreme_ok(falling, _cfg(), "LONG"))

    def test_missing_data_returns_none_and_does_not_block(self):
        """정보가 없으면 None — 호출부는 False일 때만 막는다(정보 없음으로 거래를 잃지 않는다)."""
        from bot.main import rsi_extreme_ok
        self.assertIsNone(rsi_extreme_ok(pd.DataFrame(), _cfg(), "LONG"))

    def test_rejection_does_not_close_the_window(self):
        """RSI로 보류돼도 scale_in_done을 세우면 안 된다 — 창 안에서 재시도해야 한다."""
        cfg = _cfg()
        cfg.forward_scale_in_require_rsi = True
        pm = _pm(cfg)
        ex = MagicMock()
        ex.get_mark_price.return_value = 101.0
        ex.get_klines.return_value = pd.DataFrame()
        with patch("bot.main.add_indicators", return_value=pd.DataFrame([{"rsi": 80.0}, {"rsi": 80.0}])), \
             patch("bot.main.try_average_down") as add:
            try_forward_scale_in(ex, pm, cfg, "TESTUSDT", MagicMock())
        add.assert_not_called()
        self.assertFalse(pm.positions["TESTUSDT"].scale_in_done)

    def test_code_default_is_off(self):
        import inspect
        self.assertIn(
            'forward_scale_in_require_rsi: bool = _bool("FORWARD_SCALE_IN_REQUIRE_RSI", "false")',
            inspect.getsource(Config),
        )
