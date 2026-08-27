"""[2026-08-16 실거래 발견 + 사용자승인] 진입유예 만료 시 손절폭 "중간 계단".

발견: 원복 이후 STOP_LOSS 34건 중 15건(44.1%)이 보유 170~200초에 집중됐다(유예 180초 직후).
보유시간 중앙값 193초, STOP_LOSS 손실합계 -21.71 USDT로 전체 적자(-24.54)의 대부분.
원인은 유예 만료 순간 손절폭이 2.5배 → 1.0배로 한 번에 스냅하면서, 그 사이 구간
(ROE -6% ~ -15%)에 있던 포지션이 일제히 즉시 청산되는 구조.

단순 250초 연장안은 15건 재현검증에서 개선7/악화6/변화없음2, 평균 +0.57%p이나 중앙값
-0.13%p로 결론이 나지 않았다(연장 구간에 넓은 손절선까지 밀려 더 크게 잃은 사례 3건).
그래서 "연장"이 아니라 "중간 계단"을 도입했다.
"""
import time
import unittest

from bot.config import Config
from bot.position_manager import PositionManager, grace_stop_multiplier


def cfg_stage2(sec=250.0, mult=1.8) -> Config:
    """중간 계단이 켜진 설정. 라이브 .env는 롤백돼 꺼져 있으므로(아래 참고),
    메커니즘 검증은 이 명시적 픽스처로 한다."""
    c = Config()
    c.stop_loss_grace_sec = 180.0
    c.stop_loss_grace_widen_mult = 2.5
    c.stop_loss_grace_stage2_sec = sec
    c.stop_loss_grace_stage2_mult = mult
    return c


class GraceStageConfigTests(unittest.TestCase):
    def test_env_rolled_back_to_two_stage(self):
        """[2026-08-16 01:18 도입 → 06:xx 사용자지시로 롤백]
        약 5시간 실측 비교에서 의도한 개선이 나타나지 않고 오히려 악화했다:
          도입 전 STOP_LOSS 34건/9.8%, 평균 -0.638U, 거래당 손익 -0.0715U
          도입 후 STOP_LOSS  8건/14.0%, 평균 -0.795U, 거래당 손익 -0.1207U
        보유시간도 8건 중 4건이 250~265초에 몰려, 원래 180초 클러스터(44%)가 250초로 이동만
        했다 — 임계값에 계단이 있는 한 경계효과는 원리상 남는다는 것이 실측으로 확인됐다.
        (표본 8건 vs 34건으로 얇고 횡보장 악재가 겹쳐 있어 완전한 분리는 아님.)
        코드는 보존돼 있으므로 값만 되돌리면 재활성화된다. 다시 켤 땐 이 단언을 갱신할 것."""
        self.assertEqual(Config().stop_loss_grace_stage2_sec, 0.0)

    def test_code_default_is_disabled(self):
        """환경변수가 없으면 stage2는 꺼진 상태(0)여서 기존 2단계 동작 그대로여야 한다."""
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('stop_loss_grace_stage2_sec: float = _float("STOP_LOSS_GRACE_STAGE2_SEC", 0.0)', src)

    def test_stage2_is_between_widened_and_base(self):
        """중간 계단은 이름 그대로 1.0과 widen_mult 사이여야 의미가 있다."""
        cfg = cfg_stage2()
        self.assertLess(cfg.stop_loss_grace_stage2_mult, cfg.stop_loss_grace_widen_mult)
        self.assertGreater(cfg.stop_loss_grace_stage2_mult, 1.0)

    def test_stage2_sec_is_after_grace_sec(self):
        cfg = cfg_stage2()
        self.assertGreater(cfg.stop_loss_grace_stage2_sec, cfg.stop_loss_grace_sec)


class GraceMultiplierTests(unittest.TestCase):
    """계단 메커니즘 자체의 검증 — 라이브 .env가 아니라 명시적 픽스처를 쓴다."""

    def setUp(self):
        self.cfg = cfg_stage2()
        self.now = time.time()

    def _mult(self, elapsed):
        return grace_stop_multiplier(self.cfg, self.now - elapsed)

    def test_stage1_during_grace(self):
        self.assertEqual(self._mult(0), self.cfg.stop_loss_grace_widen_mult)
        self.assertEqual(self._mult(179), self.cfg.stop_loss_grace_widen_mult)

    def test_stage2_between_grace_and_stage2(self):
        """예전엔 여기서 곧바로 1.0으로 떨어져 손절이 몰렸다."""
        self.assertEqual(self._mult(181), self.cfg.stop_loss_grace_stage2_mult)
        self.assertEqual(self._mult(249), self.cfg.stop_loss_grace_stage2_mult)

    def test_base_after_stage2(self):
        self.assertEqual(self._mult(251), 1.0)
        self.assertEqual(self._mult(3600), 1.0)

    def test_monotonically_non_increasing(self):
        """계단은 항상 좁아지는 방향이어야 한다(넓어지면 손절이 뒤로 밀려 위험)."""
        prev = None
        for elapsed in range(0, 400, 5):
            m = self._mult(elapsed)
            if prev is not None:
                self.assertLessEqual(m, prev, f"경과 {elapsed}초에서 배수가 커졌다")
            prev = m

    def test_entered_at_none_uses_widest(self):
        """진입 직전 시점(추적정보 없음)은 가장 넓은 폭으로 취급한다."""
        self.assertEqual(grace_stop_multiplier(self.cfg, None), self.cfg.stop_loss_grace_widen_mult)

    def test_disabled_stage2_falls_back_to_two_stage(self):
        """라이브 현재 상태(롤백됨)가 바로 이 경로다."""
        cfg = cfg_stage2(sec=0.0)
        self.assertEqual(grace_stop_multiplier(cfg, self.now - 181), 1.0)

    def test_live_config_is_two_stage(self):
        """롤백 후 라이브 설정이 실제로 2단계로 동작하는지 확인."""
        cfg = Config()
        self.assertEqual(grace_stop_multiplier(cfg, self.now - (cfg.stop_loss_grace_sec + 1)), 1.0)

    def test_grace_disabled_returns_one(self):
        cfg = cfg_stage2()
        cfg.stop_loss_grace_sec = 0.0
        self.assertEqual(grace_stop_multiplier(cfg, self.now - 10), 1.0)


class BothPathsAgreeTests(unittest.TestCase):
    """[2026-08-12 사고 이력] 유예 계산이 main.py와 position_manager.py에 복제돼 있어
    한쪽만 반영되는 바람에 "거래소는 넓혀뒀는데 폴링이 좁은 폭으로 먼저 손절"하는 사고가
    있었다(ONEUSDT ROE -3.04% 즉시청산). 두 경로가 항상 같은 값을 내야 한다."""

    def test_evaluate_path_and_exchange_path_match(self):
        from bot.main import compute_stop_loss_pct
        cfg = cfg_stage2()
        pm = PositionManager(cfg)
        now = time.time()
        for elapsed in (0, 100, 181, 240, 251, 600):
            entered_at = now - elapsed
            for side in ("LONG", "SHORT"):
                exchange_pct, _ = compute_stop_loss_pct(cfg, side, entered_at)
                evaluate_pct = pm._stop_loss_pct_for(side, entered_at)
                self.assertAlmostEqual(
                    exchange_pct, evaluate_pct, places=9,
                    msg=f"{side} 경과 {elapsed}초에서 두 경로가 어긋남",
                )

    def test_widened_flag_true_for_both_stages(self):
        from bot.main import compute_stop_loss_pct
        cfg = cfg_stage2()
        now = time.time()
        _, w1 = compute_stop_loss_pct(cfg, "LONG", now - 100)
        _, w2 = compute_stop_loss_pct(cfg, "LONG", now - 200)
        _, w3 = compute_stop_loss_pct(cfg, "LONG", now - 300)
        self.assertTrue(w1, "1단계는 넓혀진 상태")
        self.assertTrue(w2, "중간 계단도 기본폭보다 넓으므로 True여야 재타이트 로직이 계속 돈다")
        self.assertFalse(w3)


class ReTightenPerStageTests(unittest.TestCase):
    def test_position_tracks_applied_pct(self):
        pm = PositionManager(Config())
        pm.track("BTCUSDT", "LONG", entry_price=100.0, quantity=1.0, leverage=1.0)
        self.assertEqual(pm.positions["BTCUSDT"].applied_stop_loss_pct, 0.0)

    def test_reconcile_retightens_per_stage(self):
        """예전엔 still_widened=False일 때만 재등록해서 중간 계단이 거래소에 반영되지 않았다."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        self.assertIn("tightened_pct < applied - 1e-9 if applied > 0 else not still_widened", src)
        self.assertIn("pos.applied_stop_loss_pct = tightened_pct", src)
        self.assertIn("pos.stop_loss_widened = still_widened", src)



class AppliedPctInitializedTests(unittest.TestCase):
    """[2026-08-16 실거래 로그로 발견한 자체 결함 수정] applied_stop_loss_pct 초기값이 0이라
    `applied <= 0`에 걸려 1단계(가장 넓은 폭)에서도 불필요한 취소/재등록이 일어났고,
    로그도 "중간 계단(15.00%)"처럼 잘못 찍혔다(15%는 1단계 폭이다).
    → STOP_MARKET을 등록하는 모든 지점에서 적용값을 함께 기록하고, 모르는 상태(0)일 때는
      기본폭 구간에서만 맞춰 등록하도록 조건을 좁혔다."""

    def test_all_stop_registrations_record_applied_pct(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main)
        widened_sets = src.count(".stop_loss_widened = widened")
        applied_sets = src.count(".applied_stop_loss_pct = stop_loss_pct")
        self.assertEqual(
            widened_sets, applied_sets,
            "stop_loss_widened를 갱신하는 모든 지점에서 applied_stop_loss_pct도 함께 기록해야 한다",
        )

    def test_unknown_applied_only_syncs_when_not_widened(self):
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        self.assertIn("tightened_pct < applied - 1e-9 if applied > 0 else not still_widened", src)

    def test_log_message_does_not_claim_grace_elapsed(self):
        """1단계에서도 찍히던 '진입 유예기간(180초) 경과' 문구를 실제 경과시간 표기로 교체."""
        import inspect
        from bot import main as bot_main
        src = inspect.getsource(bot_main.reconcile_positions)
        self.assertIn("손절폭을 %s(%.2f%%)으로 좁힘 (진입 후 %.0f초 경과)", src)


if __name__ == "__main__":
    unittest.main()
