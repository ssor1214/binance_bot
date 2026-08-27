"""[2026-08-19 실거래 분석으로 발견] 최근성과 방어 배율이 최소증거금 하한에 막혀 무력화되던 문제.

**증상**
`PositionManager.recent_performance_size_multiplier()`는 docstring대로 "거래 빈도는 유지하고
크기만 줄이는" 설계이고, 로그에도 "최근 10건 성과 방어모드 ... 신규 진입 비중 75% 적용"이
295회 찍혀 있었다. 그런데 실제 주문 크기는 하나도 줄지 않았다.

원인은 compute_position_size의 마지막 클램프다:
    notional = max(notional, effective_min_margin_usdt * leverage)
잔고 4.7 / 비중 15%면 증거금 0.70인데, 0.75를 곱해 0.53이 되어도 tier 하한(잔고 8 미만이면
1.9)이 그대로 이겨 1.9로 되돌아온다. 실측으로 최근 24시간 거래의 99%가 증거금 1.9 부근이었다.
즉 이 계좌 규모에서는 **모든 비중 조절이 무력**이었다(BTC/방향/EV/상관리스크 배율도 동일).

**수정**
방어가 발동한 진입에 한해 하한을 recent_defense_min_margin_usdt까지 낮춘다.
1.4로 잡은 근거: 레버리지 4배에서 명목 5.6 USDT로, 거래소 최소명목 5.0 대비 12% 여유가 있다
(1.25면 명목이 정확히 5.0이라 수량 반올림에서 -4164로 거부될 위험). 실효 축소는
1.90 -> 1.40 = x0.74.

**역시뮬 근거 (원장 1863건, 08-11~08-19)**
    현행                 -46.606
    x0.74 축소(거래수 유지) -41.177  (개선 +5.428)
    walk-forward 전반 +2.990 / 후반 +2.438  (양쪽 플러스)
    일자별 9일 중 8일 플러스 (08-14만 -1.129, 그날은 원래 잘 벌던 날)
완전차단(S4)은 +20.878로 더 크지만 거래수가 36% 줄어 사용자의 거래량 제약과 충돌한다.

**원복**
recent_defense_min_margin_usdt의 코드 기본값이 0.0이라 .env에서 지우거나 0으로 두면
이 블록이 통째로 비활성화되어 기존 동작으로 돌아간다.

이 테스트가 지키는 것:
1. 방어 미발동 시 크기가 절대 안 바뀐다 (회귀 방지)
2. 방어 발동 시 하한이 낮아져 실제로 작아진다
3. 설정 0이면 아무 일도 없다 (원복 경로)
4. 거래소 최소명목 아래로는 내려가지 않는다
5. 호출부가 방어 여부를 실제로 넘긴다
"""
import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot.main import compute_position_size


def _ex(step=0.001, min_notional=5.0):
    """round_quantity는 실제 구현(bot/exchange.py)처럼 거래소 최소명목을 채우도록
    수량을 올려 잡는다. compute_position_size 자체는 최소명목을 강제하지 않고 이 단계에
    의존하므로, 목이 그걸 흉내내지 않으면 안전망을 검증하지 못한다."""
    import math

    def _round(sym, qty, price=None, max_notional=None):
        if price and min_notional and qty * price < min_notional:
            qty = math.ceil((min_notional / price) / step) * step
        return qty

    ex = MagicMock()
    ex.get_symbol_filters.return_value = {"step_size": step, "min_notional": min_notional}
    ex.round_quantity.side_effect = _round
    return ex


def _size(balance, defense_active, floor, price=1.0, lev=4, ratio=0.15, tier_floor=1.9):
    return compute_position_size(
        balance, "TESTUSDT", _ex(), price, ratio, lev, tier_floor,
        0.0, 0.0, 0.0, 0.0, False, False, floor, defense_active,
    )


class DefenseFloorTests(unittest.TestCase):
    BAL = 4.7

    def test_no_defense_size_unchanged(self):
        """방어가 안 걸렸으면 설정이 있어도 크기가 그대로여야 한다."""
        base = _size(self.BAL, defense_active=False, floor=0.0)
        with_cfg = _size(self.BAL, defense_active=False, floor=1.4)
        self.assertEqual(base, with_cfg)

    def test_disabled_config_is_noop(self):
        """설정 0이면 방어가 걸려도 아무 일도 없다 - 원복 경로."""
        base = _size(self.BAL, defense_active=False, floor=0.0)
        self.assertEqual(_size(self.BAL, defense_active=True, floor=0.0), base)

    def test_defense_actually_shrinks(self):
        """옛 코드에서는 이 단언이 실패한다 - 하한 1.9가 이겨 크기가 같았다."""
        base = _size(self.BAL, defense_active=False, floor=1.4)
        shrunk = _size(self.BAL, defense_active=True, floor=1.4)
        self.assertLess(shrunk, base,
                        "방어 발동 시 주문 수량이 실제로 줄어야 한다(하한이 덮어쓰면 안 됨)")
        # 증거금 1.9 -> 1.4 이므로 명목 비율은 약 0.74
        self.assertAlmostEqual(shrunk / base, 1.4 / 1.9, places=2)

    def test_never_below_exchange_min_notional(self):
        """하한을 너무 낮게 주면 compute_position_size는 그대로 통과시키고, 거래소 최소명목은
        round_quantity 단계에서 되올려진다. 즉 설정을 과하게 낮추면 의도한 축소가 아니라
        '되올림'이 일어나 크기가 예측 불가해진다 - 그래서 1.4(명목 5.6, 12% 여유)를 쓴다."""
        qty = _size(self.BAL, defense_active=True, floor=0.1, price=1.0, lev=4)
        self.assertGreaterEqual(qty * 1.0, 5.0,
                                "최종 수량은 거래소 최소명목을 채워야 한다")

    def test_recommended_floor_needs_no_bump(self):
        """1.4는 되올림 없이 그대로 나가야 한다(명목 5.6 >= 5.0)."""
        qty = _size(self.BAL, defense_active=True, floor=1.4, price=1.0, lev=4)
        self.assertAlmostEqual(qty * 1.0, 5.6, places=2)

    def test_large_balance_unaffected(self):
        """잔고 50 이상에서는 비율 사이징이 지배하므로 이 하한 우회가 개입하면 안 된다."""
        base = _size(100.0, defense_active=False, floor=1.4)
        with_def = _size(100.0, defense_active=True, floor=1.4)
        self.assertEqual(base, with_def)

    def test_trade_is_not_blocked(self):
        """크기만 줄이고 진입 자체를 막지 않는다(거래수 유지가 이 변경의 전제)."""
        self.assertGreater(_size(self.BAL, defense_active=True, floor=1.4), 0.0)


class WiringTests(unittest.TestCase):
    """설정과 호출부가 실제로 연결됐는지 - 하나라도 빠지면 조용히 무력화된다."""

    def setUp(self):
        self.src = Path("bot/main.py").read_text(encoding="utf-8-sig")

    def test_config_default_is_disabled(self):
        from bot.config import Config
        import inspect
        src = inspect.getsource(Config)
        self.assertIn('_float("RECENT_DEFENSE_MIN_MARGIN_USDT", 0.0)', src,
                      "기본값은 0.0이어야 한다(설정 없으면 기존 동작 유지)")

    def test_call_site_passes_defense_flag(self):
        self.assertIn('recent_perf_mult < 1.0', self.src,
                      "호출부가 방어 발동 여부를 넘겨야 한다")
        self.assertIn('getattr(cfg, "recent_defense_min_margin_usdt", 0.0)', self.src)

    def test_signature_has_params(self):
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_position_size":
                names = [a.arg for a in node.args.args]
                self.assertIn("recent_defense_min_margin_usdt", names)
                self.assertIn("recent_defense_active", names)
                return
        self.fail("compute_position_size 정의를 찾지 못했다")


if __name__ == "__main__":
    unittest.main()
