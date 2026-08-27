"""[2026-08-25 원칙1 강화] Rule1 완화 사다리 테스트.

주의: _TARGET_POLICY_STATE는 모듈 전역이라 테스트마다 baseline까지 초기화해야 한다.
안 하면 앞 테스트가 남긴 기준선으로 원복돼 엉뚱하게 실패한다(실제로 겪음).

두 가지를 고정한다.
1) 거래 0건 교착 — 기존 조건("거래>=3 AND 손익>0")은 거래가 0이면 영원히 완화가 안 걸렸다.
2) 1분봉 노이즈 필터가 사다리에 whipsaw 다음 순서로 들어가 있는지 —
   실측상 whipsaw(223) + one_min_noise(77)가 전체 차단의 87%였다.
"""
import unittest
from unittest.mock import MagicMock

from bot import main as bot_main
from bot.config import Config


def _cfg():
    cfg = Config()
    cfg.hourly_trade_target = 16
    cfg.slot_fill_target = 0.70
    cfg.target_relax_max_steps = 3
    cfg.min_atr_vs_stop_ratio = 0.7
    cfg.one_min_noise_max_wick_body_ratio = 2.5
    cfg.ema_gap_min_pct = 0.08
    cfg.min_entry_probability = 0.50
    return cfg


def _run(trades, pnl, open_positions=0, times=1):
    bot_main._TARGET_POLICY_STATE.update(
        {"ema_steps": 0, "prob_steps": 0, "whipsaw_steps": 0, "noise_steps": 0,
         "baseline": None, "last": None}
    )
    cfg = _cfg()
    ex = MagicMock()
    ex.get_income_history_stats.return_value = {"trades": trades, "realized_pnl": pnl}
    pm = MagicMock()
    pm.positions = {f"S{i}USDT": object() for i in range(open_positions)}
    results = [bot_main.apply_rule1_target_policy(ex, cfg, pm, 62.0) for _ in range(times)]
    return cfg, results


class Rule1RelaxLadderTests(unittest.TestCase):
    def test_zero_trades_now_relaxes(self):
        """거래 0 + 보유 0이면 실현손실도 0이라 완화해도 원칙 2를 해칠 수 없다."""
        cfg, res = _run(trades=0, pnl=0.0)
        self.assertEqual(res[-1]["action"], "relax_whipsaw_atr")
        self.assertLess(cfg.min_atr_vs_stop_ratio, 0.7)

    def test_zero_trades_with_open_position_does_not_relax(self):
        """보유 포지션이 있으면 평가손실이 있을 수 있어 가뭄 예외를 주지 않는다."""
        _cfg_out, res = _run(trades=0, pnl=0.0, open_positions=1)
        self.assertEqual(res[-1]["action"], "hold")

    def test_noise_filter_is_second_rung(self):
        """whipsaw 3단계를 다 쓴 뒤 1분봉 노이즈 필터가 다음 순서로 풀려야 한다."""
        cfg, res = _run(trades=0, pnl=0.0, times=4)
        self.assertEqual([r["action"] for r in res[:3]], ["relax_whipsaw_atr"] * 3)
        self.assertEqual(res[3]["action"], "relax_one_min_noise")
        self.assertGreater(cfg.one_min_noise_max_wick_body_ratio, 2.5)

    def test_loss_restores_every_knob(self):
        """손익이 마이너스로 돌아서면 노이즈 필터를 포함해 전부 원복돼야 한다."""
        bot_main._TARGET_POLICY_STATE.update(
            {"ema_steps": 0, "prob_steps": 0, "whipsaw_steps": 0, "noise_steps": 0,
         "baseline": None, "last": None}
        )
        cfg = _cfg()
        ex = MagicMock()
        pm = MagicMock()
        pm.positions = {}
        ex.get_income_history_stats.return_value = {"trades": 0, "realized_pnl": 0.0}
        for _ in range(4):
            bot_main.apply_rule1_target_policy(ex, cfg, pm, 62.0)
        self.assertGreater(cfg.one_min_noise_max_wick_body_ratio, 2.5)

        ex.get_income_history_stats.return_value = {"trades": 5, "realized_pnl": -1.0}
        result = bot_main.apply_rule1_target_policy(ex, cfg, pm, 62.0)
        self.assertEqual(result["action"], "restore_baseline")
        self.assertAlmostEqual(cfg.min_atr_vs_stop_ratio, 0.7, places=4)
        self.assertAlmostEqual(cfg.one_min_noise_max_wick_body_ratio, 2.5, places=4)


if __name__ == "__main__":
    unittest.main()


class RelaxNeverRaisesTests(unittest.TestCase):
    """[2026-08-25 버그수정] 완화가 값을 올리면 안 된다.

    기존 코드는 `max(하드바닥, 현재 - step)`이었다. 기준선이 하드바닥보다 낮으면
    이 max()가 값을 위로 끌어올려 "완화가 필터를 더 빡빡하게 만드는" 역전이 생긴다.
    실제로 두 곳에서 발생 중이었다:
      - min_entry_probability: 운영값 0.50인데 max(0.60, 0.48) -> 0.60으로 상승
      - min_atr_vs_stop_ratio: 하한 0으로 내리면 max(0.50, -0.10) -> 0.50으로 상승
    """

    def test_helper_never_raises_when_baseline_below_floor(self):
        from bot.main import _relax_downward
        # 기준선 0.0, 바닥 0.50 — 예전 방식이면 0.50으로 튀어 올랐다
        self.assertEqual(_relax_downward(0.0, 0.10, 0.50, 0.0), 0.0)

    def test_helper_respects_floor_when_baseline_above_it(self):
        from bot.main import _relax_downward
        self.assertAlmostEqual(_relax_downward(0.55, 0.10, 0.50, 0.80), 0.50, places=6)

    def test_helper_lowers_normally(self):
        from bot.main import _relax_downward
        self.assertAlmostEqual(_relax_downward(0.80, 0.10, 0.50, 0.80), 0.70, places=6)

    def test_probability_is_not_raised_from_operational_value(self):
        """운영값 0.50에서 확률 완화 단계가 돌아도 0.50을 넘으면 안 된다(실제 버그)."""
        from bot.main import _relax_downward
        self.assertLessEqual(_relax_downward(0.50, 0.02, 0.60, 0.50), 0.50)

    def test_whipsaw_stays_at_zero_through_full_ladder(self):
        """하한 0 설정에서 완화 사다리를 끝까지 돌려도 0을 유지해야 한다."""
        bot_main._TARGET_POLICY_STATE.update(
            {"ema_steps": 0, "prob_steps": 0, "whipsaw_steps": 0, "noise_steps": 0,
             "baseline": None, "last": None}
        )
        cfg = _cfg()
        cfg.min_atr_vs_stop_ratio = 0.0
        cfg.target_whipsaw_min_atr_floor = 0.0
        cfg.min_entry_probability = 0.50
        ex = MagicMock()
        ex.get_income_history_stats.return_value = {"trades": 0, "realized_pnl": 0.0}
        pm = MagicMock()
        pm.positions = {}
        for _ in range(12):
            bot_main.apply_rule1_target_policy(ex, cfg, pm, 62.0)
        self.assertAlmostEqual(cfg.min_atr_vs_stop_ratio, 0.0, places=6)
        self.assertLessEqual(cfg.min_entry_probability, 0.50)
