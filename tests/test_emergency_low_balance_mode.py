"""[2026-08-16 사용자요청] 비상 2배 레버리지 보호장치를 도입했다가, 같은 날 실거래에서
정상 운용을 방해하는 것으로 판단돼 .env에서는 다시 비활성화했다.

총자산이 기준 이하로 떨어지면 레버리지를 강제 축소한다. 증거금 하한(TINY_BALANCE_TIER
계단식)은 그대로 유지되므로, 레버리지만 낮추면 같은 증거금에 노셔널이 줄어 가격변동
1%당 손실이 그만큼 작아진다(6배→2배면 1/3).

"격리(ISOLATED)" 쪽은 별도 설정이 필요 없다 — CROSS_MARGIN_MIN_BALANCE_USDT=300 가드가
이미 강제하고 있고, 격리 전환에 실패하면 진입 자체가 취소된다.
"""
import unittest

from bot.config import Config


class EmergencyConfigTests(unittest.TestCase):
    def test_env_values_match_current_live_setting(self):
        cfg = Config()
        self.assertEqual(cfg.emergency_balance_usdt, 0.0)
        self.assertEqual(cfg.emergency_leverage, 2)

    def test_code_default_is_disabled(self):
        """환경변수가 없는 신규 배포 환경에서는 비활성화(0)가 기본이어야 한다."""
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('emergency_balance_usdt: float = _float("EMERGENCY_BALANCE_USDT", 0.0)', src)
        self.assertIn('emergency_leverage: int = _int("EMERGENCY_LEVERAGE", 2)', src)

    def test_isolated_is_already_guaranteed_by_cross_guard(self):
        """비상 배율을 꺼도, 크로스 차단 가드는 별개로 유지된다."""
        cfg = Config()
        self.assertGreater(cfg.cross_margin_min_balance_usdt, 0.0)


class EmergencyWiringSourceTests(unittest.TestCase):
    """execute_entry에 실제로 배선됐는지 소스 레벨 확인(이 저장소 기존 관례)."""

    def test_leverage_forced_down_only_when_balance_is_below_threshold(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("if cfg.emergency_balance_usdt > 0 and total_balance < cfg.emergency_balance_usdt:", src)
        self.assertIn("leverage = cfg.emergency_leverage", src)

    def test_uses_total_balance_not_available_balance(self):
        """'총자산' 기준이어야 한다 — 가용잔고는 포지션 증거금이 묶이면 크게 낮아져
        비상모드가 엉뚱하게 조기 발동한다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("total_balance < cfg.emergency_balance_usdt", src)
        self.assertNotIn("balance < cfg.emergency_balance_usdt", src.replace("total_balance < cfg.emergency_balance_usdt", ""))

    def test_only_lowers_never_raises(self):
        """이미 비상 배율보다 낮으면 굳이 올리지 않는다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("if leverage > cfg.emergency_leverage:", src)

    def test_logs_critical_on_activation(self):
        """사용자가 자는 동안 발동해도 로그로 남아야 한다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.execute_entry)
        self.assertIn("비상모드", src)


class EmergencySizingSanityTests(unittest.TestCase):
    """비상모드에서 주문이 거래소 최소 노셔널(약 5 USDT)을 넘는지 — 넘지 못하면
    진입이 계속 실패해 사실상 매매가 멈춘다."""

    def test_min_notional_still_satisfied_at_previous_threshold(self):
        from bot.main import compute_min_margin
        cfg = Config()
        balance = 10.0
        min_margin = compute_min_margin(balance, cfg)
        notional = min_margin * cfg.emergency_leverage
        self.assertGreaterEqual(
            notional, 5.0,
            f"비상모드 노셔널 {notional} USDT가 거래소 최소치(약 5)에 못 미쳐 진입이 계속 실패한다",
        )

    def test_env_can_disable_emergency_mode_without_code_change(self):
        cfg = Config()
        self.assertEqual(cfg.emergency_balance_usdt, 0.0)


if __name__ == "__main__":
    unittest.main()
